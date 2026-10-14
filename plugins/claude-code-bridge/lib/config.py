"""claude-code-bridge configuration: defaults overlaid with the ``claude_code_bridge:`` block of the profile config."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("claude_code_bridge")

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "claude_bin": "",
    "model": "haiku",
    "effort": "medium",
    "transport": "tmux",
    "session_prefix": "claude-code-bridge-",
    "home": "",
    "idle_exit_minutes": 60,
    "permission_mode": "dontAsk",
    "allowed_tools": ["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
    # Tool rules the session must never use, passed as --disallowedTools (deny wins over allow).
    # Empty = no flag. A channel's disallowed_tools is ADDED to this list.
    "disallowed_tools": [],
    "append_system_prompt": (
        "You are answering in a chat channel. Reply in plain text, as one message, "
        "in the user's language. Each message starts with [time · sender]; messages can "
        "arrive late or out of order, so use the timestamps to judge what the latest request is. "
        "Never mention file paths, tools, software, settings, permissions or infrastructure. "
        "If you cannot do something, say in plain words what you can't do here and offer what "
        "you can do instead. Never ask the user to change settings or do technical work."
    ),
    "max_reply_chars": 15000,
    "allowed_users": [],
    "allowed_channels": [],
    "reply_in_thread": False,
    # True = a bridged reply goes where the user wrote: a thread post gets its reply in that thread (root = the
    # post's root_id), a main-channel post gets a top-level reply. Overrides reply_in_thread. Such a reply is sent
    # through the REST API, because the platform adapter drops the thread root unless its own reply mode is "thread".
    "reply_where_written": False,
    # Passthrough (Hermes answers an investigation): True = the reply is a thread on the investigation post
    # (or the existing thread). False = the reply goes where the user wrote: main post -> main reply,
    # thread post -> reply in that thread.
    "passthrough_reply_in_thread": True,
    "wait_budget_minutes": 720,
    "tmux_socket": "claude-code-bridge",
    "poll_seconds": 2,
    # Channel-name prefixes (e.g. "inv-"): every message in a channel whose name starts with one is
    # left to Hermes, never bridged. A name that cannot be resolved is left to Hermes too.
    "passthrough_channel_prefixes": [],
    # Case-insensitive regexes. A match is not bridged: the hook returns None and Hermes answers.
    # The default is English only. Add one regex per trigger word of your own language in the profile config's
    # claude_code_bridge.passthrough_patterns (the list replaces this default).
    "passthrough_patterns": [
        r"\binvestigat",
        r"\bi want to research\b",
        r"\bdeep research\b",
    ],
    # Claude Code MCP servers for the bridge session (empty = none, no flags).
    # A value "keyfile:<service>/<name>" is read from keys_dir at session start; a server
    # whose key is missing is left out of the session (logged, session still starts).
    # Blocked prompts: a dialog in the session's pane that nobody can answer (unattended). mode: off (default,
    # no detection), notify (post the dialog to operator_channel once), decline (notify, then Escape after
    # blocked_timeout_s and a note to the session), accept (press Enter on the first option once it has settled,
    # at most 2 retries, then notify; exempt dialogs are only notified). A channel's blocked_prompt overrides it.
    "blocked_prompt": {"mode": "off"},
    "blocked_settle_s": 60,
    "blocked_timeout_s": 600,
    # Mattermost channel id that receives blocked-dialog notices. Empty = log a WARNING only. Never the bridged channel.
    "operator_channel": "",
    # Deny AskUserQuestion in the session's settings (the answer would wait for a human who is not there).
    "deny_ask_user_question": False,
    # Save-claim guard (off by default, like reply_guard). The Stop hook blocks, once per turn, a final reply that claims
    # something was saved when no Write/Edit succeeded in that turn. Patterns: stop_hook.py SAVE_CLAIM_PATTERNS.
    "save_claim_guard": False,
    "keys_dir": "",
    "mcp_servers": {},
    "mcp_allowed_tools": [],
    # Browser guard for the session's browser MCP tools (empty = off). A channel's browser_guard keys
    # are merged over these; see session.browser_guard_config for the keys and defaults.
    "browser_guard": {},
    # Reply guard (empty = off). A reply matching any of ``patterns`` (case-insensitive regexes) is not posted:
    # ``nudge`` is injected into the session as the turn's next message instead. After ``max_nudges`` nudges
    # in one turn the latest reply is posted anyway. A channel's reply_guard keys are merged over these.
    "reply_guard": {},
    # Chat via hooks (opt-in per channel; off by default). On, the chat contract is not put in the system prompt;
    # the texts come from chat_hooks_file (JSON, template: config/chat-hooks.json.example) and reach the session
    # through the Stop hook, the AskUserQuestion denial, chat_send.py and the injected message prefix.
    # A missing or invalid file turns it off for that launch (logged once), so the contract and reply guard apply.
    "chat_hooks": False,
    "chat_hooks_file": "",
    # Write guards (PreToolUse on Write|Edit|MultiEdit|NotebookEdit; off by default; a repo channel only for the
    # first). repo_scope_guard: a session writes only inside its own repo, its .claude and repo_scope_companions,
    # under repo_scope_root (empty = fail open). public_first_guard: no private copy of a file that exists as a
    # real file under public_first.public_root (see docs/claude-code-bridge.md, "Write guards").
    "repo_scope_guard": False,
    "repo_scope_root": "",
    "repo_scope_companions": [],
    "public_first_guard": False,
    "public_first": {},
    # Permission mode for repo channels (a channel with a cwd) only: "" = unset = plain launch under the user's own
    # settings. default, acceptEdits, plan, dontAsk or bypassPermissions; any other value is warned about and ignored.
    # Spool channels ignore it (they keep ``permission_mode`` above). A channel's value overrides the global one.
    "repo_permission_mode": "",
    # Extra ``claude`` arguments, appended verbatim to the launch line (a list; empty = none). Use it to match how
    # the deployment's user launches Claude Code, e.g. a permission flag. A channel's list is added to this one.
    "claude_args": [],
    # Per-channel overrides: {<channel-id>: {allowed_tools: [...], append_system_prompt: "...", cwd: "..."}}.
    # allowed_tools and disallowed_tools are ADDED to the global lists; append_system_prompt is appended after a blank line.
    # cwd (per channel only, no global default) = directory the session starts in; unset = the channel's spool dir.
    # Quote the channel id in YAML (e.g. "-1001234567890"); a missing or non-directory cwd falls back to the spool dir.
    "channels": {},
}

_cache: dict[str, Any] = {"key": None, "value": None}


def hermes_home() -> Path:
    """``$HERMES_HOME`` if set and non-empty, else ``~/.hermes``."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _cache_key(path: Path) -> tuple[str, int | None]:
    try:
        return (str(path), path.stat().st_mtime_ns)
    except OSError:
        return (str(path), None)


