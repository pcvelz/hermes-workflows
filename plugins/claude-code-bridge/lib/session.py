"""tmux-backed interactive Claude Code session for one chat channel (claude-code-bridge).

Every tmux call goes through a dedicated server socket (``-L <tmux_socket>``) and a
scrubbed environment, so chat credentials never reach the session.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import chat_hooks, config, mcp, spool

log = logging.getLogger("claude_code_bridge")

PROMPT = "❯"
BUSY_MARK = "esc to interrupt"
TRUST_RE = re.compile(r"(?i)trust (the files|this folder)|Yes, (I trust|proceed)")
BORDER_CHARS = set("─━═")
START_TIMEOUT_S = 90
LAUNCH_SCRIPT = "launch.sh"  # in the channel dir; holds the full claude command line
TMUX_ERR_MAX = 200  # chars of tmux stderr kept in a failure warning
PASTE_BUF = "ccb"
_ENV_KEYS = ("HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "TMPDIR")
_CID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Denied on every launch. The --strict-mcp-config flag does not stop Claude Code's built-in Chrome
# connector (server "claude-in-chrome") from being offered to the model; it is not on the allow list,
# so each call is refused (a deny rule holds in every permission mode) and the chat is told the access was denied.
BUILT_IN_DENIED = ["mcp__claude-in-chrome", "Monitor"]
# Monitor lets a turn end while work is still running; its completion then wakes the session into a new turn.
# Denied so a turn ends only when its work is done. Background Bash is not covered here: no deny rule for it
# has been verified; a self-woken turn is delivered by the worker instead (lib/worker.py, last_reply_to).


def clean_env() -> dict:
    """Minimal environment for tmux and the session: no tokens, no provider keys."""
    env = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
    env["TERM"] = "xterm-256color"
    return env


def _tmux(cfg: dict, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", cfg["tmux_socket"], *args],
        env=clean_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=check,
    )


def name(cfg: dict, cid: str) -> str:
    if not _CID_RE.fullmatch(cid):
        raise ValueError(f"invalid channel id: {cid!r}")
    return f"{cfg['session_prefix']}{cid}"


def _target(cfg: dict, cid: str) -> str:
    """Session target for has-session / kill-session."""
    return "=" + name(cfg, cid)


def _pane(cfg: dict, cid: str) -> str:
    """Pane target (exact session, its current window/pane) for capture/send/paste."""
    return "=" + name(cfg, cid) + ":"


def exists(cfg: dict, cid: str) -> bool:
    return _tmux(cfg, "has-session", "-t", _target(cfg, cid)).returncode == 0


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_launch_script(path: Path, argv: list[str]) -> None:
    """Write the claude command line as an executable script, atomically, owner-only (0700).

    tmux refuses a command line of this length ("command too long" for a multi-KB system prompt), so
    tmux runs only ``/bin/sh <script>``; the script ``exec``s claude, so the pane's process is claude.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n# claude-code-bridge launch line; rewritten at every session launch.\n"
                "exec " + shlex.join(argv) + "\n")
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)


def prepare_dir(cfg: dict, cid: str) -> Path:
    """Channel dir with the bridge's Stop hook and nothing else."""
    d = spool.channel_dir(cfg, cid)
    hook = d / "stop_hook.py"
    shutil.copyfile(Path(__file__).resolve().parent.parent / "stop_hook.py", hook)
    (d / ".claude").mkdir(exist_ok=True)
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(hook))}"
    settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}
    mcp.render_or_empty(config.for_channel(cfg, cid), d)  # channel MCP servers included; always a .mcp.json, so the session always runs --strict-mcp-config
    # The model must not be able to read the rendered file that carries the MCP key.
    # Relative form for the session's own cwd, absolute form for a custom cwd (the file is in the spool dir).
    deny = [f"Read(./{mcp.MCP_FILE})", f"Read(/{d / mcp.MCP_FILE})"]
    # A custom cwd is someone else's project: keep its secret files out of the session's reach.
    custom = custom_cwd(config.for_channel(cfg, cid))
    if custom is not None:
        deny += secret_read_denies(custom)
    _install_browser_guard(d, settings, config.for_channel(cfg, cid))
    _install_write_guards(d, settings, config.for_channel(cfg, cid), custom_cwd(config.for_channel(cfg, cid)))
    hooks = config.chat_hooks_texts(config.for_channel(cfg, cid))
    _install_chat_hooks(d, hooks)
    _install_ask_deny(settings, config.for_channel(cfg, cid),
                      chat_hooks.ask_denied_text(hooks, d) if hooks is not None else None)
    if hooks is None:  # with chat via hooks, the send script's refuse_patterns are the guard instead
        _install_reply_guard(d, config.for_channel(cfg, cid))
    else:
        (d / REPLY_GUARD_FILE).unlink(missing_ok=True)
    _install_save_claim_guard(d, config.for_channel(cfg, cid))
    settings["permissions"] = {"deny": deny}
    _atomic_write(d / ".claude" / "settings.json", json.dumps(settings, indent=2) + "\n")
    return d


