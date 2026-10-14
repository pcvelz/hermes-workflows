"""claude-code-bridge — answer chat channels with a persistent, interactive Claude Code session.

A ``pre_gateway_dispatch`` hook (runs before gateway authorization) takes each allowed
Mattermost message, spools it per channel and returns ``{"action": "skip"}`` so Hermes
does not answer it. A worker thread inside the gateway types the message into a tmux
session running ``claude``; a Stop hook writes the reply to the channel outbox and the
worker posts it in the thread. Hermes' own model is not called for bridged messages.

Passthrough (``passthrough_patterns``): Hermes answers in the same channel, in a thread under the
investigation post (or where the user wrote, with ``passthrough_reply_in_thread: false``)
(``source.thread_id``), and the message is not bridged.

Fail-closed: disabled by default, an empty allowlist admits nobody, and a non-Mattermost
or non-allowed message returns ``None`` so Hermes handles it as before. Any error inside
the hook is logged and returns ``None``; nothing is raised into the gateway.

Self-test: ``python3 plugins/claude-code-bridge/__init__.py``
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from .lib import config, poster, senders, spool, worker
except ImportError:  # executed as a script, or loaded without a package
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lib import config, poster, senders, spool, worker

logger = logging.getLogger("claude_code_bridge")

RESET_COMMANDS = ("/reset", "!reset")
RESET_CLEAR = "!reset clear"  # a reset that also deletes the channel's posts since the previous reset
# Operator debrief: "!debrief <question>" from an allowed sender. The session answers the operator: the answer is filed
# in the channel's spool, and posted to operator_channel only when it is in the same team. See docs/claude-code-bridge.md, "Debrief".
DEBRIEF_RE = re.compile(r"!debrief(?=\s|$)(.*)", re.IGNORECASE | re.DOTALL)


def _debrief(cfg: dict[str, Any], cid: str, item: dict[str, Any], question: str, ensure_worker: bool) -> dict[str, str]:
    """Queue a debrief item for the channel's session. The answer is filed in the spool, so no operator channel is needed."""
    question = question.strip()
    if not question:
        logger.warning("claude-code-bridge: empty !debrief dropped on channel %s", cid)
        return {"action": "skip"}
    item.update(control="debrief", text=question)
    spool.enqueue(cfg, cid, item)
    if ensure_worker:
        _start_worker()
    return {"action": "skip"}


def _platform(source: Any) -> str:
    platform = getattr(source, "platform", "") or ""
    return str(getattr(platform, "value", platform)).lower()


def _is_passthrough(text: str, cfg: dict[str, Any]) -> bool:
    """True when ``text`` matches a ``passthrough_patterns`` regex (case-insensitive). Bad patterns are skipped."""
    for pattern in cfg.get("passthrough_patterns") or []:
        try:
            if re.search(str(pattern), text, re.IGNORECASE):
                return True
        except re.error:
            logger.warning("claude-code-bridge: ignoring invalid passthrough pattern %r", pattern)
    return False


def _is_passthrough_channel(cid: str, cfg: dict[str, Any]) -> bool:
    """True when the channel's name starts with a ``passthrough_channel_prefixes`` entry.

    Fail-closed to Hermes: a name that cannot be resolved counts as passthrough.
    """
    prefixes = [str(p) for p in cfg.get("passthrough_channel_prefixes") or [] if str(p)]
    if not prefixes:
        return False
    name = poster.channel_name(cid)
    return name is None or any(name.startswith(p) for p in prefixes)


def _decide(event: Any, cfg: dict[str, Any], ensure_worker: bool = True) -> dict[str, str] | None:
    """Return the hook result for one event. ``ensure_worker=False`` skips the thread (self-test)."""
    if not cfg.get("enabled"):
        return None
    source = getattr(event, "source", None)
    if source is None or not _platform(source):
        return None

    cid = str(getattr(source, "chat_id", "") or "")
    allowed_channels = cfg.get("allowed_channels") or []
    if cid not in allowed_channels and "*" not in allowed_channels:
        return None  # not ours: Hermes handles the message
    if _is_passthrough_channel(cid, cfg):
        return None  # research channel: Hermes answers every message in it

    user = getattr(source, "user_name", None)
    allowed_users = cfg.get("allowed_users") or []
    if user not in allowed_users and "*" not in allowed_users:
        return {"action": "skip"}  # channel is bridged, sender is not: drop silently

    raw = getattr(event, "raw_message", None)
    raw_root = raw.get("root_id") if isinstance(raw, dict) else None
    text = getattr(event, "text", "") or ""
    item = {
        "platform": _platform(source),
        "post_id": getattr(event, "message_id", None),
        "root_id": raw_root or getattr(source, "thread_id", None) or None,
        "user": user,
        "text": text,
        "ts": time.time(),
    }
    debrief = DEBRIEF_RE.match(text.strip())
    if debrief:
        return _debrief(cfg, cid, item, debrief.group(1), ensure_worker)
    words = " ".join(text.strip().lower().split())
    if words in RESET_COMMANDS or words == RESET_CLEAR:
        item["control"] = "reset"  # the worker performs the reset; nothing is posted to chat
        if words == RESET_CLEAR:
            item["clear"] = True  # also delete the channel's posts since the previous reset (worker._apply_reset)
    elif _is_passthrough(text, cfg):
        # Investigation: Hermes answers it (native delegate_task fan-out), nothing is bridged.
        # The gateway sends the reply into the thread named by source.thread_id: the existing
        # thread root if the message is already a reply, else the investigation post itself.
        in_thread = item["root_id"]  # the user wrote inside a thread: always stay in it
        if cfg.get("passthrough_reply_in_thread", True):
            in_thread = in_thread or item["post_id"]  # public default: thread on the investigation post
        source.thread_id = in_thread or None
        return None
    spool.enqueue(cfg, cid, item)
    if ensure_worker:
        _start_worker()
    return {"action": "skip"}