def _as_list(value: str) -> list[str]:
    """Parse a JSON/YAML list string, else split on commas."""
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        parsed = None
    if isinstance(parsed, list):
        return [str(v) for v in parsed]
    return [v.strip() for v in value.split(",") if v.strip()]


def load() -> dict[str, Any]:
    """Return DEFAULTS overlaid with ``claude_code_bridge`` from ``hermes_home()/config.yaml``.

    A missing file or key, or a parse error, yields DEFAULTS (parse errors are logged).
    Results are cached by file path and mtime; each call returns a fresh copy.
    """
    path = hermes_home() / "config.yaml"
    key = _cache_key(path)
    if _cache["key"] != key or _cache["value"] is None:
        cfg = copy.deepcopy(DEFAULTS)
        if key[1] is not None:
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                logger.warning("claude-code-bridge: could not parse %s (%s); using defaults", path, type(exc).__name__)
                raw = None
            block = raw.get("claude_code_bridge") if isinstance(raw, dict) else None
            if isinstance(block, dict):
                cfg.update(block)
            # `hermes config set key '["a"]'` stores lists as strings; accept that form.
            for name, default in DEFAULTS.items():
                if isinstance(default, list) and isinstance(cfg.get(name), str):
                    cfg[name] = _as_list(cfg[name])
            if str(cfg.get("operator_channel") or "").strip():
                logger.warning("claude-code-bridge: operator_channel is set globally; each bridged channel's team is "
                               "checked against it at send time, and a post to another team is never sent. "
                               "Set a per-channel operator_channel for each team.")
        _cache["key"] = key
        _cache["value"] = cfg
    return copy.deepcopy(_cache["value"])


