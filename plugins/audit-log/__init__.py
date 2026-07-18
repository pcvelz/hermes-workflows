"""audit-log — persist every EXECUTED agent command/edit to a structured log.

Hermes has no per-command audit log. This plugin adds one: a ``post_tool_call``
hook (the Hermes equivalent of Claude Code's ``PostToolUse``) appends one line
per executed tool call to

    <repo>/logs/bash-commands.log   <- terminal commands
    <repo>/logs/file-edits.log      <- write_file / edit_file / patch

The ``logs/`` folder lives in the project root and is gitignored (the Hermes
analog of Claude Code's ``.claude/logs/``). It is resolved repo-root-relative
from this file (symlink-safe via ``.resolve()``) and overridable via the
``HERMES_CMDLOG_DIR`` env var (used by the self-test).

Design notes:
  * Observational ONLY — this hook never returns a block action. Gating risky
    commands is the ``policy-gate`` plugin's job (a ``pre_tool_call`` hook).
    ``post_tool_call`` fires AFTER execution, so a command logged here actually
    ran; a command blocked by policy-gate never reaches this hook.
  * Fail-open and silent: any error is swallowed so logging can never break a
    tool call (the runtime also wraps hook errors, but we defend anyway).
  * cwd is NOT in the post_tool_call payload; it is recorded per-command in
    ``agent.log`` and is cross-referenceable by session id.

Log line formats (chosen to grep like the CC logs):
    bash-commands.log : [YYYY-MM-DD HH:MM:SS] [sid] [EXECUTED] [<dur>] [exit=<n>] <command>
    file-edits.log    : [YYYY-MM-DD HH:MM:SS] [sid] [<tool>] <path>
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

try:  # single source of truth for the (profile-aware) Hermes home
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - defensive fallback if import path shifts
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path(os.path.expanduser("~/.hermes"))


_TERMINAL_TOOLS = {"terminal"}
# tool name -> the args key holding the human-meaningful target path
_EDIT_TOOLS = {"write_file": "path", "edit_file": "path", "patch": "path"}


# ---------------------------------------------------------------------------
# Secret scrubbing — NEVER write a credential to the log. Logging commands
# verbatim would persist any secret pasted into a command or inline script on
# disk forever, so every line is scrubbed before it is written. This targets
# SECRETS (passwords / keys / tokens), not general PII.
# ---------------------------------------------------------------------------

# Env-var NAME fragments whose VALUES must never appear in the log verbatim.
_SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

# key=value / key: value forms (require an explicit : or = so we don't mangle
# innocent commands like `glab token list`).
_CRED_KV = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"auth[_-]?token|client[_-]?secret)\b\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)
_FLAG = re.compile(r"(?i)(--?(?:password|passwd|token|api[-_]?key|secret)[= ])(\S+)")
_AUTH = re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)(\S+)")
_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_URLCRED = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:)([^\s/@]+)(@)")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _secret_env_values():
    vals = []
    for k, v in os.environ.items():
        if not v or len(v) < 6:
            continue
        ku = k.upper()
        if any(h in ku for h in _SECRET_ENV_HINTS):
            vals.append(v)
    # longest first so the largest secret is redacted before any substring of it
    return sorted(set(vals), key=len, reverse=True)


def _scrub(text):
    """Redact secrets from a command/path before it is written to the log."""
    if not isinstance(text, str) or not text:
        return text
    s = text
    for v in _secret_env_values():
        if v in s:
            s = s.replace(v, "[REDACTED:env-secret]")
    s = _CRED_KV.sub(lambda m: f"{m.group(1)}[REDACTED]", s)
    s = _FLAG.sub(lambda m: f"{m.group(1)}[REDACTED]", s)
    s = _AUTH.sub(lambda m: f"{m.group(1)}[REDACTED]", s)
    s = _AWS.sub("[REDACTED:aws-key]", s)
    s = _URLCRED.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}", s)
    s = _PEM.sub("[REDACTED:private-key]", s)
    return s


def _logdir() -> Path:
    """Resolve the log dir: a project-local ``logs/`` folder (gitignored) — the
    Hermes analog of Claude Code's ``.claude/logs/``. Repo-root-relative from this
    file (``.resolve()`` follows the profile->repo plugin symlink); env-overridable
    for tests; falls back to ``{HERMES_HOME}/logs`` if repo resolution ever fails."""
    override = os.environ.get("HERMES_CMDLOG_DIR", "").strip()
    if override:
        d = Path(override)
    else:
        try:
            d = Path(__file__).resolve().parents[2] / "logs"
        except Exception:
            d = get_hermes_home() / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _short_session(session_id) -> str:
    if not session_id or not isinstance(session_id, str):
        return "--------"
    # Hermes ids look like 20260101_120000_abc123 -> keep the unique tail token
    return session_id.rsplit("_", 1)[-1][:12]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _exit_code(result):
    """Best-effort extract an exit/return code from a terminal result JSON."""
    if not isinstance(result, str):
        return None
    try:
        obj = json.loads(result)
    except Exception:
        return None
    if isinstance(obj, dict):
        for k in ("exit_code", "returncode", "return_code", "exit_status", "code", "status"):
            v = obj.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                return v
    return None


def _append(path: Path, line: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _on_post_tool_call(tool_name=None, args=None, result=None, session_id=None,
                       duration_ms=None, **kwargs):
    try:
        args = args or {}
        sid = _short_session(session_id)
        ts = _now()
        dur = f"{int(duration_ms)}ms" if isinstance(duration_ms, (int, float)) else "-"

        if tool_name in _TERMINAL_TOOLS:
            cmd = args.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                return None
            rc = _exit_code(result)
            rc_s = f"exit={rc}" if rc is not None else "exit=?"
            cmd1 = _scrub(cmd).replace("\r", "").replace("\n", "\\n")
            _append(_logdir() / "bash-commands.log",
                    f"[{ts}] [{sid}] [EXECUTED] [{dur}] [{rc_s}] {cmd1}")
        elif tool_name in _EDIT_TOOLS:
            path = args.get(_EDIT_TOOLS[tool_name])
            if not isinstance(path, str) or not path:
                return None
            _append(_logdir() / "file-edits.log",
                    f"[{ts}] [{sid}] [{tool_name}] {_scrub(path)}")
    except Exception:
        pass  # never let logging break a tool call
    return None


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)


# ---------------------------------------------------------------------------
# Runnable self-test:  python3 plugins/audit-log/__init__.py
# Writes a couple of synthetic lines to a temp log dir and prints them.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    tmp = tempfile.mkdtemp(prefix="audit-log-selftest-")
    os.environ["HERMES_CMDLOG_DIR"] = tmp
    _on_post_tool_call(
        tool_name="terminal",
        args={"command": "ls -la /; echo hi"},
        result='{"output": "...", "exit_code": 0}',
        session_id="20260101_120000_abc123",
        duration_ms=42,
    )
    _on_post_tool_call(
        tool_name="write_file",
        args={"path": "/tmp/example.txt"},
        result='{"ok": true}',
        session_id="20260101_120000_abc123",
        duration_ms=3,
    )
    # scrubbing demo: env-secret value + keyword cred + Authorization bearer
    os.environ["DEMO_API_TOKEN"] = "tok_live_ABC123XYZ789"
    _on_post_tool_call(
        tool_name="terminal",
        args={"command": "curl -H 'Authorization: Bearer tok_live_ABC123XYZ789' https://api.x/v1 "
                         "&& cat > cfg.yaml <<'E'\npassword: S3cr3tP@ss\nE"},
        result='{"exit_code": 0}',
        session_id="20260101_120000_abc123",
        duration_ms=12,
    )
    for name in ("bash-commands.log", "file-edits.log"):
        p = Path(tmp) / name
        print(f"--- {p} ---")
        print(p.read_text(encoding="utf-8").rstrip())