def _on_dispatch(event: Any = None, gateway: Any = None, **kwargs: Any) -> dict[str, str] | None:
    """``pre_gateway_dispatch`` hook. Fail-open: any error is logged and returns None."""
    try:
        if gateway is not None:
            # The hook runs on the gateway's event loop; replies are sent back onto it.
            senders.remember(gateway, asyncio.get_running_loop())
        return _decide(event, config.load())
    except Exception:
        logger.exception("claude-code-bridge: dispatch hook failed; message left to Hermes")
        return None


def _start_worker() -> None:
    worker.start_thread(config.load, lambda c, r, t: senders.send(config.load(), c, r, t))


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
            ("non-allowed channel -> None", _decide(_event("mattermost", "other", "alice"), _cfg("true"), False) is None),
            ("unknown user -> skip, nothing queued",
             _decide(_event("mattermost", "chan1", "mallory"), _cfg("true"), False) == {"action": "skip"}
             and spool.pending(_cfg("true"), "chan1") == []),
            ("allowed user -> skip and enqueued",
             _decide(_event("mattermost", "chan1", "alice"), _cfg("true"), False) == {"action": "skip"}
             and len(spool.pending(_cfg("true"), "chan1")) == 1),
            ("telegram allowed channel -> skip and enqueued",
             _decide(_event("telegram", "chan1", "alice"), _cfg("true"), False) == {"action": "skip"}
             and len(spool.pending(_cfg("true"), "chan1")) == 2),
        ]

        # Custom cwd: secret denies are added; a channel without cwd gets none.
        from lib import session

        cwd_dir = Path(tmp, "cwd-project")
        cwd_dir.mkdir()
        cwd_cfg_text = base.format(enabled="true", home=home) + f'  channels:\n    chanw: {{cwd: "{cwd_dir}"}}\n'
        Path(tmp, "config.yaml").write_text(cwd_cfg_text, encoding="utf-8")
        stamp[0] += 10
        os.utime(Path(tmp, "config.yaml"), ns=(stamp[0] * 10**9, stamp[0] * 10**9))
        cwd_cfg = config.load()
        denies_custom = session.prepare_dir(cwd_cfg, "chanw")
        import json as _json
        deny_custom = _json.loads((denies_custom / ".claude" / "settings.json").read_text())["permissions"]["deny"]
        denies_plain = _json.loads((session.prepare_dir(cwd_cfg, "chan1") / ".claude" / "settings.json").read_text())["permissions"]["deny"]
        checks += [
            ("custom cwd -> secret read denies added",
             all(any(d.endswith(p) for d in deny_custom) for p in ("/config/.env)", "/config/.storage/**)", "/**/secrets.yaml)"))),
            ("no cwd -> no secret read denies",
             not any(d.endswith("/config/.env)") for d in denies_plain)),
        ]

        # senders: a SendResult(success=False) must read as a failed send.
        import asyncio
        import threading

        class _Adapter:
            def __init__(self, result: Any) -> None:
                self.result = result

            async def send(self, chat_id: str, content: str, reply_to: Any = None, metadata: Any = None) -> Any:
                return self.result

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()

        class _Platform:
            def __init__(self, value: str) -> None:
                self.value = value

        def _send_with(result: Any) -> bool:
            senders.remember(SimpleNamespace(adapters={_Platform("mattermost"): _Adapter(result)}), loop)
            spool.save_state(cwd_cfg, "sendchan", {"platform": "mattermost"})
            return senders.send(cwd_cfg, "sendchan", None, "hi")

        checks += [
            ("send: SendResult(success=True) -> True", _send_with(SimpleNamespace(success=True, error=None)) is True),
            ("send: SendResult(success=False) -> False", _send_with(SimpleNamespace(success=False, error="boom")) is False),
        ]
        failed = [name for name, ok in checks if not ok]
        for name, ok in checks:
            print(("PASS " if ok else "FAIL ") + name)
        if failed:
            sys.exit(1)
        print("claude-code-bridge self-test: all checks passed")
