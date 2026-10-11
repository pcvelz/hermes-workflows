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
_channel_names: dict[str, str] = {}  # successful lookups only: a channel's name rarely changes


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


POSTS_PAGE_SIZE = 200
POSTS_MAX_PAGES = 100  # a bound on one listing: 20 000 posts


def channel_posts_since(channel_id: str, since_ms: int) -> list[dict[str, Any]] | None:
    """Posts of a channel created at or after ``since_ms`` (epoch ms), newest first.

    Pages through the channel newest-first (``page``/``per_page``) and stops at the first page that reaches an
    older post or runs out. Returns None on a missing credential or a failed request (logged, not retried).
    """
    base_url = os.environ.get("MATTERMOST_URL", "").rstrip("/")
    token = os.environ.get("MATTERMOST_TOKEN", "")
    if not base_url or not token:
        logger.warning("claude-code-bridge: Mattermost credentials missing; channel not listed")
        return None
    out: list[dict[str, Any]] = []
    for page in range(POSTS_MAX_PAGES):
        request = urllib.request.Request(
            f"{base_url}/api/v4/channels/{channel_id}/posts?page={page}&per_page={POSTS_PAGE_SIZE}",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # fail-open: the caller deletes nothing
            logger.warning("claude-code-bridge: channel listing failed (%s)", type(exc).__name__)
            return None
        order = data.get("order") if isinstance(data, dict) else None
        posts = data.get("posts") if isinstance(data, dict) else None
        if not order or not isinstance(posts, dict):
            break
        reached_older = False
        for post_id in order:
            post = posts.get(post_id)
            if not isinstance(post, dict):
                continue
            if int(post.get("create_at") or 0) < since_ms:
                reached_older = True
                continue
            out.append(post)
        if reached_older or len(order) < POSTS_PAGE_SIZE:
            break
    return out


def delete_post(post_id: str) -> bool:
    """Delete one post (the operator's own ``!debrief`` command) through the REST API. Best-effort: True on a 2xx
    response; a missing credential, network or HTTP error is logged and reported as False. Never retried."""
    if not post_id:
        return False
    base_url = os.environ.get("MATTERMOST_URL", "").rstrip("/")
    token = os.environ.get("MATTERMOST_TOKEN", "")
    if not base_url or not token:
        logger.warning("claude-code-bridge: Mattermost credentials missing; post not deleted")
        return False
    request = urllib.request.Request(
        f"{base_url}/api/v4/posts/{post_id}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
    except Exception as exc:  # fail-open: the debrief still runs
        logger.warning("claude-code-bridge: delete failed (%s)", type(exc).__name__)
        return False
    return 200 <= status < 300


_team_ids: dict[str, str] = {}  # successful lookups only: a channel never moves to another team


def channel_team(channel_id: str) -> str | None:
    """The team id of a Mattermost channel (``team_id`` from GET /api/v4/channels/{id}), or None.

    A direct message has no team and reads as None. Successful lookups are cached; a failed lookup is logged
    and reported as None (the caller treats it as "not known to be the same team").
    """
    if channel_id in _team_ids:
        return _team_ids[channel_id]
    base_url = os.environ.get("MATTERMOST_URL", "").rstrip("/")
    token = os.environ.get("MATTERMOST_TOKEN", "")
    if not base_url or not token:
        logger.warning("claude-code-bridge: Mattermost credentials missing; team unknown")
        return None
    request = urllib.request.Request(
        f"{base_url}/api/v4/channels/{channel_id}",
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # fail-closed: the caller does not post
        logger.warning("claude-code-bridge: channel team lookup failed (%s)", type(exc).__name__)
        return None
    team = data.get("team_id") if isinstance(data, dict) else None
    if not team:
        return None
    _team_ids[channel_id] = str(team)
    return _team_ids[channel_id]


def same_team(channel_id: str, other_id: str) -> bool:
    """True only when both channels are known to be in the same team. Otherwise False, with a WARNING that names
    no channel content (the team boundary: a bridged channel's content never leaves its team)."""
    mine = channel_team(channel_id)
    theirs = channel_team(other_id)
    if not mine or not theirs:
        logger.warning("claude-code-bridge: operator post not sent: the team of a channel is unknown (fail closed)")
        return False
    if mine != theirs:
        logger.warning("claude-code-bridge: operator post not sent: operator_channel is in another team")
        return False
    return True


def channel_name(channel_id: str) -> str | None:
    """Return the Mattermost channel's internal name (e.g. ``inv-topic-ab12``), or None.

    Successful lookups are cached for the life of the process. Missing credentials, network or
    HTTP errors are logged and reported as None; the caller decides what None means.
    """
    if channel_id in _channel_names:
        return _channel_names[channel_id]
    base_url = os.environ.get("MATTERMOST_URL", "").rstrip("/")
    token = os.environ.get("MATTERMOST_TOKEN", "")
    if not base_url or not token:
        logger.warning("claude-code-bridge: Mattermost credentials missing; channel name unknown")
        return None
    request = urllib.request.Request(
        f"{base_url}/api/v4/channels/{channel_id}",
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # fail-open: the caller treats None as "unknown"
        logger.warning("claude-code-bridge: channel lookup failed (%s)", type(exc).__name__)
        return None
    name = data.get("name") if isinstance(data, dict) else None
    if not name:
        return None
    _channel_names[channel_id] = str(name)
    return _channel_names[channel_id]
