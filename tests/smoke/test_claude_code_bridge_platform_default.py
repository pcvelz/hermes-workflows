#!/usr/bin/env python3
"""Smoke tests for claude-code-bridge: a reply on a channel whose platform is not recorded.

A channel fed by a watcher (not a chat dispatch) has no ``platform`` in its state. The reply was
held as "no live adapter" until the wait budget parked it, although Mattermost has a REST fallback.
An unrecorded platform on a Mattermost channel must go through that fallback. Other unrecorded
platforms still hold the reply. No test calls the network: ``poster.post`` is a mock.
"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN_DIR))

from lib import config, senders, spool  # noqa: E402

CID = "chan-watch"


class UnrecordedPlatformTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["home"] = str(Path(self._tmp.name) / "claude-code-bridge")
        self.post = mock.patch.object(senders.poster, "post", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        senders._gateway, senders._loop = None, None

    def test_null_platform_on_mattermost_channel_uses_rest_fallback(self):
        spool.save_state(self.cfg, CID, {"platform": None})
        self.assertTrue(senders.send(self.cfg, CID, None, "hola"))
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0], CID)

    def test_null_platform_for_watcher_channel_is_posted_and_marked_success(self):
        spool.save_state(self.cfg, CID, {"platform": None})
        result = senders.send(self.cfg, CID, "root-1", "novedades")
        self.assertIs(result, True)
        self.assertEqual(self.post.call_args.args[1], "root-1")

    def test_non_mattermost_unknown_platform_is_still_held(self):
        spool.save_state(self.cfg, CID, {"platform": "telegram"})
        self.assertIsNone(senders.send(self.cfg, CID, None, "hola"))
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
