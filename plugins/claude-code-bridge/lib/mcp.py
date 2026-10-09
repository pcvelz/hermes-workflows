"""MCP servers for the bridge's Claude Code session, rendered per channel.

``render()`` writes ``<channel-dir>/.mcp.json`` (mode 0600) with the configured ``mcp_servers``
and every ``keyfile:<service>/<name>`` value resolved from ``keys_dir``. The session is then
launched with ``--mcp-config <that file> --strict-mcp-config``. Secret values are never logged;
log lines name the server and the key reference only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("claude_code_bridge")

MCP_FILE = ".mcp.json"
KEYFILE_PREFIX = "keyfile:"
_SEG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class KeyUnavailable(Exception):
    """A keyfile reference is malformed or its file is missing or empty."""


def resolve_keyfile(ref: str, keys: Path) -> str:
    """Read ``keys/<service>/<name>`` for a ``keyfile:<service>/<name>`` reference (whitespace stripped)."""
    parts = ref[len(KEYFILE_PREFIX):].split("/")
    if len(parts) != 2 or not all(_SEG_RE.fullmatch(p) for p in parts):
        raise KeyUnavailable(f"malformed key reference {ref.split(':', 1)[0]}:…")
    try:
        value = (keys / parts[0] / parts[1]).read_text(encoding="utf-8").strip()
    except OSError:
        raise KeyUnavailable(f"key file {parts[0]}/{parts[1]} not readable") from None
    if not value:
        raise KeyUnavailable(f"key file {parts[0]}/{parts[1]} is empty")
    return value


def _resolve(value: Any, keys: Path) -> Any:
    if isinstance(value, str) and value.startswith(KEYFILE_PREFIX):
        return resolve_keyfile(value, keys)
    if isinstance(value, dict):
        return {k: _resolve(v, keys) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, keys) for v in value]
    return value


def payload(cfg: dict[str, Any], warn: bool = True) -> dict[str, Any] | None:
    """The ``.mcp.json`` document for the configured servers (keys resolved), or None when none survive.

    Nothing is written. ``warn=False`` silences the per-server warnings (used by the launch fingerprint).
    Servers with an unresolvable key, or a non-mapping spec, are left out.
    """
    servers = cfg.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        if warn:
            log.warning("claude-code-bridge: mcp_servers is not a mapping; no MCP servers in the session")
        servers = {}
    keys = config.keys_dir(cfg)
    rendered: dict[str, Any] = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            if warn:
                log.warning("claude-code-bridge: MCP server %s is not a mapping; left out", name)
            continue
        try:
            rendered[str(name)] = _resolve(spec, keys)
        except KeyUnavailable as exc:
            if warn:
                log.warning("claude-code-bridge: MCP server %s left out of the session (%s)", name, exc)
    if not rendered:
        return None
    return {"mcpServers": rendered}


def dump(doc: dict[str, Any]) -> str:
    """Serialized ``.mcp.json`` text."""
    return json.dumps(doc, indent=2) + "\n"


def render(cfg: dict[str, Any], channel_dir: Path) -> bool:
    """Write ``channel_dir/.mcp.json`` for the configured servers. True if a file was written.

    When no server survives, any stale file is removed so the session gets no MCP flags.
    """
    path = channel_dir / MCP_FILE
    doc = payload(cfg)
    if doc is None:
        path.unlink(missing_ok=True)
        return False
    tmp = path.with_name(MCP_FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(dump(doc))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


def allowed_tools_for(cfg: dict[str, Any], has_mcp: bool) -> list[str]:
    """``allowed_tools`` plus ``mcp_allowed_tools`` when the session has an MCP file."""
    tools = list(cfg["allowed_tools"])
    if has_mcp:
        tools += [str(t) for t in (cfg.get("mcp_allowed_tools") or [])]
    return tools


def allowed_tools(cfg: dict[str, Any], channel_dir: Path) -> list[str]:
    """``allowed_tools`` plus ``mcp_allowed_tools`` when an MCP file was rendered for the channel."""
    return allowed_tools_for(cfg, (channel_dir / MCP_FILE).is_file())
