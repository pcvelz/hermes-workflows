"""Post a bridged reply to a Mattermost thread through the REST API.

Credentials come from the gateway process environment (``MATTERMOST_URL``,
``MATTERMOST_TOKEN``). The token is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger("claude_code_bridge")

TIMEOUT_SECONDS = 20
ELLIPSIS = "…"


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


def post(channel_id: str, root_id: str | None, text: str, max_chars: int) -> bool:
    """Create a post in ``channel_id`` (threaded under ``root_id`` when given).

    Returns True on a 2xx response. Empty or whitespace-only text is never posted.
    Network and configuration errors are logged and reported as False.
    """
    body_text = (text or "").strip()
    if not body_text:
        return False
    base_url = os.environ.get("MATTERMOST_URL", "").rstrip("/")
    token = os.environ.get("MATTERMOST_TOKEN", "")
    if not base_url or not token:
        logger.warning("claude-code-bridge: Mattermost credentials missing; reply not posted")
        return False

    payload: dict[str, Any] = {
        "channel_id": channel_id,
        "message": truncate(body_text, max_chars),
    }
    if root_id:
        payload["root_id"] = root_id

    request = urllib.request.Request(
        f"{base_url}/api/v4/posts",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
    except Exception as exc:  # fail-open: never raise into the gateway
        logger.warning("claude-code-bridge: post failed (%s)", type(exc).__name__)
        return False
    if 200 <= status < 300:
        return True
    logger.warning("claude-code-bridge: post returned HTTP %s", status)
    return False
