#!/usr/bin/env python3
"""Smoke tests for claude-code-bridge reply delivery (``lib/senders.py``).

No test touches the network: ``poster.post`` is mocked, and the gateway is a stand-in object
with an ``adapters`` map. A temporary HERMES_HOME holds the channel state.
"""
import asyncio
import copy
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN_DIR))

from lib import config, senders, spool  # noqa: E402

CID = "chan-senders"
ROOT = "root-post-1"


class _Platform:
    def __init__(self, value):
        self.value = value


class _Adapter:
    def __init__(self, success=True):
        self.success = success
        self.calls = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append((chat_id, content, reply_to))
        return SimpleNamespace(success=self.success, error=None if self.success else "boom")


class SenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["home"] = str(self.tmp / "claude-code-bridge")
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.tmp / "hermes-home")
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._loop_thread.start()
        self.addCleanup(self._cleanup)
        self.post = mock.patch.object(senders.poster, "post", return_value=True).start()
        senders._gateway, senders._loop = None, None

    def _cleanup(self):
        senders._gateway, senders._loop = None, None
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._loop_thread.join(timeout=5)
        self.loop.close()
        self._tmp.cleanup()
        mock.patch.stopall()
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home

    def _set_platform(self, platform):
        state = spool.load_state(self.cfg, CID)
        state["platform"] = platform
        spool.save_state(self.cfg, CID, state)

    def _remember(self, adapter):
        gateway = SimpleNamespace(adapters={_Platform("mattermost"): adapter})
        senders.remember(gateway, self.loop)

    def test_mattermost_without_gateway_reference_posts_via_rest(self):
        # Startup case: in-flight turns resume before any inbound message, so no gateway is known yet.
        self._set_platform("mattermost")
        result = senders.send(self.cfg, CID, ROOT, "resumed answer")
        self.assertIs(result, True)
        self.post.assert_called_once_with(CID, ROOT, "resumed answer", 15000)

    def test_mattermost_rest_failure_is_false_so_the_reply_is_retried(self):
        self._set_platform("mattermost")
        self.post.return_value = False
        self.assertIs(senders.send(self.cfg, CID, None, "answer"), False)

    def test_other_platform_without_adapter_is_held(self):
        self._set_platform("telegram")
        self.assertIsNone(senders.send(self.cfg, CID, None, "answer"))
        self.post.assert_not_called()

    def test_live_adapter_is_used_and_rest_is_not(self):
        self._set_platform("mattermost")
        adapter = _Adapter(success=True)
        self._remember(adapter)
        self.assertIs(senders.send(self.cfg, CID, ROOT, "via adapter"), True)
        self.assertEqual(adapter.calls, [(CID, "via adapter", ROOT)])
        self.post.assert_not_called()

    def test_failed_adapter_send_is_false_and_not_rerouted_to_rest(self):
        self._set_platform("mattermost")
        self._remember(_Adapter(success=False))
        self.assertIs(senders.send(self.cfg, CID, None, "answer"), False)
        self.post.assert_not_called()

    def test_empty_text_is_never_sent(self):
        self._set_platform("mattermost")
        self.assertIs(senders.send(self.cfg, CID, None, "   "), False)
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
