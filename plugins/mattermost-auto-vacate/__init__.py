"""mattermost-auto-vacate — the bot leaves a Mattermost channel once no human is left in it.

A daemon thread inside the gateway sweeps the bot's channels every ``interval_s`` seconds. A channel
where the bot is the only non-bot member for at least ``grace_s`` (measured across sweeps, reset by any
human joining) is left by the bot itself. Channels are never archived or deleted, posts are never
touched, and other members are never removed. Credentials come from the gateway's own process env
(``MATTERMOST_URL``, ``MATTERMOST_TOKEN``); the token is never logged.

Fail-closed on config: an unreadable config disables the plugin. Missing credentials stop the thread
after one WARNING. REST errors are logged and retried on the next sweep.

Self-test: ``python3 -m unittest tests/smoke/test_mattermost_auto_vacate.py`` (from the repo root).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    from . import config, vacate
except ImportError:  # loaded as a top-level module (tests, scripts)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config  # type: ignore[no-redef]
    import vacate  # type: ignore[no-redef]

logger = logging.getLogger("mattermost_auto_vacate")

_lock = threading.Lock()
_thread: threading.Thread | None = None


def _cycle() -> float | None:
    """One pass of the worker loop. Returns the seconds to sleep, or None to stop the thread."""
    cfg: dict[str, Any] = config.load()
    if not cfg["enabled"]:
        return float(cfg["interval_s"])  # idle: no calls
    base_url = os.environ.get("MATTERMOST_URL", "").rstrip("/")
    token = os.environ.get("MATTERMOST_TOKEN", "")
    if not base_url or not token:
        logger.warning("mattermost-auto-vacate: MATTERMOST_URL or MATTERMOST_TOKEN missing; plugin disabled for this process")
        return None
    try:
        vacate.run_once(cfg, base_url, token, config.state_path(), now=time.time)
    except vacate.MattermostError as exc:
        logger.warning("auto-vacate: sweep failed (%s); retrying next sweep", exc)
    except Exception:
        logger.exception("auto-vacate: sweep crashed; retrying next sweep")
    return float(cfg["interval_s"])


def _loop() -> None:
    while True:
        pause = _cycle()
        if pause is None:
            return
        time.sleep(pause)


def register(ctx: Any) -> None:
    """Start the sweep thread once, unless the profile config disables the plugin."""
    global _thread
    if not config.load()["enabled"]:
        logger.info("mattermost-auto-vacate: disabled by config")
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="mattermost-auto-vacate", daemon=True)
        _thread.start()
