"""Chat via hooks: the command a bridged session runs to answer the chat (claude-code-bridge).

Copied into each channel directory (next to stop_hook.py) when ``chat_hooks`` is on, and run there by the
session through Bash: ``<python> <channel dir>/chat_send.py "<message>"``. A reply may hold several posts:
a line holding only ``---post---`` starts the next one.

- A part matching a ``refuse_patterns`` regex refuses the whole message: nothing is written, the script
  prints ``send_refused`` (with {reason} filled) and exits 1.
- Otherwise the message is written to ``outbox/`` as one item (the worker splits it into posts), the turn's
  sent count goes up by one (``chat_turn.json``), and the script prints "Sent to the chat." and exits 0.

Standalone: it reads ``chat_hooks.json`` from its own directory and imports nothing from the bridge.
It never posts to chat itself.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

RUNTIME_FILE = "chat_hooks.json"  # written by lib/session.py at launch
TURN_FILE = "chat_turn.json"  # per-turn sent and block counts; reset by lib/worker.py on a new turn
POST_SPLIT = re.compile(r"^[ \t]*---post---[ \t]*$", re.MULTILINE)
SENT_OK = "Sent to the chat."
DEBRIEF_STATE = "debrief.json"  # present while the turn is an operator debrief; written by lib/spool.py set_debrief
DEBRIEF_REFUSED = "Debrief turn: answer in your final text, not in the chat."


def _here() -> Path:
    return Path(__file__).resolve().parent


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_turn(d: Path) -> dict:
    try:
        data = json.loads((d / TURN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _refusal(hooks: dict, parts: list[str]) -> str | None:
    """The send_refused text for the first refused part, or None when every part is allowed."""
    for part in parts:
        for rule in hooks.get("refuse_patterns") or []:
            try:
                if re.search(str(rule.get("pattern", "")), part):
                    return str(hooks["send_refused"]).replace("{reason}", str(rule.get("reason", "")))
            except re.error:
                continue
    return None


def main(argv: list[str]) -> int:
    d = _here()
    if (d / DEBRIEF_STATE).exists():  # an operator debrief turn answers the operator, through the final text only
        print(DEBRIEF_REFUSED)
        return 1
    try:
        hooks = json.loads((d / RUNTIME_FILE).read_text(encoding="utf-8"))
        if not isinstance(hooks, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        print("Chat replies are not available in this session; nothing was sent.")
        return 1
    message = " ".join(argv[1:]) if len(argv) > 1 else sys.stdin.read()
    parts = [p.strip() for p in POST_SPLIT.split(message) if p.strip()]
    if not parts:
        print("Nothing was sent: the message is empty. Write the message and send it again.")
        return 2
    refused = _refusal(hooks, parts)
    if refused is not None:
        print(refused)
        return 1
    outbox = d / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = time.time_ns()
    while (outbox / f"{stamp}.json").exists():
        stamp += 1
    payload = {"session_id": None, "text": message.strip(), "ts": time.time(), "interim": True}
    _atomic_write(outbox / f"{stamp}.json", json.dumps(payload, ensure_ascii=False))
    turn = _read_turn(d)
    turn["sent"] = int(turn.get("sent") or 0) + 1
    turn["blocks"] = int(turn.get("blocks") or 0)
    _atomic_write(d / TURN_FILE, json.dumps(turn))
    print(SENT_OK)
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv)
    except Exception:
        print("Nothing was sent: the chat could not be reached from this session. Try the same message again.")
        code = 1
    sys.exit(code)