def for_channel(cfg: dict[str, Any], cid: str) -> dict[str, Any]:
    """Effective config for one channel: a copy of ``cfg`` with that channel's override applied.

    Unknown sub-keys are ignored; a missing or malformed ``channels`` block returns a plain copy.
    """
    eff = copy.deepcopy(cfg)
    channels = cfg.get("channels")
    # Keys match on their string form, so unquoted YAML ids (e.g. Telegram -100123...) still resolve.
    override = next((v for k, v in channels.items() if str(k) == cid), None) if isinstance(channels, dict) else None
    if not isinstance(override, dict):
        override = {}  # the global chat_hooks settings still apply below
    operator = override.get("operator_channel")
    if isinstance(operator, str) and operator.strip():
        eff["operator_channel"] = operator.strip()  # this channel's operator (its team's); overrides the global one
    for key in ("repo_scope_guard", "public_first_guard", "save_claim_guard"):
        if isinstance(override.get(key), bool):
            eff[key] = override[key]
    root = override.get("repo_scope_root")
    if isinstance(root, str) and root.strip():
        eff["repo_scope_root"] = root.strip()
    companions = override.get("repo_scope_companions")
    if isinstance(companions, str):
        companions = _as_list(companions)
    if isinstance(companions, list):  # added to the global list, like disallowed_tools
        merged = list(eff.get("repo_scope_companions") or [])
        eff["repo_scope_companions"] = merged + [str(c) for c in companions if str(c) not in merged]
    public_first = override.get("public_first")
    if isinstance(public_first, dict) and public_first:
        base_pf = eff.get("public_first")
        merged_pf = dict(base_pf) if isinstance(base_pf, dict) else {}
        merged_pf.update({str(k): v for k, v in public_first.items()})
        eff["public_first"] = merged_pf
    chat = override.get("chat_hooks")
    if isinstance(chat, bool):
        eff["chat_hooks"] = chat
    hooks_file = override.get("chat_hooks_file")
    if isinstance(hooks_file, str) and hooks_file.strip():
        eff["chat_hooks_file"] = hooks_file.strip()
    if chat_hooks_texts(eff) is not None:
        # Chat via hooks: the chat contract is not in the system prompt; the channel's own prompt and soul still are.
        eff["append_system_prompt"] = ""
    cwd = override.get("cwd")
    if cwd:
        eff["cwd"] = str(cwd)
    # Launch model / effort for this channel only; a blank or non-string value keeps the global one.
    for key in ("model", "effort"):
        value = override.get(key)
        if isinstance(value, str) and value:
            eff[key] = value
    extra = override.get("allowed_tools")
    if isinstance(extra, str):
        extra = _as_list(extra)
    if isinstance(extra, list):
        eff["allowed_tools"] = list(eff.get("allowed_tools") or []) + [str(t) for t in extra]
    # Channel MCP servers merge into the global map by name (a channel entry wins on a name clash).
    servers = override.get("mcp_servers")
    if isinstance(servers, dict) and servers:
        base = eff.get("mcp_servers")
        merged_servers = dict(base) if isinstance(base, dict) else {}
        merged_servers.update({str(k): v for k, v in servers.items()})
        eff["mcp_servers"] = merged_servers
    repo_mode = override.get("repo_permission_mode")
    if isinstance(repo_mode, str) and repo_mode.strip():
        eff["repo_permission_mode"] = repo_mode.strip()
    extra_args = override.get("claude_args")
    if isinstance(extra_args, list):
        eff["claude_args"] = list(eff.get("claude_args") or []) + [str(a) for a in extra_args]
    mcp_extra = override.get("mcp_allowed_tools")
    if isinstance(mcp_extra, str):
        mcp_extra = _as_list(mcp_extra)
    if isinstance(mcp_extra, list):
        eff["mcp_allowed_tools"] = list(eff.get("mcp_allowed_tools") or []) + [str(t) for t in mcp_extra]
    guard = override.get("browser_guard")
    if isinstance(guard, dict) and guard:
        base_guard = eff.get("browser_guard")
        merged_guard = dict(base_guard) if isinstance(base_guard, dict) else {}
        merged_guard.update({str(k): v for k, v in guard.items()})
        eff["browser_guard"] = merged_guard
    reply_guard = override.get("reply_guard")
    if isinstance(reply_guard, dict) and reply_guard:
        base_reply_guard = eff.get("reply_guard")
        merged_reply_guard = dict(base_reply_guard) if isinstance(base_reply_guard, dict) else {}
        merged_reply_guard.update({str(k): v for k, v in reply_guard.items()})
        eff["reply_guard"] = merged_reply_guard
    blocked = override.get("blocked_prompt")
    if isinstance(blocked, str) and blocked.strip():
        blocked = {"mode": blocked.strip()}
    if isinstance(blocked, dict) and blocked:
        base_blocked = eff.get("blocked_prompt")
        merged_blocked = dict(base_blocked) if isinstance(base_blocked, dict) else {}
        merged_blocked.update({str(k): v for k, v in blocked.items()})
        eff["blocked_prompt"] = merged_blocked
    denied = override.get("disallowed_tools")
    if isinstance(denied, str):
        denied = _as_list(denied)
    if isinstance(denied, list):
        merged = list(eff.get("disallowed_tools") or [])
        eff["disallowed_tools"] = merged + [str(t) for t in denied if str(t) not in merged]
    prompt = override.get("append_system_prompt")
    if isinstance(prompt, str) and prompt.strip():
        eff["append_system_prompt"] = f"{eff.get('append_system_prompt') or ''}\n\n{prompt}".lstrip("\n")
    soul = _soul_text(override.get("soul_file"))
    if soul:
        eff["append_system_prompt"] = f"{eff.get('append_system_prompt') or ''}\n\n{soul}".lstrip("\n")
    return eff


