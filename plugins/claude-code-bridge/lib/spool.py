"""File-based spool for claude-code-bridge: per-channel inbox/outbox folders and state.json.

Every write is atomic: the payload goes to a temp file in the same directory and is
moved into place with ``os.replace``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger("claude_code_bridge")

_CID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

STATE_DEFAULTS: dict[str, Any] = {
    "session_id": None,
    "started_once": False,
    "inflight": None,
    "inflight_since": None,
    "last_activity": 0,
}


def _check_cid(cid: str) -> str:
    if not isinstance(cid, str) or not _CID_RE.fullmatch(cid):
        raise ValueError("invalid channel id")
    return cid


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _unique_json_path(directory: Path) -> Path:
    stamp = time.time_ns()
    while (directory / f"{stamp}.json").exists():
        stamp += 1
    return directory / f"{stamp}.json"


def channel_dir(cfg: dict[str, Any], cid: str) -> Path:
    """``home/channels/<cid>`` with ``inbox/`` and ``outbox/`` created. Invalid cid -> ValueError."""
    _check_cid(cid)
    directory = config.home(cfg) / "channels" / cid
    (directory / "inbox").mkdir(parents=True, exist_ok=True)
    (directory / "outbox").mkdir(parents=True, exist_ok=True)
    return directory


def enqueue(cfg: dict[str, Any], cid: str, item: dict[str, Any], name: str | None = None) -> Path:
    """Write ``item`` to ``inbox/<time_ns>.json`` atomically and return its path.

    ``name`` (a file stem, e.g. the ``time_ns`` of a popped item) keeps the item's
    original queue position; if that name is taken, a fresh name is used instead.
    """
    inbox = channel_dir(cfg, cid) / "inbox"
    path = inbox / f"{name}.json" if name else _unique_json_path(inbox)
    if path.exists():
        path = _unique_json_path(inbox)
    _atomic_write(path, json.dumps(item, ensure_ascii=False))
    return path


def pending(cfg: dict[str, Any], cid: str) -> list[Path]:
    """Inbox files, oldest first."""
    inbox = channel_dir(cfg, cid) / "inbox"
    return sorted(inbox.glob("*.json"), key=lambda p: (len(p.stem), p.stem))


def pop(path: Path) -> dict[str, Any]:
    """Read a spool file, unlink it, and return its JSON payload."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    return data


def channels(cfg: dict[str, Any]) -> list[str]:
    """Names of existing channel directories."""
    root = config.home(cfg) / "channels"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def load_state(cfg: dict[str, Any], cid: str) -> dict[str, Any]:
    """``state.json`` merged over STATE_DEFAULTS. Missing or unreadable -> defaults."""
    state = copy.deepcopy(STATE_DEFAULTS)
    path = channel_dir(cfg, cid) / "state.json"
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("claude-code-bridge: unreadable state for channel (%s); using defaults", type(exc).__name__)
            loaded = None
        if isinstance(loaded, dict):
            state.update(loaded)
    return state


def save_state(cfg: dict[str, Any], cid: str, state: dict[str, Any]) -> None:
    """Write ``state.json`` atomically."""
    path = channel_dir(cfg, cid) / "state.json"
    _atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2))


def put_reply(cfg: dict[str, Any], cid: str, reply: dict[str, Any]) -> Path:
    """Write ``reply`` back to ``outbox/`` so a failed post is retried on a later tick."""
    path = _unique_json_path(channel_dir(cfg, cid) / "outbox")
    _atomic_write(path, json.dumps(reply, ensure_ascii=False))
    return path


def take_reply(cfg: dict[str, Any], cid: str) -> dict[str, Any] | None:
    """Pop the oldest ``outbox/*.json`` reply, or return None when there is none."""
    outbox = channel_dir(cfg, cid) / "outbox"
    for path in sorted(outbox.glob("*.json"), key=lambda p: (len(p.stem), p.stem)):
        try:
            return pop(path)
        except FileNotFoundError:
            continue
    return None
