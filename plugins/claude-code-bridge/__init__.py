"""claude-code-bridge — answer chat channels with a persistent, interactive Claude Code session.

A ``pre_gateway_dispatch`` hook (runs before gateway authorization) takes each allowed
Mattermost message, spools it per channel and returns ``{"action": "skip"}`` so Hermes
does not answer it. A worker thread inside the gateway types the message into a tmux
session running ``claude``; a Stop hook writes the reply to the channel outbox and the
worker posts it in the thread. Hermes' own model is not called for bridged messages.

Passthrough (``passthrough_patterns``): Hermes answers in the same channel, in a thread under the
investigation post (``source.thread_id``), and the message is not bridged.

Fail-closed: disabled by default, an empty allowlist admits nobody, and a non-Mattermost
or non-allowed message returns ``None`` so Hermes handles it as before. Any error inside
the hook is logged and returns ``None``; nothing is raised into the gateway.

Self-test: ``python3 plugins/claude-code-bridge/__init__.py``
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from .lib import config, poster, spool, worker
except ImportError:  # executed as a script, or loaded without a package
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lib import config, poster, spool, worker

logger = logging.getLogger("claude_code_bridge")

RESET_COMMANDS = ("/reset", "!reset")


def _is_mattermost(source: Any) -> bool:
    platform = getattr(source, "platform", "")
    return "mattermost" in str(getattr(platform, "value", platform)).lower()


def _is_passthrough(text: str, cfg: dict[str, Any]) -> bool:
    """True when ``text`` matches a ``passthrough_patterns`` regex (case-insensitive). Bad patterns are skipped."""
    for pattern in cfg.get("passthrough_patterns") or []:
        try:
            if re.search(str(pattern), text, re.IGNORECASE):
                return True
        except re.error:
            logger.warning("claude-code-bridge: ignoring invalid passthrough pattern %r", pattern)
    return False


def _decide(event: Any, cfg: dict[str, Any], ensure_worker: bool = True) -> dict[str, str] | None:
    """Return the hook result for one event. ``ensure_worker=False`` skips the thread (self-test)."""
    if not cfg.get("enabled"):
        return None
    source = getattr(event, "source", None)
    if source is None or not _is_mattermost(source):
        return None

    cid = str(getattr(source, "chat_id", "") or "")
    allowed_channels = cfg.get("allowed_channels") or []
    if cid not in allowed_channels and "*" not in allowed_channels:
        return None  # not ours: Hermes handles the message

    user = getattr(source, "user_name", None)
    allowed_users = cfg.get("allowed_users") or []
    if user not in allowed_users and "*" not in allowed_users:
        return {"action": "skip"}  # channel is bridged, sender is not: drop silently

    raw = getattr(event, "raw_message", None)
    raw_root = raw.get("root_id") if isinstance(raw, dict) else None
    text = getattr(event, "text", "") or ""
    item = {
        "post_id": getattr(event, "message_id", None),
        "root_id": raw_root or getattr(source, "thread_id", None) or None,
        "user": user,
        "text": text,
        "ts": time.time(),
    }
    if text.strip().lower() in RESET_COMMANDS:
        item["control"] = "reset"  # the worker performs the reset; nothing is posted to chat
    elif _is_passthrough(text, cfg):
        # Investigation: Hermes answers it (native delegate_task fan-out), nothing is bridged.
        # The gateway sends the reply into the thread named by source.thread_id: the existing
        # thread root if the message is already a reply, else the investigation post itself.
        source.thread_id = item["root_id"] or item["post_id"] or None
        return None
    spool.enqueue(cfg, cid, item)
    if ensure_worker:
        _start_worker()
    return {"action": "skip"}


def _on_dispatch(event: Any = None, **kwargs: Any) -> dict[str, str] | None:
    """``pre_gateway_dispatch`` hook. Fail-open: any error is logged and returns None."""
    try:
        return _decide(event, config.load())
    except Exception:
        logger.exception("claude-code-bridge: dispatch hook failed; message left to Hermes")
        return None


def _start_worker() -> None:
    worker.start_thread(
        config.load,
        lambda c, r, t: poster.post(c, r, t, config.load().get("max_reply_chars", 15000)),
    )


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
    # Start at load too, so messages queued before a gateway restart are delivered.
    try:
        if config.load().get("enabled"):
            _start_worker()
    except Exception:
        logger.exception("claude-code-bridge: worker start at register failed")


# ---------------------------------------------------------------------------
# Runnable self-test:  python3 plugins/claude-code-bridge/__init__.py
# Runs the allowlist decision on fake events against a temporary HERMES_HOME.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    def _event(platform: str, chat: str, user: str) -> SimpleNamespace:
        source = SimpleNamespace(platform=SimpleNamespace(value=platform), chat_id=chat, user_name=user, thread_id=None)
        return SimpleNamespace(source=source, text="hello", message_id="p1", raw_message={"root_id": ""})

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HERMES_HOME"] = tmp
        base = "claude_code_bridge:\n  enabled: {enabled}\n  allowed_users: [alice]\n  allowed_channels: [chan1]\n  home: {home}\n"
        home = str(Path(tmp) / "claude-code-bridge")
        stamp = [1_700_000_000]

        def _cfg(enabled: str) -> dict[str, Any]:
            path = Path(tmp, "config.yaml")
            path.write_text(base.format(enabled=enabled, home=home), encoding="utf-8")
            stamp[0] += 10  # distinct mtime so config.load() never serves a stale cache entry
            os.utime(path, ns=(stamp[0] * 10**9, stamp[0] * 10**9))
            return config.load()

        checks = [
            ("disabled -> None", _decide(_event("mattermost", "chan1", "alice"), _cfg("false"), False) is None),
            ("non-mattermost -> None", _decide(_event("telegram", "chan1", "alice"), _cfg("true"), False) is None),
            ("non-allowed channel -> None", _decide(_event("mattermost", "other", "alice"), _cfg("true"), False) is None),
            ("unknown user -> skip, nothing queued",
             _decide(_event("mattermost", "chan1", "mallory"), _cfg("true"), False) == {"action": "skip"}
             and spool.pending(_cfg("true"), "chan1") == []),
            ("allowed user -> skip and enqueued",
             _decide(_event("mattermost", "chan1", "alice"), _cfg("true"), False) == {"action": "skip"}
             and len(spool.pending(_cfg("true"), "chan1")) == 1),
        ]
        failed = [name for name, ok in checks if not ok]
        for name, ok in checks:
            print(("PASS " if ok else "FAIL ") + name)
        if failed:
            sys.exit(1)
        print("claude-code-bridge self-test: all checks passed")
