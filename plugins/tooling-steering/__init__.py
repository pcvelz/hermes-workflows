"""tooling-steering — deterministic tool-discipline enforcement for the Hermes agent.

Problem
-------
When a user asks the agent to make an HTTP / API call or set up an MCP integration,
the agent sometimes asks the user to paste the curl command or copy-paste credentials
instead of running the call itself via the ``terminal`` tool. This is a repeating
failure mode that a persona line alone does not reliably prevent on a small local model.

Fix
---
Make the steering deterministic. The ``pre_llm_call`` hook fires once per turn, before
the tool-calling loop, and a callback returning ``{"context": ...}`` injects that text
into the CURRENT turn's user message — the freshest, most-followed position in the
prompt, and cache-safe (it never touches the cached system-prompt prefix; it is
ephemeral and never persisted to the session DB). This is the Hermes equivalent of a
Claude Code ``UserPromptSubmit`` context-injection hook.

So: on every turn we classify the user's message; if it signals an HTTP API call, curl
usage, endpoint access, or MCP setup, we inject a concrete tool-discipline instruction.
Coding / file / chat messages are left untouched.

Structure
---------
* ``lib/tooling_intent.py`` — classifier + steering-text (single source of truth,
  self-tests via ``python3 lib/tooling_intent.py``).
* this file — thin wiring only: ``register(ctx)`` + the hook callback.
"""

import os
import sys

# The plugin is import-loaded by Hermes under an opaque module name, so relative
# imports won't resolve — put our own lib/ on sys.path and import absolutely.
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import tooling_intent  # noqa: E402  (path set above)


def _on_pre_llm_call(**kwargs):
    """``pre_llm_call`` callback. Inject tool-discipline for API / MCP tasks.

    Kwargs supplied by ``agent/conversation_loop.py``: ``session_id``,
    ``user_message``, ``conversation_history``, ``is_first_turn``, ``model``,
    ``platform``, ``sender_id``. We only need ``user_message``.

    Returns ``{"context": <instruction>}`` to inject into the current turn's user
    message, or ``None`` to leave the turn untouched. Must never raise — the hook
    dispatcher swallows exceptions, but returning cleanly keeps the logs quiet.
    """
    try:
        text = kwargs.get("user_message") or ""
        kind = tooling_intent.classify(text)
        if not kind:
            return None
        return {"context": tooling_intent.build_steer(kind, text)}
    except Exception:
        return None


def register(ctx) -> None:
    """Plugin entrypoint — register the per-turn steering hook."""
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