BROWSER_GUARD_CONFIG = "browser_guard.json"
BROWSER_GUARD_SCRIPT = "browser_guard.py"
BROWSER_GUARD_TOOLS = "mcp__browser__.*"


def _cap(value) -> int | None:
    """A per-window call cap: a whole number >= 1, or None (unset or 0 = no cap). Anything else raises ValueError."""
    if value in (None, "", 0):
        return None
    if isinstance(value, bool):
        raise ValueError("cap must be a number")
    number = int(value)
    if number < 0:
        raise ValueError("cap must not be negative")
    return number


def _strings(value) -> list[str]:
    if isinstance(value, str):
        value = config._as_list(value)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("expected a list")
    return [str(v) for v in value]


def browser_guard_config(eff: dict) -> dict | None:
    """Normalised browser guard of a channel's effective config (``config.for_channel``), or None when off.

    Keys: ``tools`` (regex of guarded tool names, default ``mcp__browser__.*``), ``network_tools`` (regex of
    the tools that count as network calls for spacing and caps; empty = none), ``min_interval_s``,
    ``max_per_hour``, ``max_per_day`` (0 or unset = no cap), ``deny_url_patterns`` (regexes on
    ``tool_input.url``) and ``deny_tools`` (exact tool names, which must match ``tools``).

    Never raises: the worker polls this through ``fingerprint``. An invalid value yields
    ``{"tools": ..., "misconfigured": <reason>}``, which makes the guard block every guarded call.
    """
    raw = eff.get("browser_guard")
    if not isinstance(raw, dict) or not raw:
        return None
    tools = str(raw.get("tools") or BROWSER_GUARD_TOOLS)
    try:
        re.compile(tools)
        network = str(raw.get("network_tools") or "")
        if network:
            re.compile(network)
        interval = float(raw.get("min_interval_s") or 0)
        if not (0 <= interval < float("inf")):
            raise ValueError("min_interval_s must be a finite number >= 0")
        deny_urls = _strings(raw.get("deny_url_patterns"))
        for pattern in deny_urls:
            re.compile(pattern)
        deny_tools = _strings(raw.get("deny_tools"))
        for name in deny_tools:
            if not re.fullmatch(tools, name):
                raise ValueError(f"deny_tools entry {name!r} is outside tools")
        return {
            "tools": tools,
            "network_tools": network,
            "min_interval_s": interval,
            "max_per_hour": _cap(raw.get("max_per_hour")),
            "max_per_day": _cap(raw.get("max_per_day")),
            "deny_url_patterns": deny_urls,
            "deny_tools": deny_tools,
        }
    except (TypeError, ValueError, re.error) as exc:
        return {"tools": tools, "misconfigured": type(exc).__name__ + ": " + str(exc)}


def _install_browser_guard(d: Path, settings: dict, eff: dict) -> None:
    """PreToolUse guard hook, its script and config when the channel has a browser_guard; otherwise remove stale copies."""
    guard = browser_guard_config(eff)
    script = d / BROWSER_GUARD_SCRIPT
    config_file = d / BROWSER_GUARD_CONFIG
    if guard is None:
        for stale in (script, config_file):
            stale.unlink(missing_ok=True)
        return
    if "misconfigured" in guard:
        log.warning("claude-code-bridge: browser_guard for channel %s is invalid (%s); guarded calls will be blocked",
                    d.name, guard["misconfigured"])
    shutil.copyfile(Path(__file__).resolve().parent.parent / BROWSER_GUARD_SCRIPT, script)
    _atomic_write(config_file, json.dumps(guard, indent=2) + "\n")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    # Appended, not assigned: the write guards and the AskUserQuestion denial may already be in PreToolUse.
    settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {"matcher": guard["tools"], "hooks": [{"type": "command", "command": command}]})


