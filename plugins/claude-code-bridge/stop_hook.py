"""Claude Code Stop hook for claude-code-bridge: the reply guard, and the hand-off of the final reply.

Copied into each channel directory and run from there by Claude Code once per turn, when the turn
ends. It reads the hook JSON from stdin, then does one of two things:

- Blocks the stop when the final reply matches the channel's reply guard (``reply_guard.json``,
  written at launch) and this turn has fewer nudges than ``max_nudges``. It prints
  ``{"decision": "block", "reason": ...}`` on stdout. Claude Code then continues the same turn with
  the reason as feedback. Nothing is written to the outbox; the nudge count (``reply_guard_state.json``)
  goes up by one and one line is appended to ``reply_guard.log``.
- Otherwise writes ``outbox/<time_ns>.json`` next to itself, which the gateway worker posts, and
  resets the nudge count. A match past ``max_nudges`` is delivered with a WARNING line in the log.

It never posts to chat. Any failure of the guard delivers the reply rather than blocking, and the
script always exits 0.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

GUARD_CONFIG = "reply_guard.json"  # written by lib/session.py at launch
GUARD_STATE = "reply_guard_state.json"  # per-turn nudge count; reset by lib/spool.py on a new turn
GUARD_LOG = "reply_guard.log"
DEFAULT_NUDGE = (
    "Your reply was not sent: it gives up instead of delivering. Do not apologise or explain. "
    "Continue the task now and post the best result you can in the required format."
)


def _here() -> Path:
    return Path(__file__).resolve().parent


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _log(d: Path, line: str) -> None:
    try:
        with (d / GUARD_LOG).open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def _load_guard(d: Path) -> dict | None:
    """The guard as ``{"patterns": [(text, compiled)], "nudge": str, "max_nudges": int}``, or None when off."""
    try:
        raw = json.loads((d / GUARD_CONFIG).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _log(d, "WARNING reply_guard: reply_guard.json unreadable; replies are delivered without the guard")
        return None
    if not isinstance(raw, dict):
        _log(d, "WARNING reply_guard: reply_guard.json is not an object; replies are delivered without the guard")
        return None
    patterns = []
    for text in raw.get("patterns") or []:
        if isinstance(text, str):
            try:
                patterns.append((text, re.compile(text)))
            except re.error:
                pass
    if not patterns:
        return None
    try:
        max_nudges = max(0, int(raw.get("max_nudges")))
    except (TypeError, ValueError):
        max_nudges = 2
    nudge = raw.get("nudge")
    if not (isinstance(nudge, str) and nudge.strip()):
        nudge = DEFAULT_NUDGE
    return {"patterns": patterns, "nudge": nudge, "max_nudges": max_nudges}


def _read_count(d: Path) -> int:
    try:
        return max(0, int(json.loads((d / GUARD_STATE).read_text(encoding="utf-8")).get("nudges") or 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def _write_count(d: Path, n: int) -> bool:
    try:
        _atomic_write(d / GUARD_STATE, json.dumps({"nudges": n}))
        return True
    except OSError:
        return False


def _reason(d: Path, text: str, guard: dict) -> str | None:
    """The block reason for ``text``, or None to deliver the reply. Records a nudge only when it blocks."""
    hit = None
    for pattern, rx in guard["patterns"]:
        found = rx.search(text)
        if found:
            hit = (pattern, found.group(0))
            break
    if hit is None:
        return None
    count = _read_count(d)
    if count >= guard["max_nudges"]:
        _log(d, f"WARNING reply_guard: gave up after {count} nudges (pattern {hit[0]}); delivering the latest reply")
        return None
    if not _write_count(d, count + 1):
        _log(d, "WARNING reply_guard: nudge could not be recorded; delivering the reply")
        return None
    _log(d, f"INFO reply_guard: blocked a give-up reply (pattern {hit[0]}); nudge {count + 1} of {guard['max_nudges']}")
    return f"{guard['nudge']}\nMatched phrase: \"{hit[1]}\""


def _guard_reason(d: Path, text: str) -> str | None:
    """The block reason, or None (guard off, clean reply, cap reached, or any failure: deliver)."""
    try:
        guard = _load_guard(d)
        if guard is None:
            return None
        reason = _reason(d, text, guard)
        if reason is None:
            _write_count(d, 0)  # a delivered reply ends the turn's nudges
        return reason
    except Exception:
        return None


def _deliver(d: Path, data: dict, text: str, turn_end: bool = False) -> None:
    outbox = d / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": data.get("session_id"),
        "text": text,
        "ts": time.time(),
    }
    if turn_end:
        payload["turn_end"] = True  # chat via hooks: the turn is over, nothing new to post
    stamp = time.time_ns()
    while (outbox / f"{stamp}.json").exists():
        stamp += 1
    final = outbox / f"{stamp}.json"
    tmp = outbox / f".{final.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)


RUNTIME_FILE = "chat_hooks.json"  # chat via hooks, written by lib/session.py at launch when the channel has it on
TURN_FILE = "chat_turn.json"  # per-turn {"sent": n, "blocks": n}; the send script counts sent, this hook counts blocks
DEBRIEF_STATE = "debrief.json"  # present while the turn is an operator debrief; written by lib/spool.py set_debrief


def _load_chat(d: Path) -> dict | None:
    """The chat-hooks runtime file as a dict, or None when chat via hooks is off for the channel.

    An unreadable file is logged and treated as off: the turn then follows the plain path (delivery).
    """
    try:
        raw = json.loads((d / RUNTIME_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _log(d, "WARNING chat_hooks: chat_hooks.json unreadable; the reply is delivered as is")
        return None
    if not isinstance(raw, dict):
        _log(d, "WARNING chat_hooks: chat_hooks.json is not an object; the reply is delivered as is")
        return None
    return raw


def _read_turn(d: Path) -> dict:
    try:
        data = json.loads((d / TURN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _chat_stop(d: Path, data: dict, text: str, hooks: dict) -> str | None:
    """Chat via hooks at the end of a turn. Returns the block reason, or None when the turn ends.

    A message was sent this turn: end the turn and post nothing (a turn-end item keeps the worker's
    in-flight bookkeeping right). Nothing sent: block with the stop text until ``max_stop_blocks``; after that the
    final text is delivered as a fallback, with a WARNING, so a turn is never lost.
    """
    turn = _read_turn(d)
    sent = int(turn.get("sent") or 0)
    blocks = int(turn.get("blocks") or 0)
    if sent > 0:
        _deliver(d, data, "", turn_end=True)
        return None
    cap = int(hooks.get("max_stop_blocks") or 0)
    stop_text = str(hooks.get("stop_nothing_sent") or "")
    if blocks < cap and stop_text:
        try:
            _atomic_write(d / TURN_FILE, json.dumps({"sent": 0, "blocks": blocks + 1}))
        except OSError:
            _log(d, "WARNING chat_hooks: block could not be recorded; the reply is delivered")
            _deliver(d, data, text)
            return None
        _log(d, f"INFO chat_hooks: turn ended with nothing sent; block {blocks + 1} of {cap}")
        return stop_text
    _log(d, f"WARNING chat_hooks: nothing sent after {blocks} blocks; delivering the final text")
    _deliver(d, data, text)
    return None


def main() -> None:
    raw = sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        return
    text = data.get("last_assistant_message") or ""
    text = text if isinstance(text, str) else str(text)
    d = _here()
    if (d / DEBRIEF_STATE).exists():
        # An operator debrief turn: the final text is the answer for the operator (the worker routes it there).
        # No chat block and no reply guard: the turn ends normally.
        _deliver(d, data, text)
        return
    hooks = _load_chat(d)
    if hooks is not None:
        reason = _chat_stop(d, data, text, hooks)
        if reason is not None:
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
        return
    reason = _guard_reason(d, text)
    if reason is not None:
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
        return
    _deliver(d, data, text)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
