"""skill-router — force a matching skill's directive to the model on a small local LLM.

Hermes shows the model only ``<available_skills>`` (name + truncated description);
the full SKILL.md loads opt-in via ``skill_view()``, which a small local model
reliably does NOT call. So a skill's must-follow guidance never reaches the model.
This plugin fixes that with a ``pre_llm_call`` hook that injects the matched skill's
``router_directive`` into the CURRENT turn's user message (freshest slot, cache-safe,
ephemeral).

Thin wiring only — the matcher + directive logic live in ``lib/skill_match.py``
(single source of truth, self-tested via ``python3 lib/skill_match.py``).
"""

import os
import sys

# Import-loaded by Hermes under an opaque module name → put our lib/ on sys.path.
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import skill_match  # noqa: E402  (path set above)


def _on_pre_llm_call(**kwargs):
    """Inject the matched skill's router_directive, or leave the turn untouched.

    Kwargs from ``agent/conversation_loop.py``: ``user_message`` (+ session_id,
    ``conversation_history`` — a ``list(messages)`` of ``{"role","content"}`` dicts,
    the current turn's user message already appended as the last element —
    ``is_first_turn``, model, platform, sender_id). Must never raise.

    The match falls back to ``conversation_history`` so a TOPICLESS FOLLOW-UP
    ("test again", "where are we?") still re-injects the directive of the skill
    the conversation was already about — Hermes reloads that history each turn,
    so it survives the fresh-process-per-turn lifecycle (a module-level sticky
    dict would always be empty here).
    """
    try:
        sk, via = skill_match.match_with_history(
            kwargs.get("user_message") or "",
            kwargs.get("conversation_history") or [],
        )
        if not sk:
            return None
        steer = skill_match.build_steer(sk, via)
        # Let a skill's directive reference the poster's id (e.g. a dispatch skill
        # can use --sender "{sender_id}" to pick the right team/requester reliably).
        steer = steer.replace("{sender_id}", str(kwargs.get("sender_id") or ""))
        return {"context": steer}
    except Exception:
        return None


def register(ctx) -> None:
    """Plugin entrypoint — register the per-turn skill-routing hook."""
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