WRITE_GUARD_MATCHER = "Write|Edit|MultiEdit|NotebookEdit"
REPO_SCOPE_SCRIPT = "repo_scope_guard.py"  # plugin root; copied into the channel dir when on
PUBLIC_FIRST_SCRIPT = "public_first_guard.py"
_warned_guards: set[str] = set()


def _warn_guard_once(message: str) -> None:
    if message not in _warned_guards:
        _warned_guards.add(message)
        log.warning("claude-code-bridge: %s", message)


def repo_scope_args(eff: dict, cwd: Path | None) -> list[str] | None:
    """Arguments of the repo scope guard for a channel, or None when it is not installed (fail open): off, no
    ``repo_scope_root``, or no ``cwd`` (a spool channel has no repo of its own to scope to)."""
    if not eff.get("repo_scope_guard") or cwd is None:
        return None
    root = str(eff.get("repo_scope_root") or "").strip()
    if not root:
        _warn_guard_once("repo_scope_guard needs repo_scope_root; the guard is not installed")
        return None
    args = ["--root", str(Path(root).expanduser()), "--session-dir", str(cwd)]
    for companion in eff.get("repo_scope_companions") or []:
        args += ["--companion", str(companion)]
    return args


def public_first_args(eff: dict) -> list[str] | None:
    """Arguments of the public-first guard for a channel, or None when it is not installed (off, or no roots)."""
    if not eff.get("public_first_guard"):
        return None
    public_first = eff.get("public_first") if isinstance(eff.get("public_first"), dict) else {}
    private = str(public_first.get("private_root") or "").strip()
    public = str(public_first.get("public_root") or "").strip()
    if not private or not public:
        _warn_guard_once("public_first_guard needs public_first.private_root and public_root; the guard is not installed")
        return None
    return ["--private-root", str(Path(private).expanduser()), "--public-root", str(Path(public).expanduser())]


def _install_write_guards(d: Path, settings: dict, eff: dict, cwd: Path | None) -> None:
    """PreToolUse write guards (scripts copied into the channel dir); a guard that is off has its copy removed.
    Appended, so the browser guard and the AskUserQuestion denial keep their hooks."""
    for script_name, args in ((REPO_SCOPE_SCRIPT, repo_scope_args(eff, cwd)),
                              (PUBLIC_FIRST_SCRIPT, public_first_args(eff))):
        script = d / script_name
        if args is None:
            script.unlink(missing_ok=True)
            continue
        shutil.copyfile(Path(__file__).resolve().parent.parent / script_name, script)
        command = shlex.join([sys.executable, str(script), *args])
        settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
            {"matcher": WRITE_GUARD_MATCHER, "hooks": [{"type": "command", "command": command}]})


ASK_USER_QUESTION_MESSAGE = "Ask the user in your chat reply instead; there is no interactive user here."


def _install_chat_hooks(d: Path, hooks: dict | None) -> None:
    """Chat via hooks on: write the send script and the channel's runtime texts; off: remove both (and the turn state)."""
    script = d / chat_hooks.SEND_SCRIPT
    runtime = d / chat_hooks.RUNTIME_FILE
    if hooks is None:
        for stale in (script, runtime, d / chat_hooks.TURN_FILE):
            stale.unlink(missing_ok=True)
        return
    shutil.copyfile(Path(__file__).resolve().parent.parent / chat_hooks.SEND_SCRIPT, script)
    _atomic_write(runtime, json.dumps(chat_hooks.runtime_payload(hooks, d), indent=2, ensure_ascii=False) + "\n")


