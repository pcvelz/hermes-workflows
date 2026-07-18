"""prompt-log — log every user prompt (secrets scrubbed) to logs/user-prompts.log.

The Hermes analog of Claude Code's ``log-user-prompt.sh`` UserPromptSubmit hook.
Completes the audit trio alongside audit-log's ``bash-commands.log`` (terminal
commands) and ``file-edits.log`` (write/edit/patch operations):

    bash-commands.log   <- every terminal command the agent executed
    file-edits.log      <- every file write / edit / patch
    user-prompts.log    <- every user message that initiated a turn  ← THIS PLUGIN

Log line format:
    [YYYY-MM-DD HH:MM:SS] [sid] [PROMPT] <scrubbed prompt, newlines→\\n, ≤500 chars>

where ``sid`` is the last 12 chars of the session-id tail token (e.g. ``abc123``
from ``20260101_120000_abc123``), or ``--------`` when no session id is available.

Design notes:
  * Observational ONLY — always returns None; never injects context (that is a
    steering plugin's job, e.g. skill-router). This is a pure append-only audit log.
  * Fail-open and silent: the entire body is wrapped in try/except → pass so a
    logging error can never interrupt a user turn.
  * ``pre_llm_call`` fires before the tool-calling loop on every turn, which is the
    earliest safe point to capture the user message.  The kwarg is ``user_message``.
  * The log dir is resolved repo-root-relative (``plugins/prompt-log/`` →
    ``../../logs/``) via ``.resolve()`` which follows the profile→repo plugin
    symlink, so the log always lands in the repo regardless of which profile
    loaded the plugin.  Override with ``HERMES_CMDLOG_DIR`` (shared with
    audit-log for easy redirection in tests).
  * Scrubbing matches audit-log's ``_CRED_KV`` + Authorization + env-secret
    patterns; secrets pasted into prompts are redacted before hitting disk.
"""
import os
import re
from datetime import datetime
from pathlib import Path

try:
    from hermes_constants import get_hermes_home
except Exception:
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path(os.path.expanduser("~/.hermes"))


# ---------------------------------------------------------------------------
# Secret scrubbing — copied verbatim in spirit from audit-log._scrub().
# ---------------------------------------------------------------------------

_SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

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

_PROMPT_TRUNCATE = 500


def _secret_env_values():
    vals = []
    for k, v in os.environ.items():
        if not v or len(v) < 6:
            continue
        ku = k.upper()
        if any(h in ku for h in _SECRET_ENV_HINTS):
            vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


def _scrub(text: str) -> str:
    """Redact credential/secret material from a prompt before logging."""
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


# ---------------------------------------------------------------------------
# Log directory resolution — same contract as audit-log._logdir().
# ---------------------------------------------------------------------------

def _logdir() -> Path:
    """Resolve <repo>/logs/ — shared with audit-log, overridable for tests."""
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
    """Extract the 12-char tail token from a Hermes session id, or '--------'."""
    if not session_id or not isinstance(session_id, str):
        return "--------"
    return session_id.rsplit("_", 1)[-1][:12]


# ---------------------------------------------------------------------------
# Hook implementation
# ---------------------------------------------------------------------------

def _on_pre_llm_call(session_id=None, user_message=None, **kwargs):
    """``pre_llm_call`` callback — log the user message and always return None.

    This hook is OBSERVATIONAL ONLY.  It never returns a dict; doing so would
    inject context into the turn (that is a steering plugin's job, not ours).
    """
    try:
        if not isinstance(user_message, str) or not user_message.strip():
            return None

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sid = _short_session(session_id)

        # Scrub secrets, flatten newlines, truncate.
        cleaned = _scrub(user_message)
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = cleaned.replace("\n", "\\n")
        if len(cleaned) > _PROMPT_TRUNCATE:
            cleaned = cleaned[:_PROMPT_TRUNCATE] + "…"

        line = f"[{ts}] [{sid}] [PROMPT] {cleaned}"

        log_path = _logdir() / "user-prompts.log"
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    except Exception:
        pass  # fail-open: never interrupt a user turn

    return None


def register(ctx) -> None:
    """Plugin entrypoint — register the per-turn prompt logging hook."""
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)


# ---------------------------------------------------------------------------
# Runnable self-test:  python3 plugins/prompt-log/__init__.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    tmp = tempfile.mkdtemp(prefix="promptlog-selftest-")
    os.environ["HERMES_CMDLOG_DIR"] = tmp

    # (a) Normal prompt
    _on_pre_llm_call(
        session_id="20260101_120000_abc123",
        user_message="What time is it in Amsterdam?",
    )

    # (b) Prompt containing secrets
    _on_pre_llm_call(
        session_id="20260101_120000_abc123",
        user_message="my api_key=sk_live_SECRET123 and password: hunter2 — please help",
    )

    log_path = Path(tmp) / "user-prompts.log"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()

    print("--- user-prompts.log ---")
    for line in lines:
        print(line)
    print()

    # Assertions
    ok = True

    # Line (a): normal prompt must appear intact (no redaction marker)
    assert len(lines) >= 2, "Expected at least 2 log lines"
    if "[REDACTED]" in lines[0]:
        print("FAIL: normal prompt was incorrectly redacted")
        ok = False
    elif "What time is it in Amsterdam?" not in lines[0]:
        print("FAIL: normal prompt not found in log line")
        ok = False

    # Line (b): secret VALUES must not appear verbatim
    secret_values = ["sk_live_SECRET123", "hunter2"]
    for secret in secret_values:
        if secret in lines[1]:
            print(f"FAIL: secret value '{secret}' found verbatim in log line")
            ok = False

    # Line (b): scrub markers must be present
    if "[REDACTED]" not in lines[1]:
        print("FAIL: expected [REDACTED] marker in scrubbed line")
        ok = False

    # Return None contract
    result = _on_pre_llm_call(
        session_id="test",
        user_message="check None return",
    )
    if result is not None:
        print(f"FAIL: hook returned {result!r} instead of None")
        ok = False

    print("PASS" if ok else "FAIL")
