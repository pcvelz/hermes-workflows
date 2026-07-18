"""
tooling_intent.py — intent classification + steering-text builder for the
``tooling-steering`` Hermes plugin.

Kept as a standalone importable module (not inlined in the plugin entrypoint)
so the heuristics are unit-testable in isolation and live in ONE place — the
single source of truth for two questions:

  1. classify(text)        -> does this message involve an HTTP API / MCP setup?
  2. build_steer(kind, …)  -> what exact instruction do we inject for it?

Design notes
------------
* The classifier is intentionally a cheap regex heuristic (runs on every turn,
  must not add latency or call the model). It errs toward NOT steering: a false
  negative just means the agent falls back to its default behaviour; a false
  positive injects an unwanted tool-discipline nudge into a coding task. Direct
  coding / file / chat messages are excluded up front.
* The steering text is injected ephemerally into the user message (never the
  cached system prompt), so verbosity here costs nothing in the prompt cache.

Run ``python3 tooling_intent.py`` for a self-test over labelled samples.
"""

import re

# Messages that are clearly direct coding / file / chat requests — not an HTTP or
# MCP task even if they incidentally mention a URL or tool name. Matched on the
# leading verb of the (lowercased, stripped) message.
_ACTION_PREFIXES = (
    "fix", "write", "refactor", "debug", "implement", "add", "remove", "delete",
    "create", "build", "compile", "test", "deploy", "commit", "rename", "move",
    "edit", "patch", "summarize", "summarise", "explain", "what is", "what's",
)

# Inline code blocks / file paths / stack traces -> a coding context, not an API task.
# Only excludes statements/pastes, not questions (see _looks_like_action).
_CODE_SIGNALS = re.compile(
    r"```|\b/\w+/\w+|\b\w+\.(?:py|js|ts|tsx|sh|ya?ml|json|md|go|rs|c|cpp|java)\b"
    r"|\btraceback\b|\bstack ?trace\b|\bexception\b",
    re.I,
)

# Interrogative lead-in or trailing "?" — a question, even if it mentions a file or
# tool, should be classified on intent rather than excluded as "code".
_QUESTION = re.compile(
    r"^\s*(what|how|which|who|why|when|where|is|are|do|does|did|should|can|could|"
    r"would|will|any(?:one|body))\b|\?\s*$",
    re.I,
)

# HTTP / API / backend-call intent: curl, HTTP verbs, endpoint mentions, bearer tokens,
# API clients, REST / GraphQL, webhook, and MCP setup/installation signals.
_API_SIGNALS = re.compile(
    r"\bcurl\b"
    r"|\bhttp(?:s)?\b"
    r"|\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b"
    r"|\b(?:api|endpoint|base[_-]?url|webhook|webhook[_-]?url)\b"
    r"|\b(?:auth(?:orization|entication)?|bearer|api[_-]?key|access[_-]?token|oauth)\b"
    r"|\b(?:rest(?:ful)?|graphql|grpc|openapi|swagger)\b"
    r"|\b(?:fetch|axios|requests\.get|requests\.post|httpx|aiohttp)\b"
    r"|\b(?:response|status[_-]?code|json[_-]?body|payload)\b"
    r"|\b(?:mcp|model[_-]?context[_-]?protocol)\b"
    r"|\b(?:set\s+up|install|configure|connect)\b.{0,40}\b(?:mcp|server|integration|connector)\b"
    r"|\b(?:connected\s*accounts?|composio|smithery|zapier|make\.com|n8n)\b",
    re.I,
)


def _looks_like_action(text: str) -> bool:
    """True when the message is a direct coding/file/chat request (never steer)."""
    t = text.strip().lower()
    for p in _ACTION_PREFIXES:
        if t == p or t.startswith(p + " "):
            return True
    if not _QUESTION.search(text) and _CODE_SIGNALS.search(text):
        return True
    return False


def classify(text: str):
    """Return the steering kind for a user message, or None.

    "api"  -> the task involves an HTTP API call, curl, or MCP setup
               -> inject tool-discipline instruction.
    None   -> a direct coding / file / chat message -> no steer.
    """
    if not text or not text.strip():
        return None
    if _looks_like_action(text):
        return None
    if _API_SIGNALS.search(text):
        return "api"
    return None


def build_steer(kind: str, text: str = "") -> str:
    """Return the instruction injected (ephemerally) into the user message."""
    return (
        "[tooling-steering] This task involves an HTTP API / backend call or MCP setup. "
        "Use the `terminal` tool with curl (or the `web` tool) and run it YOURSELF — "
        "do not ask the user to paste commands or copy-paste curl examples. "
        "If a page must render JavaScript before data is accessible, use a headless browser. "
        "Never drive the user's real, visible browser (no computer_use / AppleScript). "
        "Complete the request end-to-end with your tools and report the result."
    )


# --------------------------------------------------------------------------- #
# Self-test: ``python3 tooling_intent.py`` — labelled samples, one PASS/FAIL
# per case, exits non-zero on any failure so CI / pre-deploy checks catch regressions.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    SAMPLES = [
        # POSITIVE — should classify as "api"
        ("curl the composio API and list my connected accounts", "api"),
        ("set up the gmail MCP server", "api"),
        ("hit the /connectedAccounts endpoint and show me the response", "api"),
        ("fetch https://api.example.com/users with a Bearer token", "api"),
        ("install the Smithery MCP connector", "api"),
        ("POST to the /messages endpoint with a JSON body", "api"),
        ("configure the n8n webhook URL for this integration", "api"),
        ("call the OpenAI REST API to list my models", "api"),
        # NEGATIVE — should return None
        ("write a fizzbuzz function in Python", None),
        ("summarize this file for me", None),
        ("what's the weather like today?", None),
        ("refactor the gateway config", None),
        ("debug the traceback in agent_init.py", None),
        ("hello, how are you?", None),
        ("", None),
    ]
    ok = 0
    for text, expected in SAMPLES:
        got = classify(text)
        flag = "ok " if got == expected else "FAIL"
        if got == expected:
            ok += 1
        print(f"  [{flag}] {got!s:>5}  (want {expected!s:>5})  {text[:60]!r}")
    print(f"\n{ok}/{len(SAMPLES)} classifications correct")
    raise SystemExit(0 if ok == len(SAMPLES) else 1)