def soul_path(raw: str) -> Path:
    """``soul_file`` as a path: ``~`` expanded; a relative path resolves against ``hermes_home()``."""
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else hermes_home() / path


def _soul_text(raw: Any) -> str:
    """Stripped text of a channel's ``soul_file``; "" when unset, unreadable or blank (logged, never raised)."""
    if not raw:
        return ""
    path = soul_path(str(raw))
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "claude-code-bridge: soul_file %s not readable (%s); using the prompt without it",
            path, type(exc).__name__,
        )
        return ""


CHAT_HOOK_TEXT_KEYS = ("audience", "ask_denied", "stop_nothing_sent", "send_refused", "prompt_context")
_warned_chat_hooks: set[str] = set()


def _chat_hooks_problem(data: Any) -> str | None:
    """Why a chat-hooks texts document is unusable, or None when it is valid."""
    if not isinstance(data, dict):
        return "not a JSON object"
    for key in CHAT_HOOK_TEXT_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"'{key}' must be a non-empty string"
    rules = data.get("refuse_patterns")
    if not isinstance(rules, list):
        return "'refuse_patterns' must be a list"
    for rule in rules:
        if not (isinstance(rule, dict) and isinstance(rule.get("pattern"), str) and isinstance(rule.get("reason"), str)):
            return "each refuse_patterns entry needs a 'pattern' and a 'reason' string"
        try:
            re.compile(rule["pattern"])
        except re.error as exc:
            return f"refuse pattern {rule['pattern']!r} is invalid ({exc})"
    cap = data.get("max_stop_blocks")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        return "'max_stop_blocks' must be a whole number >= 0"
    return None


def _chat_hooks_warn(reason: str) -> None:
    if reason not in _warned_chat_hooks:
        _warned_chat_hooks.add(reason)
        logger.warning("claude-code-bridge: chat_hooks is off for this launch: %s", reason)


def chat_hooks_texts(eff: dict[str, Any]) -> dict[str, Any] | None:
    """Validated chat-hooks texts of a channel's effective config (``for_channel``), or None when chat_hooks is off
    or its file is unusable (logged once).

    Keys: the strings in CHAT_HOOK_TEXT_KEYS, ``refuse_patterns`` (list of {pattern, reason}) and ``max_stop_blocks``.
    """
    if not eff.get("chat_hooks"):
        return None
    raw_file = str(eff.get("chat_hooks_file") or "").strip()
    if not raw_file:
        _chat_hooks_warn("chat_hooks_file is not set")
        return None
    path = soul_path(raw_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _chat_hooks_warn(f"{path} is not readable ({type(exc).__name__})")
        return None
    problem = _chat_hooks_problem(data)
    if problem:
        _chat_hooks_warn(f"{path}: {problem}")
        return None
    out: dict[str, Any] = {key: data[key] for key in CHAT_HOOK_TEXT_KEYS}
    out["refuse_patterns"] = [{"pattern": r["pattern"], "reason": r["reason"]} for r in data["refuse_patterns"]]
    out["max_stop_blocks"] = data["max_stop_blocks"]
    return out


def keys_dir(cfg: dict[str, Any]) -> Path:
    """``cfg["keys_dir"]`` (expanded) or ``hermes_home()/"keys"``."""
    configured = cfg.get("keys_dir")
    if configured:
        return Path(str(configured)).expanduser()
    return hermes_home() / "keys"


def home(cfg: dict[str, Any]) -> Path:
    """Bridge state root: ``cfg["home"]`` (expanded) or ``hermes_home()/"claude-code-bridge"``."""
    configured = cfg.get("home")
    if configured:
        return Path(str(configured)).expanduser()
    return hermes_home() / "claude-code-bridge"


def claude_bin(cfg: dict[str, Any]) -> str | None:
    """``cfg["claude_bin"]`` if set, else the first ``claude`` on PATH, else None."""
    configured = cfg.get("claude_bin")
    if configured:
        return str(configured)
    return shutil.which("claude")
