"""memory-guard — block the agent from persisting secrets into persistent memory.

Hermes' memory tool writes durable entries to MEMORY.md / USER.md that survive
across sessions and are injected into every future turn. If the agent is handed
a credential mid-session (e.g. "my Gmail password is hunter2") it can
proactively save it to memory — permanently leaking the secret to every future
session context.

This plugin is the Hermes analog of Claude Code's ``block-memory-write.sh``
PreToolUse hook. It registers a ``pre_tool_call`` hook that intercepts every
call to the ``memory`` tool. If the call is a write-ish action (add, replace)
AND the content contains something that looks like a secret, the call is blocked
with a clear, operator-visible message. All other calls (remove, read, or
content that is clean) pass through untouched.

Fail-open: any exception in the hook returns None (allow). A plugin error must
never break a legitimate memory write.

Secret detection scope (v1 — secrets only, not general PII):
  * Secret-named env-var values present in the text (e.g. GMAIL_PASSWORD value)
  * key=value / key: value credential forms
    (password, passwd, secret, token, api_key, access_key, auth_token,
     client_secret, private_key, oauth_token)
  * CLI flag forms  (--password=X, --token X, etc.)
  * Authorization: Bearer/Basic <value>
  * AWS access key IDs (AKIA…)
  * URL-embedded credentials (scheme://user:pass@…)
  * PEM private key headers (-----BEGIN … PRIVATE KEY-----)
"""
import os
import re

# ---------------------------------------------------------------------------
# Write-ish actions that carry new secret-bearing content into memory.
# "remove" only carries old_text (a lookup fragment) and "read" carries nothing
# — neither can introduce a new secret, so we leave them alone.
# ---------------------------------------------------------------------------
_WRITE_ACTIONS = {"add", "replace"}

# ---------------------------------------------------------------------------
# Secret-pattern regexes (same family as audit-log's _scrub, adapted to
# DETECT rather than redact — we need a bool, not a cleaned string).
# ---------------------------------------------------------------------------

# Env-var name fragments whose values should never enter memory verbatim.
_SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

# key=value / key: value forms.
_CRED_KV = re.compile(
    r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"auth[_-]?token|client[_-]?secret|private[_-]?key|oauth[_-]?token)\b"
    r"\s*[:=]\s*"
    r"(?:\"[^\"]{3,}\"|'[^']{3,}'|\S{3,})"
)

# CLI flag forms (--password=VALUE, --token VALUE).
_FLAG = re.compile(
    r"(?i)--?(?:password|passwd|token|api[-_]?key|secret)[= ]\S+"
)

# HTTP Authorization header values.
_AUTH = re.compile(
    r"(?i)authorization:\s*(?:bearer|basic)\s+\S+"
)

# AWS access key IDs.
_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

# URL-embedded credentials (scheme://user:pass@host).
_URLCRED = re.compile(
    r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s/@]+@"
)

# PEM private key block header.
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _secret_env_values():
    """Return live env-var values whose names suggest they hold secrets."""
    vals = []
    for k, v in os.environ.items():
        if not v or len(v) < 6:
            continue
        ku = k.upper()
        if any(h in ku for h in _SECRET_ENV_HINTS):
            vals.append(v)
    # longest first so a longer secret matches before any substring
    return sorted(set(vals), key=len, reverse=True)


def _contains_secret(text: str) -> bool:
    """Return True if *text* appears to contain a secret credential."""
    if not isinstance(text, str) or not text:
        return False
    # Check live env-var secrets first (fastest signal when they are present).
    for v in _secret_env_values():
        if v in text:
            return True
    # Pattern-based checks.
    if _CRED_KV.search(text):
        return True
    if _FLAG.search(text):
        return True
    if _AUTH.search(text):
        return True
    if _AWS.search(text):
        return True
    if _URLCRED.search(text):
        return True
    if _PEM.search(text):
        return True
    return False


# ---------------------------------------------------------------------------
# The hook itself
# ---------------------------------------------------------------------------

_BLOCK_MESSAGE = (
    "BLOCKED by memory-guard: the content you are attempting to persist appears "
    "to contain a secret (password, token, API key, or private key). "
    "Secrets must NOT be written to persistent memory — a secret written to memory "
    "leaks into every subsequent session's context. "
    "The value was NOT written. "
    "Store secrets in the OS keychain or a dedicated secret store, not in memory."
)


