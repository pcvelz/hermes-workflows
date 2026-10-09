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

from . import config, mcp, spool

log = logging.getLogger("claude_code_bridge")

PROMPT = "❯"
BUSY_MARK = "esc to interrupt"
TRUST_RE = re.compile(r"(?i)trust (the files|this folder)|Yes, (I trust|proceed)")
BORDER_CHARS = set("─━═")
START_TIMEOUT_S = 90
PASTE_BUF = "ccb"
_ENV_KEYS = ("HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "TMPDIR")
_CID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


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


def prepare_dir(cfg: dict, cid: str) -> Path:
    """Channel dir with the bridge's Stop hook and nothing else."""
    d = spool.channel_dir(cfg, cid)
    hook = d / "stop_hook.py"
    shutil.copyfile(Path(__file__).resolve().parent.parent / "stop_hook.py", hook)
    (d / ".claude").mkdir(exist_ok=True)
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(hook))}"
    settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}
    mcp.render_or_empty(cfg, d)  # always a .mcp.json, so the session always runs --strict-mcp-config
    # The model must not be able to read the rendered file that carries the MCP key.
    settings["permissions"] = {"deny": [f"Read(./{mcp.MCP_FILE})"]}
    _atomic_write(d / ".claude" / "settings.json", json.dumps(settings, indent=2) + "\n")
    return d


def _argv(cfg: dict, cid: str, sid: str, has_mcp: bool, resume: bool) -> list[str]:
    cfg = config.for_channel(cfg, cid)  # per-channel allowed_tools / append_system_prompt
    argv = [
        config.claude_bin(cfg) or "claude",
        "--model", cfg["model"],
        "--effort", cfg["effort"],
        "--setting-sources", "project,local",
        "--permission-mode", cfg["permission_mode"],
        "--allowedTools", ",".join(mcp.allowed_tools_for(cfg, has_mcp)),
        "--append-system-prompt", cfg["append_system_prompt"],
    ]
    # Deny rules win over allow rules; no flag at all when none are configured.
    denied = [str(t) for t in (cfg.get("disallowed_tools") or [])]
    if denied:
        argv += ["--disallowedTools", ",".join(denied)]
    # Always strict: only the servers in this file (possibly none), never a project .mcp.json,
    # a user/account MCP server or a plugin's. Without these flags a connector would load.
    argv += ["--mcp-config", str(spool.channel_dir(cfg, cid) / mcp.MCP_FILE), "--strict-mcp-config"]
    if resume:
        argv += ["--resume", sid]
    else:
        argv += ["--session-id", sid]
    return argv


def launch_argv(cfg: dict, cid: str, state: dict) -> list[str]:
    sid = state.get("session_id") or str(uuid.uuid4())
    has_mcp = mcp.has_servers(spool.channel_dir(cfg, cid))
    return _argv(cfg, cid, sid, has_mcp, bool(state.get("started_once")))


def fingerprint(cfg: dict, cid: str) -> str:
    """sha256 of the launch-relevant config: the argv ``launch_argv`` would build for a new
    session, plus the sha256 of the rendered ``.mcp.json`` text. Independent of session state;
    nothing is written and no MCP content is returned or logged."""
    doc = mcp.payload(cfg, warn=False)
    argv = _argv(cfg, cid, "fingerprint", doc is not None, False)
    mcp_sha = hashlib.sha256(mcp.dump(doc).encode("utf-8")).hexdigest() if doc is not None else None
    blob = json.dumps({"argv": argv, "mcp_sha256": mcp_sha}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def start(cfg: dict, cid: str, state: dict) -> bool:
    """Launch the session if needed and wait until it is idle (trust dialog answered once)."""
    if not config.claude_bin(cfg):
        log.warning("claude-code-bridge: no claude binary configured")
        return False
    d = prepare_dir(cfg, cid)
    if not state.get("session_id"):
        state["session_id"] = str(uuid.uuid4())
    if not exists(cfg, cid):
        argv = launch_argv(cfg, cid, state)
        r = _tmux(cfg, "new-session", "-d", "-s", name(cfg, cid),
                  "-x", "200", "-y", "50", "-c", str(d), shlex.join(argv))
        if r.returncode != 0:
            log.warning("claude-code-bridge: new-session failed for channel %s", cid)
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
