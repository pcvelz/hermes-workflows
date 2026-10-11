"""Chat via hooks (claude-code-bridge): placeholder rendering and the per-channel files the hooks read.

The texts come from the ``chat_hooks_file`` JSON (see config.chat_hooks_texts). ``{send}`` renders as the
exact command line the session runs to answer the chat (``chat_send.py``), ``{audience}`` as the text
the file names for the readers, and ``{sender}``/``{time}`` per message. Rendering only replaces the
known placeholders, so any other braces in a text are left as written.
"""

from __future__ import annotations

import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

RUNTIME_FILE = "chat_hooks.json"  # in the channel dir; the Stop hook and chat_send.py read it
TURN_FILE = "chat_turn.json"  # per-turn state: {"sent": n, "blocks": n}; reset by the worker on a new turn
SEND_SCRIPT = "chat_send.py"  # copied into the channel dir at launch


def send_prefix(directory: Path) -> str:
    """The send command without its message: ``<python> <channel dir>/chat_send.py``."""
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(directory) / SEND_SCRIPT))}"


def send_command(directory: Path) -> str:
    """The send command line: ``<python> <channel dir>/chat_send.py "<message>"``."""
    return send_prefix(directory) + ' "<message>"'


def render(text: str, **values: Any) -> str:
    """Replace ``{key}`` for each given key; every other brace pair is left as written."""
    out = text
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def runtime_payload(hooks: dict[str, Any], directory: Path) -> dict[str, Any]:
    """What the channel dir's ``chat_hooks.json`` holds: the texts the hooks print, with {send} and {audience}
    filled in; ``{reason}`` stays for chat_send.py to fill."""
    values = {"send": send_command(directory), "audience": hooks["audience"]}
    return {
        "stop_nothing_sent": render(hooks["stop_nothing_sent"], **values),
        "send_refused": render(hooks["send_refused"], **values),
        "refuse_patterns": hooks["refuse_patterns"],
        "max_stop_blocks": hooks["max_stop_blocks"],
    }


def ask_denied_text(hooks: dict[str, Any], directory: Path) -> str:
    """The AskUserQuestion denial text with {send} and {audience} filled in."""
    return render(hooks["ask_denied"], send=send_command(directory), audience=hooks["audience"])


def prompt_text(hooks: dict[str, Any], directory: Path, item: dict[str, Any]) -> str:
    """The text injected for one chat message: the rendered ``prompt_context``, then the message text."""
    ts = item.get("ts")
    stamp = time.time() if ts is None else float(ts)
    when = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    prefix = render(
        hooks["prompt_context"],
        send=send_command(directory),
        sender=item.get("user") or "unknown",
        time=when,
    )
    return f"{prefix}\n{item.get('text', '')}"
