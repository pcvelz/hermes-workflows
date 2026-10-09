"""claude-code-bridge configuration: defaults overlaid with the ``claude_code_bridge:`` block of the profile config."""

from __future__ import annotations

import copy
import logging
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("claude_code_bridge")

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "claude_bin": "",
    "model": "haiku",
    "effort": "medium",
    "transport": "tmux",
    "session_prefix": "claude-code-bridge-",
    "home": "",
    "idle_exit_minutes": 60,
    "permission_mode": "dontAsk",
    "allowed_tools": ["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
    "append_system_prompt": (
        "You are answering in a chat channel. Reply in plain text, as one message, "
        "in the user's language. Never mention file paths, tools or infrastructure. "
        "Each message starts with [time · sender]. Messages can arrive late or out of order; "
        "use the timestamps to judge what the latest request is."
    ),
    "max_reply_chars": 15000,
    "allowed_users": [],
    "allowed_channels": [],
    "reply_in_thread": False,
    "wait_budget_minutes": 360,
    "tmux_socket": "claude-code-bridge",
    "poll_seconds": 2,
    # Case-insensitive regexes. A match is not bridged: the hook returns None and Hermes answers.
    "passthrough_patterns": [
        r"\binvestigat",
        r"\binvestigar",
        r"\binvestigaci[oó]n",
        r"\bi want to research\b",
        r"\bquiero investigar\b",
        r"\bdeep research\b",
    ],
    # Claude Code MCP servers for the bridge session (empty = none, no flags).
    # A value "keyfile:<service>/<name>" is read from keys_dir at session start; a server
    # whose key is missing is left out of the session (logged, session still starts).
    "keys_dir": "",
    "mcp_servers": {},
    "mcp_allowed_tools": [],
}

_cache: dict[str, Any] = {"key": None, "value": None}


def hermes_home() -> Path:
    """``$HERMES_HOME`` if set and non-empty, else ``~/.hermes``."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _cache_key(path: Path) -> tuple[str, int | None]:
    try:
        return (str(path), path.stat().st_mtime_ns)
    except OSError:
        return (str(path), None)


def _as_list(value: str) -> list[str]:
    """Parse a JSON/YAML list string, else split on commas."""
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        parsed = None
    if isinstance(parsed, list):
        return [str(v) for v in parsed]
    return [v.strip() for v in value.split(",") if v.strip()]


def load() -> dict[str, Any]:
    """Return DEFAULTS overlaid with ``claude_code_bridge`` from ``hermes_home()/config.yaml``.

    A missing file or key, or a parse error, yields DEFAULTS (parse errors are logged).
    Results are cached by file path and mtime; each call returns a fresh copy.
    """
    path = hermes_home() / "config.yaml"
    key = _cache_key(path)
    if _cache["key"] != key or _cache["value"] is None:
        cfg = copy.deepcopy(DEFAULTS)
        if key[1] is not None:
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                logger.warning("claude-code-bridge: could not parse %s (%s); using defaults", path, type(exc).__name__)
                raw = None
            block = raw.get("claude_code_bridge") if isinstance(raw, dict) else None
            if isinstance(block, dict):
                cfg.update(block)
            # `hermes config set key '["a"]'` stores lists as strings; accept that form.
            for name, default in DEFAULTS.items():
                if isinstance(default, list) and isinstance(cfg.get(name), str):
                    cfg[name] = _as_list(cfg[name])
        _cache["key"] = key
        _cache["value"] = cfg
    return copy.deepcopy(_cache["value"])


def keys_dir(cfg: dict[str, Any]) -> Path:
    """``cfg["keys_dir"]`` (expanded) or ``hermes_home()/"keys"``."""
    configured = cfg.get("keys_dir")
    if configured:
        return Path(str(configured)).expanduser()
    return hermes_home() / "keys"


def home(cfg: dict[str, Any]) -> Path:
    """Bridge state root: ``cfg["home"]`` (expanded) or ``hermes_home()/"claude-code-bridge"``."""
    configured = cfg.get("home")
    if configured:
        return Path(str(configured)).expanduser()
    return hermes_home() / "claude-code-bridge"


def claude_bin(cfg: dict[str, Any]) -> str | None:
    """``cfg["claude_bin"]`` if set, else the first ``claude`` on PATH, else None."""
    configured = cfg.get("claude_bin")
    if configured:
        return str(configured)
    return shutil.which("claude")