def _install_ask_deny(settings: dict, eff: dict, text: str | None = None) -> None:
    """A PreToolUse hook refuses AskUserQuestion when ``deny_ask_user_question`` is on or chat via hooks is on
    (``text`` = its rendered ask_denied text); exit 2 blocks the call and the message on stderr goes back to the
    model. Appended, so other PreToolUse hooks stay."""
    if text is None and not eff.get("deny_ask_user_question"):
        return
    message = text if text is not None else ASK_USER_QUESTION_MESSAGE
    command = "printf '%s\\n' " + shlex.quote(message) + " >&2; exit 2"
    settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {"matcher": "AskUserQuestion", "hooks": [{"type": "command", "command": command}]})


REPLY_GUARD_FILE = "reply_guard.json"
REPLY_GUARD_NUDGE = (
    "Your reply was not sent: it gives up instead of delivering. Do not apologise or explain. "
    "Continue the task now and post the best result you can in the required format."
)
REPLY_GUARD_MAX_NUDGES = 2
_warned_patterns: set[str] = set()


def reply_guard_config(eff: dict) -> dict | None:
    """Compiled reply guard of a channel's effective config (``config.for_channel``), or None when off.

    Keys: ``patterns`` (regexes, case-sensitive as written: use ``(?i)``), ``nudge`` and ``max_nudges``.
    Off unless at least one pattern compiles; an invalid pattern is logged once and dropped. The Stop hook
    (stop_hook.py) reads the result from ``reply_guard.json`` and does the matching itself.
    """
    raw = eff.get("reply_guard")
    if not isinstance(raw, dict):
        return None
    patterns = raw.get("patterns") or []
    if isinstance(patterns, str):
        patterns = config._as_list(patterns)
    kept = []
    for pattern in patterns:
        text = str(pattern)
        try:
            re.compile(text)
        except re.error as exc:
            if text not in _warned_patterns:
                _warned_patterns.add(text)
                log.warning("claude-code-bridge: reply_guard pattern %r is invalid (%s); ignored", text, exc)
            continue
        kept.append(text)
    if not kept:
        return None
    nudge = raw.get("nudge")
    try:
        cap = REPLY_GUARD_MAX_NUDGES if raw.get("max_nudges") is None else max(0, int(raw["max_nudges"]))
    except (TypeError, ValueError):
        cap = REPLY_GUARD_MAX_NUDGES
    return {
        "patterns": kept,
        "nudge": nudge if isinstance(nudge, str) and nudge.strip() else REPLY_GUARD_NUDGE,
        "max_nudges": cap,
    }


SAVE_CLAIM_FILE = "save_claim_guard.json"  # the Stop hook's switch (stop_hook.py holds the patterns)


def _install_save_claim_guard(d: Path, eff: dict) -> None:
    """Write the save-claim switch for the Stop hook when ``save_claim_guard`` is on; remove a stale copy when off."""
    path = d / SAVE_CLAIM_FILE
    if eff.get("save_claim_guard") is True:
        _atomic_write(path, json.dumps({"enabled": True}) + "\n")
    else:
        path.unlink(missing_ok=True)


def _install_reply_guard(d: Path, eff: dict) -> None:
    """Write the channel's compiled reply guard for the Stop hook; remove a stale copy when it is off."""
    guard = reply_guard_config(eff)
    path = d / REPLY_GUARD_FILE
    if guard is None:
        path.unlink(missing_ok=True)
        return
    _atomic_write(path, json.dumps(guard, indent=2, ensure_ascii=False) + "\n")


SECRET_READ_PATTERNS = ("config/.env", "config/.storage/**", "**/secrets.yaml")


def secret_read_denies(cwd: Path) -> list[str]:
    """Absolute ``Read`` deny rules for the secret locations under a custom cwd."""
    return [f"Read(/{cwd / pattern})" for pattern in SECRET_READ_PATTERNS]


def custom_cwd(eff: dict) -> Path | None:
    """The channel's ``cwd`` override when it names an existing directory, else None (spool dir).

    ``eff`` must already be the channel's effective config (``config.for_channel``).
    """
    raw = eff.get("cwd")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if path.is_dir():
        return path
    log.warning("claude-code-bridge: cwd %r is not a directory; starting in the channel dir", str(raw))
    return None


REPO_PERMISSION_MODES = ("default", "acceptEdits", "plan", "dontAsk", "bypassPermissions")
_warned_modes: set[str] = set()


