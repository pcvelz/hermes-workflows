"""Sweep logic for mattermost-auto-vacate: find channels where the bot is the only human left, and leave.

Only the bot's own membership is ever removed (``DELETE /channels/{id}/members/{bot}``). Channels are
never archived or deleted, posts are never touched, and other members are never removed.
REST errors are raised as ``MattermostError``; the sweep logs them and the next sweep retries.
Credentials are passed in by the caller and never logged.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("mattermost_auto_vacate")

TIMEOUT_SECONDS = 20
PAGE_SIZE = 200
# Channel types the bot may leave. "D" (direct) and "G" (group message) are never touched.
LEAVABLE_TYPES = ("O", "P")


class MattermostError(RuntimeError):
    """A REST call failed. The message names the method and path only, never the token."""


class Client:
    """Minimal Mattermost REST client (bearer token) built on ``urllib``."""

    def __init__(self, base_url: str, token: str, timeout: float = TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise MattermostError(f"{method} {path.split('?')[0]} returned HTTP {exc.code}") from None
        except Exception as exc:
            raise MattermostError(f"{method} {path.split('?')[0]} failed ({type(exc).__name__})") from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise MattermostError(f"{method} {path.split('?')[0]} returned non-JSON") from None

    def get(self, path: str) -> Any:
        return self._call("GET", path)

    def post(self, path: str, body: Any) -> Any:
        return self._call("POST", path, body)

    def delete(self, path: str) -> Any:
        return self._call("DELETE", path)


def list_member_ids(client: Client, channel_id: str) -> list[str]:
    """Every user id that is a member of the channel (paged)."""
    ids: list[str] = []
    page = 0
    while True:
        batch = client.get(f"/api/v4/channels/{channel_id}/members?page={page}&per_page={PAGE_SIZE}")
        if not isinstance(batch, list):
            raise MattermostError("channel members response was not a list")
        ids.extend(str(m["user_id"]) for m in batch if isinstance(m, dict) and m.get("user_id"))
        if len(batch) < PAGE_SIZE:
            return ids
        page += 1


def count_humans(client: Client, user_ids: list[str]) -> int:
    """How many of ``user_ids`` are not bots. An id the server does not describe counts as a human (fail-closed)."""
    if not user_ids:
        return 0
    users = client.post("/api/v4/users/ids", user_ids)
    if not isinstance(users, list):
        raise MattermostError("users/ids response was not a list")
    is_bot = {str(u.get("id")): bool(u.get("is_bot")) for u in users if isinstance(u, dict)}
    return sum(1 for uid in user_ids if not is_bot.get(uid, False))


def load_state(path: Path) -> dict[str, float]:
    """Channel id -> epoch seconds when the channel was first seen with no human. Missing or corrupt = empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("auto-vacate: state file unreadable; starting with an empty state")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}


def save_state(path: Path, state: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def sweep(
    client: Client,
    bot_id: str,
    cfg: dict[str, Any],
    state: dict[str, float],
    now: float,
    user_ref: str = "me",
) -> bool:
    """One pass over the bot's channels. Mutates ``state``; returns True when the whole pass succeeded.

    ``user_ref`` is the user the channel lists are read for (``me`` for the bot itself).
    Returns False when any REST call failed; state is then not pruned, so no timer is lost to an outage.
    """
    exclude = set(cfg.get("exclude") or [])
    grace = float(cfg.get("grace_s", 600))
    dry_run = bool(cfg.get("dry_run", False))
    complete = True
    seen: set[str] = set()

    try:
        teams = client.get(f"/api/v4/users/{user_ref}/teams")
    except MattermostError as exc:
        logger.warning("auto-vacate: listing teams failed (%s); retrying next sweep", exc)
        return False

    for team in teams or []:
        team_id = str(team.get("id") or "")
        team_name = str(team.get("name") or team_id)
        if not team_id:
            continue
        try:
            channels = client.get(f"/api/v4/users/{user_ref}/teams/{team_id}/channels")
        except MattermostError as exc:
            logger.warning("auto-vacate: listing channels of team %s failed (%s); retrying next sweep", team_name, exc)
            complete = False
            continue

        for channel in channels or []:
            cid = str(channel.get("id") or "")
            name = str(channel.get("name") or cid)
            if not cid or channel.get("type") not in LEAVABLE_TYPES or name in exclude:
                continue
            seen.add(cid)
            try:
                others = [uid for uid in list_member_ids(client, cid) if uid != bot_id]
                humans = count_humans(client, others)
            except MattermostError as exc:
                logger.warning("auto-vacate: checking channel %s failed (%s); retrying next sweep", name, exc)
                complete = False
                continue

            if humans > 0:
                if state.pop(cid, None) is not None:
                    logger.debug("auto-vacate: channel %s has a human member again; timer reset", name)
                continue

            first_alone = state.setdefault(cid, now)
            alone_for = int(now - first_alone)
            if alone_for < grace:
                continue
            if dry_run:
                logger.info(
                    "auto-vacate (dry run): would leave channel %s (team %s), sole member for %ds",
                    name, team_name, alone_for,
                )
                continue
            try:
                client.delete(f"/api/v4/channels/{cid}/members/{bot_id}")
            except MattermostError as exc:
                logger.warning("auto-vacate: leaving channel %s failed (%s); retrying next sweep", name, exc)
                complete = False
                continue
            state.pop(cid, None)
            logger.info("auto-vacate: left channel %s (team %s), sole member for %ds", name, team_name, alone_for)

    if complete:
        for cid in [c for c in state if c not in seen]:
            del state[cid]  # the bot left, the channel was archived, or it became excluded / direct
    return complete


def run_once(
    cfg: dict[str, Any],
    base_url: str,
    token: str,
    state_file: Path,
    now: Callable[[], float] = time.time,
) -> bool:
    """Resolve the bot's identity, run one sweep, and persist the state. Raises ``MattermostError`` if identity fails."""
    client = Client(base_url, token)
    me = client.get("/api/v4/users/me")
    if not isinstance(me, dict) or not me.get("id"):
        raise MattermostError("users/me response had no id")
    state = load_state(state_file)
    complete = sweep(client, str(me["id"]), cfg, state, now(), user_ref="me")
    save_state(state_file, state)
    return complete