def _on_pre_tool_call(tool_name=None, args=None, **kwargs):
    """Pre-tool-call hook: block memory writes that contain secrets.

    Returns a block dict if a secret is detected, None otherwise.
    Any exception falls through to None (fail-open).
    """
    try:
        if tool_name != "memory":
            return None

        args = args or {}
        action = args.get("action", "")
        if action not in _WRITE_ACTIONS:
            return None  # remove / unknown — no new content being persisted

        content = args.get("content", "")
        if not content:
            return None  # empty content, nothing to check

        if _contains_secret(content):
            return {"action": "block", "message": _BLOCK_MESSAGE}

    except Exception:
        # Fail-open: plugin errors must never break a legitimate memory write.
        pass

    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)


# ---------------------------------------------------------------------------
# Runnable self-test:  python3 plugins/memory-guard/__init__.py
#
# Two cases:
#   (a) memory add whose content is "gmail password: hunter2"  → BLOCKED
#   (b) memory add whose content is "user prefers dark mode"   → ALLOWED
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    _PASS = "\033[32mPASS\033[0m"
    _FAIL = "\033[31mFAIL\033[0m"
    failures = 0

    # Case (a): secret content must be blocked
    result_a = _on_pre_tool_call(
        tool_name="memory",
        args={"action": "add", "target": "user", "content": "gmail password: hunter2"},
    )
    blocked_a = isinstance(result_a, dict) and result_a.get("action") == "block"
    label_a = _PASS if blocked_a else _FAIL
    if not blocked_a:
        failures += 1
    print(f"[{label_a}] (a) 'gmail password: hunter2' → BLOCKED")
    if blocked_a:
        print(f"       message preview: {result_a['message'][:120]}…")

    # Case (b): innocent content must be allowed (None returned)
    result_b = _on_pre_tool_call(
        tool_name="memory",
        args={"action": "add", "target": "user", "content": "user prefers dark mode"},
    )
    allowed_b = result_b is None
    label_b = _PASS if allowed_b else _FAIL
    if not allowed_b:
        failures += 1
    print(f"[{label_b}] (b) 'user prefers dark mode' → ALLOWED")

    # Case (c): non-memory tool is always allowed (guard doesn't fire)
    result_c = _on_pre_tool_call(
        tool_name="terminal",
        args={"command": "echo password: hunter2"},
    )
    allowed_c = result_c is None
    label_c = _PASS if allowed_c else _FAIL
    if not allowed_c:
        failures += 1
    print(f"[{label_c}] (c) terminal tool with 'password: hunter2' → ALLOWED (not memory)")

    # Case (d): memory remove action is always allowed (no new content persisted)
    result_d = _on_pre_tool_call(
        tool_name="memory",
        args={"action": "remove", "target": "user", "old_text": "gmail password: hunter2"},
    )
    allowed_d = result_d is None
    label_d = _PASS if allowed_d else _FAIL
    if not allowed_d:
        failures += 1
    print(f"[{label_d}] (d) memory remove with old_text containing secret → ALLOWED (remove, not write)")

    # Case (e): env-var secret value present in content
    os.environ["DEMO_API_TOKEN"] = "tok_live_SECRET999ABC"
    result_e = _on_pre_tool_call(
        tool_name="memory",
        args={"action": "add", "target": "memory", "content": "api key is tok_live_SECRET999ABC"},
    )
    blocked_e = isinstance(result_e, dict) and result_e.get("action") == "block"
    label_e = _PASS if blocked_e else _FAIL
    if not blocked_e:
        failures += 1
    print(f"[{label_e}] (e) env-var secret value in content → BLOCKED")

    # Case (f): AWS key in content
    result_f = _on_pre_tool_call(
        tool_name="memory",
        args={"action": "replace", "target": "memory",
              "old_text": "aws key",
              "content": "aws access key: AKIAIOSFODNN7EXAMPLE"},
    )
    blocked_f = isinstance(result_f, dict) and result_f.get("action") == "block"
    label_f = _PASS if blocked_f else _FAIL
    if not blocked_f:
        failures += 1
    print(f"[{label_f}] (f) AWS AKIA key in replace content → BLOCKED")

    print()
    if failures == 0:
        print("All cases PASS.")
    else:
        print(f"{failures} case(s) FAILED.")
        sys.exit(1)