def repo_permission_mode(eff: dict) -> str | None:
    """The validated ``repo_permission_mode`` of a channel's effective config, or None (unset or unknown).

    An unknown value is logged once and ignored: the repo channel then launches under the user's own settings.
    """
    raw = str(eff.get("repo_permission_mode") or "").strip()
    if not raw:
        return None
    if raw in REPO_PERMISSION_MODES:
        return raw
    if raw not in _warned_modes:
        _warned_modes.add(raw)
        log.warning("claude-code-bridge: repo_permission_mode %r is not one of %s; ignored",
                    raw, ", ".join(REPO_PERMISSION_MODES))
    return None


def _argv(cfg: dict, cid: str, sid: str, has_mcp: bool, resume: bool) -> list[str]:
    cfg = config.for_channel(cfg, cid)  # per-channel allowed_tools / append_system_prompt / cwd
    spool_dir = spool.channel_dir(cfg, cid)
    # A channel with a cwd (a repo) starts Claude Code there exactly as a person would: plain ``claude``, with the
    # user's own settings (user, project, local, defaultMode) deciding the rights. The bridge adds only the Stop
    # hook (additive --settings), the MCP config, the soul and the built-in connector deny; no permission, allow or
    # setting-sources flags. Its MCP servers' tools therefore need allow rules in the user's settings or in claude_args.
    # A channel without a cwd (spool dir) keeps the bridge's own sandbox: setting sources, permission mode, allow list.
    repo = custom_cwd(cfg) is not None
    hooks = config.chat_hooks_texts(cfg)  # chat via hooks: the send script is the only way to answer the chat
    argv = [config.claude_bin(cfg) or "claude", "--model", cfg["model"], "--effort", cfg["effort"]]
    if not repo:
        allowed = mcp.allowed_tools_for(cfg, has_mcp)
        if hooks is not None:
            # Exact send command, as a prefix rule: the session may run it and nothing else of the kind.
            allowed.append(f"Bash({chat_hooks.send_prefix(spool_dir)}:*)")
        argv += [
            "--setting-sources", "project,local",
            "--permission-mode", cfg["permission_mode"],
            "--allowedTools", ",".join(allowed),
        ]
    else:
        mode = repo_permission_mode(cfg)  # unset = no flag: the user's own defaultMode applies
        if mode:
            argv += ["--permission-mode", mode]
    if cfg["append_system_prompt"]:  # empty with chat via hooks (and no channel prompt or soul): no flag at all
        argv += ["--append-system-prompt", cfg["append_system_prompt"]]
    # Deny rules win over allow rules. The built-in connector deny is always present.
    denied = list(BUILT_IN_DENIED)
    for rule in ([] if repo else (str(t) for t in (cfg.get("disallowed_tools") or []))):
        if rule not in denied:
            denied.append(rule)
    argv += ["--disallowedTools", ",".join(denied)]
    # Always strict: only the servers in this file (possibly none), never a project .mcp.json,
    # a user/account MCP server or a plugin's. Without these flags a connector would load.
    argv += ["--mcp-config", str(spool_dir / mcp.MCP_FILE), "--strict-mcp-config"]
    # A repo cwd does not carry the channel dir's Stop hook and deny rules with it, so load them explicitly
    # (additive: they merge with the repo's own settings).
    if repo:
        argv += ["--settings", str(spool_dir / ".claude" / "settings.json")]
    argv += [str(a) for a in (cfg.get("claude_args") or [])]  # verbatim, after the bridge's own flags
    if resume:
        argv += ["--resume", sid]
    else:
        argv += ["--session-id", sid]
    return argv


def launch_argv(cfg: dict, cid: str, state: dict) -> list[str]:
    sid = state.get("session_id") or str(uuid.uuid4())
    has_mcp = mcp.has_servers(spool.channel_dir(cfg, cid))
    return _argv(cfg, cid, sid, has_mcp, bool(state.get("started_once")))


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
# Scripts copied into each channel dir at launch. Their source bodies are part of the launch fingerprint, so a
# code change relaunches the session (the running copy is the old one until then).
COPIED_SCRIPTS = ("stop_hook.py", "browser_guard.py", "chat_send.py", "repo_scope_guard.py", "public_first_guard.py")


