"""mattermost-auto-vacate configuration: defaults overlaid with the ``mattermost_auto_vacate:`` block of the profile config."""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("mattermost_auto_vacate")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Sweep period, seconds.
    "interval_s": 300,
    # A channel must have had no human member (the bot alone, or with other bots only) for at least
    # this long, across consecutive sweeps, before the bot leaves. Protects channels being set up.
    "grace_s": 600,
    # Internal channel names that are never left.
    "exclude": ["town-square", "off-topic"],
    # Log what would happen; never leave.
    "dry_run": False,
}

_TRUE = ("1", "true", "yes", "on")


def hermes_home() -> Path:
    """``$HERMES_HOME`` if set and non-empty, else ``~/.hermes``."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def state_path() -> Path:
    """Where the first-seen-alone timestamps are kept: ``<HERMES_HOME>/mattermost-auto-vacate/state.json``."""
    return hermes_home() / "mattermost-auto-vacate" / "state.json"


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_seconds(value: Any, default: int, minimum: int) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        logger.warning("mattermost-auto-vacate: invalid seconds value %r; using %s", value, default)
        return default
    if seconds < minimum:
        logger.warning("mattermost-auto-vacate: seconds value %r below %s; using %s", value, minimum, default)
        return default
    return seconds


def _as_names(value: Any) -> list[str]:
    if value is None:
        return list(DEFAULTS["exclude"])
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return list(DEFAULTS["exclude"])


def load() -> dict[str, Any]:
    """Return DEFAULTS overlaid with the ``mattermost_auto_vacate`` block of ``hermes_home()/config.yaml``.

    A missing file or block yields DEFAULTS. An unreadable config file fails closed: the
    returned ``enabled`` is False, so nothing is left automatically (the error is logged).
    """
    cfg = copy.deepcopy(DEFAULTS)
    path = hermes_home() / "config.yaml"
    if not path.exists():
        return cfg
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("mattermost-auto-vacate: config unreadable (%s); plugin disabled", type(exc).__name__)
        cfg["enabled"] = False
        return cfg
    block = data.get("mattermost_auto_vacate") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return cfg
    cfg["enabled"] = _as_bool(block.get("enabled", cfg["enabled"]), cfg["enabled"])
    cfg["dry_run"] = _as_bool(block.get("dry_run", cfg["dry_run"]), cfg["dry_run"])
    cfg["interval_s"] = _as_seconds(block.get("interval_s", cfg["interval_s"]), DEFAULTS["interval_s"], 1)
    cfg["grace_s"] = _as_seconds(block.get("grace_s", cfg["grace_s"]), DEFAULTS["grace_s"], 0)
    cfg["exclude"] = _as_names(block.get("exclude", cfg["exclude"]))
    return cfg
