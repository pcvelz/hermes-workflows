"""Deliver a bridged reply through the Hermes platform adapter the message arrived on.

The dispatch hook hands over the live gateway and its event loop (``remember``). A reply is
then sent with that platform's own adapter, so Mattermost, Telegram or any other platform the
gateway runs uses the same path. The platform name for each channel is kept in its state.

The gateway reference only arrives with the first inbound message, but in-flight turns are
resumed at startup, before any message. Until a reference exists, a Mattermost reply is posted
through the REST API (``poster``) with the gateway's own bot credentials, so it never waits on
that first message. Other platforms with no live adapter are held and retried.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any

from . import config, poster, spool

logger = logging.getLogger("claude_code_bridge")

SEND_TIMEOUT_S = 60
DEFAULT_PLATFORM = "mattermost"
ELLIPSIS = "…"

_gateway: Any = None
_loop: asyncio.AbstractEventLoop | None = None


def remember(gateway: Any, loop: asyncio.AbstractEventLoop) -> None:
    """Keep the gateway and its event loop for replies sent from the worker thread."""
    global _gateway, _loop
    _gateway, _loop = gateway, loop


def truncate(text: str, max_chars: int) -> str:
    """Cut ``text`` to at most ``max_chars`` characters, ending with an ellipsis when cut.

    The cut is placed at the last newline or space before the limit when there is one.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[: max_chars - len(ELLIPSIS)]
    cut = max(head.rfind("\n"), head.rfind(" "))
    if cut > 0:
        head = head[:cut]
    return head.rstrip() + ELLIPSIS


def _adapter(platform: str) -> Any:
    adapters = getattr(_gateway, "adapters", None) or {}
    return next((a for p, a in adapters.items() if str(getattr(p, "value", p)) == platform), None)


def send(cfg: dict[str, Any], cid: str, root: Any, text: str) -> bool:
    """Post ``text`` to channel ``cid`` (threaded under ``root`` when given) on its platform.

    Returns True when the adapter reports success, False on a failed send, and None when no
    adapter is live yet for a platform without a REST fallback (a wait, not a failure: the worker
    holds the reply without using an attempt). A Mattermost reply with no live adapter is posted
    through the REST API instead. Empty text is never sent. Errors are logged and reported as
    False, so the worker keeps the reply in the outbox and retries.
    """
    body = (text or "").strip()
    if not body:
        return False
    # A channel fed by a watcher has no recorded platform: Mattermost is the only one with a REST fallback,
    # so an unrecorded platform is treated as Mattermost rather than held until the wait budget parks it.
    platform = spool.load_state(cfg, cid).get("platform") or DEFAULT_PLATFORM
    adapter = _adapter(platform)
    max_chars = int(config.for_channel(cfg, cid).get("max_reply_chars", 15000))
    # reply_where_written: the adapter sends reply_to as a thread root only in its own "thread" reply mode,
    # so a Mattermost reply that must land in a thread goes through the REST API, which always sends the root.
    if root and platform == "mattermost" and config.for_channel(cfg, cid).get("reply_where_written"):
        return poster.post(cid, str(root), body, max_chars)
    if adapter is None or _loop is None:
        if platform == "mattermost":
            logger.info("claude-code-bridge: no live adapter yet; posting on channel %s via REST", cid)
            return poster.post(cid, str(root) if root else None, body, max_chars)
        logger.debug("claude-code-bridge: no live adapter for platform %r yet; reply held", platform)
        return None
    body = truncate(body, max_chars)
    try:
        future = asyncio.run_coroutine_threadsafe(
            adapter.send(cid, body, reply_to=str(root) if root else None), _loop
        )
        result = future.result(timeout=SEND_TIMEOUT_S)
    except (concurrent.futures.TimeoutError, Exception) as exc:  # fail-open: never raise into the gateway
        logger.warning("claude-code-bridge: send failed on channel %s (%s)", cid, type(exc).__name__)
        return False
    # SendResult is an object, not a bool: a failed send is success=False with an error set.
    if not getattr(result, "success", False):
        logger.warning("claude-code-bridge: adapter reported a failed send on channel %s (%s)",
                       cid, getattr(result, "error", None))
        return False
    return True