def script_sha256() -> dict[str, str | None]:
    """sha256 of each copied script's source under PLUGIN_ROOT (None when missing)."""
    out: dict[str, str | None] = {}
    for name in COPIED_SCRIPTS:
        try:
            out[name] = hashlib.sha256((PLUGIN_ROOT / name).read_bytes()).hexdigest()
        except OSError:
            out[name] = None
    return out


def fingerprint(cfg: dict, cid: str) -> str:
    """sha256 of the launch-relevant config: the argv ``launch_argv`` would build for a new
    session, the sha256 of the rendered ``.mcp.json`` text, and the sha256 of each copied script's source.
    Independent of session state; nothing is written and no MCP content is returned or logged."""
    doc = mcp.payload(config.for_channel(cfg, cid), warn=False)
    argv = _argv(cfg, cid, "fingerprint", doc is not None, False)
    mcp_sha = hashlib.sha256(mcp.dump(doc).encode("utf-8")).hexdigest() if doc is not None else None
    fields = {"argv": argv, "mcp_sha256": mcp_sha, "scripts": script_sha256()}
    if config.for_channel(cfg, cid).get("deny_ask_user_question"):  # only when on, so other channels keep their fingerprint
        fields["deny_ask_user_question"] = True
    guard = browser_guard_config(config.for_channel(cfg, cid))
    if guard is not None:  # only when configured, so unconfigured channels keep their fingerprint
        fields["browser_guard"] = guard
    scope = repo_scope_args(config.for_channel(cfg, cid), custom_cwd(config.for_channel(cfg, cid)))
    public = public_first_args(config.for_channel(cfg, cid))
    if scope is not None or public is not None:  # only when a write guard is installed; other channels keep their fingerprint
        fields["write_guards"] = {"repo_scope": scope, "public_first": public}
    hooks = config.chat_hooks_texts(config.for_channel(cfg, cid))
    if hooks is not None:  # chat via hooks: the texts (and so the launch) change with the file; off = unchanged
        fields["chat_hooks"] = hooks
    reply = reply_guard_config(config.for_channel(cfg, cid)) if hooks is None else None
    if reply is not None:  # same rule: unconfigured channels keep their fingerprint
        fields["reply_guard"] = reply
    if config.for_channel(cfg, cid).get("save_claim_guard") is True:  # only when on, so other channels keep their fingerprint
        fields["save_claim_guard"] = True
    blob = json.dumps(fields, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def start(cfg: dict, cid: str, state: dict) -> bool:
    """Launch the session if needed and wait until it is idle (trust dialog answered once)."""
    if not config.claude_bin(cfg):
        log.warning("claude-code-bridge: no claude binary configured")
        return False
    d = prepare_dir(cfg, cid)
    work = custom_cwd(config.for_channel(cfg, cid)) or d
    if not state.get("session_id"):
        state["session_id"] = str(uuid.uuid4())
    if not exists(cfg, cid):
        script = d / LAUNCH_SCRIPT
        _write_launch_script(script, launch_argv(cfg, cid, state))
        r = _tmux(cfg, "new-session", "-d", "-s", name(cfg, cid),
                  "-x", "200", "-y", "50", "-c", str(work), f"/bin/sh {shlex.quote(str(script))}")
        if r.returncode != 0:
            err = (r.stderr or "").strip()[:TMUX_ERR_MAX]
            log.warning("claude-code-bridge: new-session failed for channel %s: %s", cid, err)
            return False
    deadline = time.monotonic() + START_TIMEOUT_S
    pressed = False
    while time.monotonic() < deadline:
        pane = capture(cfg, cid)
        if not pressed and TRUST_RE.search(pane):
            # The dialog pre-selects "No, exit": move to the "Yes" option before Enter.
            if re.search(r"❯\s*No, exit", pane):
                _tmux(cfg, "send-keys", "-t", _pane(cfg, cid), "Down")
                time.sleep(0.5)
            _tmux(cfg, "send-keys", "-t", _pane(cfg, cid), "Enter")
            pressed = True
            time.sleep(2)
            continue
        if is_idle(cfg, cid):
            state["started_once"] = True
            return True
        time.sleep(1)
    log.warning("claude-code-bridge: session for channel %s not ready in time; killing", cid)
    _tmux(cfg, "kill-session", "-t", _target(cfg, cid))
    return False


_DIM_RE = re.compile(r"\x1b\[2m.*?\x1b\[(?:0|22)?m")
_SGR_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_styled(raw: str) -> str:
    """Drop dim spans (placeholder / ghost suggestions, not typed text), then all escapes."""
    return _SGR_RE.sub("", _DIM_RE.sub("", raw or ""))


def capture(cfg: dict, cid: str) -> str:
    """Pane text with placeholder/ghost text removed (captured with styles, -e)."""
    r = _tmux(cfg, "capture-pane", "-e", "-p", "-t", _pane(cfg, cid))
    return strip_styled(r.stdout) if r.returncode == 0 else ""


def is_busy(pane: str) -> bool:
    return BUSY_MARK in (pane or "").lower()


def _prompt_index(lines: list[str]) -> int | None:
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith(PROMPT):
            return i
    return None


def box_text(pane: str) -> str:
    """Text typed into the input box: the prompt line's remainder plus wrapped lines.

    Stops at a blank line or a box border so the TUI's chrome below the box is not
    counted as typed text.
    """
    lines = (pane or "").splitlines()
    idx = _prompt_index(lines)
    if idx is None:
        return ""
    parts = [lines[idx].lstrip()[len(PROMPT):].strip()]
    for line in lines[idx + 1:]:
        s = line.strip()
        if not s or set(s) <= BORDER_CHARS:
            break
        parts.append(s)
    return " ".join(p for p in parts if p).strip()


def is_idle(cfg: dict, cid: str) -> bool:
    if not exists(cfg, cid):
        return False
    pane = capture(cfg, cid)
    if _prompt_index(pane.splitlines()) is None:
        return False
    return not is_busy(pane) and box_text(pane) == ""


def _enter(cfg: dict, cid: str) -> None:
    _tmux(cfg, "send-keys", "-t", _pane(cfg, cid), "Enter")


def press(cfg: dict, cid: str, key: str) -> bool:
    """Send one named key ("Enter", "Escape") to the channel's pane through the bridge's tmux socket.

    Returns True when tmux accepted the key. Used for blocked-prompt answers (lib/worker.py).
    """
    return _tmux(cfg, "send-keys", "-t", _pane(cfg, cid), key).returncode == 0


def _paste(cfg: dict, cid: str, text: str) -> bool:
    """Bracketed paste of text into the session's input box (no Enter)."""
    fd, path = tempfile.mkstemp(prefix="ccb-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if _tmux(cfg, "load-buffer", "-b", PASTE_BUF, path).returncode != 0:
            return False
        r = _tmux(cfg, "paste-buffer", "-p", "-d", "-b", PASTE_BUF, "-t", _pane(cfg, cid))
        return r.returncode == 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _settle(cfg: dict, cid: str, window: float) -> bool:
    """True once the turn started (busy) or the box emptied, within the window."""
    deadline = time.monotonic() + window
    while True:
        pane = capture(cfg, cid)
        if is_busy(pane) or box_text(pane) == "":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def inject(cfg: dict, cid: str, text: str) -> bool:
    if not text.strip() or not is_idle(cfg, cid):
        return False
    if not _paste(cfg, cid, text):
        return False
    time.sleep(0.8)
    _enter(cfg, cid)
    if _settle(cfg, cid, 3.0):
        return True
    for attempt in range(1, 4):
        time.sleep(0.8 * attempt)
        _enter(cfg, cid)
        if _settle(cfg, cid, 3.0):
            return True
    _tmux(cfg, "send-keys", "-t", _pane(cfg, cid), "C-u")
    log.warning("claude-code-bridge: input stranded in channel %s; cleared line", cid)
    return False


def stop(cfg: dict, cid: str) -> None:
    if not exists(cfg, cid):
        return
    if _paste(cfg, cid, "/exit"):
        time.sleep(0.8)
        _enter(cfg, cid)
    time.sleep(3)
    _tmux(cfg, "kill-session", "-t", _target(cfg, cid))
