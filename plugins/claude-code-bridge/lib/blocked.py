"""Blocked-prompt detection for a bridged Claude Code pane (claude-code-bridge).

A bridged session is unattended: a dialog in its pane (a repo hook's confirmation, a permission
prompt) waits forever unless something answers it. ``detect`` reads the bottom of a captured pane and
says whether a modal with a numbered option block is open. ``exempt_reason`` names the dialogs that
must never be answered automatically (rewind, model choice, exit confirmation, AskUserQuestion).
``mode`` reads the channel's ``blocked_prompt`` setting. Pure functions; no I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

logger = logging.getLogger("claude_code_bridge")

MODES = ("off", "notify", "decline", "accept")
DEFAULT_MODE = "off"
TAIL_LINES = 60
SELECTOR = "❯"

_MODAL_RE = re.compile(r"Enter to select|Do you want to (?:proceed|continue|make|create|run|allow)|Would you like to proceed")
_QUESTION_RE = re.compile(r"Do you want to (?:proceed|continue|make|create|run|allow)|Would you like to proceed")
_OPTION_RE = re.compile(r"^\s*(?:❯\s*)?\d+\.\s+\S")
_REWIND_TITLE_RE = re.compile(r"^\s*Rewind\s*$")
_REWIND_WARNING = "Rewinding does not affect files edited manually or via bash"
_REWIND_OPTION_RE = re.compile(r"^[^0-9]*[0-9]+\.\s+(?:Restore|Summarize)(?:\s|$)")
_AUQ_FOOTER_RE = re.compile(r"^\s*Enter to select")

_warned_modes: set[str] = set()


def mode(eff: dict[str, Any]) -> str:
    """The channel's effective ``blocked_prompt`` mode: off, notify, decline or accept.

    Accepts the mapping form (``{mode: ...}``) and the bare string form. Unset or unknown values are
    ``off``; an unknown value is logged once.
    """
    raw = eff.get("blocked_prompt")
    value = raw.get("mode") if isinstance(raw, dict) else raw
    text = str(value or DEFAULT_MODE).strip().lower()
    if text in MODES:
        return text
    if text not in _warned_modes:
        _warned_modes.add(text)
        logger.warning("claude-code-bridge: blocked_prompt mode %r is not one of %s; treated as off",
                       text, ", ".join(MODES))
    return DEFAULT_MODE


def exempt_reason(lines: list[str]) -> str | None:
    """Why this dialog must never be answered automatically, or None.

    Exempt patterns: rewind (title or warning plus at least two
    Restore/Summarize options), the /model selection, the exit confirmation while background work runs,
    and AskUserQuestion menus (their footer opens with "Enter to select").
    """
    text = "\n".join(lines)
    if any(_REWIND_TITLE_RE.match(ln) for ln in lines) or _REWIND_WARNING in text:
        if sum(1 for ln in lines if _REWIND_OPTION_RE.match(ln)) >= 2:
            return "rewind"
    if "Select model" in text and "Switch between Claude models" in text:
        return "model selection"
    if "Background work is running" in text and "The following will stop when you exit" in text:
        return "exit confirmation"
    if any(_AUQ_FOOTER_RE.match(ln) for ln in lines):
        return "AskUserQuestion"
    return None


def detect(pane: Any) -> dict[str, Any] | None:
    """The blocked dialog in the bottom ``TAIL_LINES`` of ``pane``, or None.

    A dialog is a modal (a question line, the "Enter to select" footer, or an exempt dialog's marker)
    plus at least two numbered option rows and a ``❯`` selector line. The idle footer does not veto it.
    Returns ``{"sig", "question", "options", "exempt"}``; ``sig`` is a hash of the question and the
    options, so the same dialog keeps the same signature across ticks.
    """
    lines = (pane if isinstance(pane, str) else "").splitlines()[-TAIL_LINES:]
    exempt = exempt_reason(lines)
    if exempt is None and not any(_MODAL_RE.search(ln) for ln in lines):
        return None
    options = [ln.strip() for ln in lines if _OPTION_RE.match(ln)]
    if len(options) < 2 or not any(SELECTOR in ln for ln in lines):
        return None
    questions = [ln.strip() for ln in lines if _QUESTION_RE.search(ln)]
    if not questions:
        questions = [ln.strip() for ln in lines if _MODAL_RE.search(ln)]
    if not questions:  # exempt dialogs such as "Select model" have a title line instead
        questions = [next(ln.strip() for ln in lines if ln.strip())]
    payload = json.dumps({"q": questions, "o": options}, ensure_ascii=False)
    return {
        "sig": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "question": questions[0],
        "options": options,
        "exempt": exempt,
    }
