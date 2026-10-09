"""claude-code-bridge worker: one in-flight message per channel, FIFO for the rest.

``tick`` is one pass over every channel: collect the reply of the in-flight turn,
enforce the wait budget, start the session when work is queued, inject the next
message when the session is idle, and stop idle sessions. All session work goes
through the ``session`` module so tests can replace it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable

from . import session, spool

logger = logging.getLogger("claude_code_bridge")

WAIT_FAILURE_TEXT = "Sorry, I could not answer this one."

_thread_lock = threading.Lock()
_thread: threading.Thread | None = None


def format_message(item: dict[str, Any]) -> str:
    """Prefix the text with its local time and sender: ``[YYYY-MM-DD HH:MM · user] text``.

    Uses the item's epoch ``ts``; falls back to now when it is missing.
    """
    ts = item.get("ts")
    stamp = time.time() if ts is None else float(ts)
    when = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    return f"[{when} · {item.get('user') or 'unknown'}] {item.get('text', '')}"


def _reply_root(cfg: dict[str, Any], inflight: dict[str, Any]) -> str | None:
    """Where a reply goes: the user's own thread if they wrote in one; the user's post only
    when ``reply_in_thread`` is set; otherwise top-level (None)."""
    if inflight.get("root_id"):
        return inflight["root_id"]
    if cfg.get("reply_in_thread") and inflight.get("post_id"):
        return inflight["post_id"]
    return None


def _apply_reset(cfg: dict[str, Any], cid: str, now: float) -> None:
    """Handle queued ``/reset`` control items before any chat message on the channel.

    Stops the session, drops the inbox up to and including the last reset (messages sent
    after it are kept for the new session), clears the outbox and resets the session state
    so the next message starts a NEW Claude Code conversation. Posts nothing to chat.
    """
    paths = spool.pending(cfg, cid)
    last = None
    for i, path in enumerate(paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("control") == "reset":
            last = i
    if last is None:
        return
    session.stop(cfg, cid)
    for path in paths[: last + 1]:
        path.unlink(missing_ok=True)
    for path in (spool.channel_dir(cfg, cid) / "outbox").glob("*.json"):
        path.unlink(missing_ok=True)
    state = spool.load_state(cfg, cid)
    state.update(session_id=None, started_once=False, inflight=None, inflight_since=None, last_activity=now)
    spool.save_state(cfg, cid, state)
    logger.info("claude-code-bridge: reset session on channel %s", cid)


MAX_REPLY_POST_ATTEMPTS = 5


def _post_reply(post_fn: Callable[[str, Any, str], Any], cid: str, thread_root: Any, text: str) -> bool:
    """Post a reply; a raised exception or a falsy return value counts as a failed post."""
    try:
        ok = post_fn(cid, thread_root, text)
    except Exception as exc:  # the reply must survive a crashing poster
        logger.warning("claude-code-bridge: reply post raised %s on channel %s", type(exc).__name__, cid)
        return False
    if not ok:
        logger.warning("claude-code-bridge: reply post failed on channel %s", cid)
    return bool(ok)


def _start_session(cfg: dict[str, Any], cid: str, state: dict[str, Any], now: float) -> bool:
    """Start the session and record the launch fingerprint it was started with. Saves state."""
    started = session.start(cfg, cid, state)
    if started:
        state["launch_fingerprint"] = session.fingerprint(cfg, cid)
        state["last_activity"] = now
    spool.save_state(cfg, cid, state)
    if not started:
        logger.warning("claude-code-bridge: session start failed on channel %s; will retry", cid)
    return started


def _tick_channel(cfg: dict[str, Any], cid: str, post_fn: Callable[[str, Any, str], Any], now: float) -> None:
    _apply_reset(cfg, cid, now)
    state = spool.load_state(cfg, cid)
    inflight = state.get("inflight")
    budget_s = float(cfg.get("wait_budget_minutes", 360)) * 60

    # 1-3: collect the reply of the in-flight turn, or enforce the wait budget.
    reply = spool.take_reply(cfg, cid)
    if inflight:
        thread_root = _reply_root(cfg, inflight)
        if reply is not None:
            if _post_reply(post_fn, cid, thread_root, reply.get("text", "")):
                state["inflight"] = None
                state["inflight_since"] = None
                state["last_activity"] = now
            else:
                attempts = int(reply.get("post_attempts") or 0) + 1
                if attempts < MAX_REPLY_POST_ATTEMPTS:
                    reply["post_attempts"] = attempts
                    spool.put_reply(cfg, cid, reply)  # kept in the outbox; retried on the next tick
                    logger.warning("claude-code-bridge: will retry reply on channel %s (attempt %d of %d)",
                                   cid, attempts + 1, MAX_REPLY_POST_ATTEMPTS)
                else:
                    logger.error("claude-code-bridge: reply dropped on channel %s after %d failed posts",
                                 cid, attempts)
                    state["inflight"] = None
                    state["inflight_since"] = None
                    state["last_activity"] = now
        elif now - float(state.get("inflight_since") or 0) > budget_s:
            post_fn(cid, thread_root, WAIT_FAILURE_TEXT)
            state["inflight"] = None
            state["inflight_since"] = None
    elif reply is not None:
        logger.info("claude-code-bridge: dropped a reply with no in-flight message on channel %s", cid)

    # 4: nothing in flight -> start the session if needed, then inject the next message.
    if not state.get("inflight"):
        queued = spool.pending(cfg, cid)
        if queued:
            if not session.exists(cfg, cid):
                if state.get("session_id") is None:
                    state["session_id"] = str(uuid.uuid4())
                if not _start_session(cfg, cid, state, now):
                    return
            elif session.fingerprint(cfg, cid) != state.get("launch_fingerprint"):
                # Launch settings changed since this session started (or are unknown): restart it
                # with --resume so the conversation continues under the new settings.
                logger.info("claude-code-bridge: launch config changed; restarting session (context kept) on channel %s", cid)
                session.stop(cfg, cid)
                state["started_once"] = True
                if not _start_session(cfg, cid, state, now):
                    return
            if session.is_idle(cfg, cid):
                path = queued[0]
                item = spool.pop(path)
                if session.inject(cfg, cid, format_message(item)):
                    state["inflight"] = item
                    state["inflight_since"] = now
                    state["last_activity"] = now
                else:
                    spool.enqueue(cfg, cid, item, name=path.stem)
                    logger.warning("claude-code-bridge: inject failed on channel %s; message re-queued", cid)

        # 5: idle exit once nothing is queued or in flight.
        elif (
            session.exists(cfg, cid)
            and now - float(state.get("last_activity") or 0) > float(cfg.get("idle_exit_minutes", 60)) * 60
        ):
            session.stop(cfg, cid)
            logger.info("claude-code-bridge: stopped idle session on channel %s", cid)

    # 6: persist.
    spool.save_state(cfg, cid, state)


def tick(cfg: dict[str, Any], post_fn: Callable[[str, Any, str], Any], now: float | None = None) -> None:
    """One worker pass over every channel. Each channel is isolated; errors are logged."""
    current = time.time() if now is None else now
    for cid in spool.channels(cfg):
        try:
            _tick_channel(cfg, cid, post_fn, current)
        except Exception:
            logger.exception("claude-code-bridge: worker error on channel %s", cid)


def start_thread(get_cfg: Callable[[], dict[str, Any]], post_fn: Callable[[str, Any, str], Any]) -> threading.Thread:
    """Start the daemon worker loop once; later calls return the running thread."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread

        def _loop() -> None:
            while True:
                poll = 2.0
                try:
                    cfg = get_cfg()
                    poll = float(cfg.get("poll_seconds", 2))
                    tick(cfg, post_fn)
                except Exception:
                    logger.exception("claude-code-bridge: worker loop error")
                time.sleep(poll)

        _thread = threading.Thread(target=_loop, name="claude-code-bridge-worker", daemon=True)
        _thread.start()
        return _thread
