#!/usr/bin/env python3
"""Smoke tests for claude-code-bridge ``reply_where_written``: a reply goes where the user wrote.

Worker side (``lib/worker.py`` ``_reply_root`` through ``tick``) and sender side (``lib/senders.py``):
a message written inside a thread gets its reply in that thread, a main-channel message gets a
top-level reply, and with the option off the placement is unchanged. No network, no real session:
the session module and the post function are mocks; ``poster.post`` and the adapter are mocks.
"""
import asyncio
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN_DIR))

from lib import config, senders, spool, worker  # noqa: E402

CID = "chan-rww"
NOW = 1_800_000_000.0


class _WorkerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["home"] = str(self.tmp / "claude-code-bridge")
        self.cfg["allowed_users"] = ["alice"]
        self.cfg["allowed_channels"] = [CID]
        self.session = mock.MagicMock(name="session")
        self.session.exists.return_value = True
        self.session.is_idle.return_value = True
        self.session.fingerprint.return_value = "fp"
        self.post = mock.MagicMock(name="post_fn")
        patcher = mock.patch.object(worker, "session", self.session)
        patcher.start()
        self.addCleanup(patcher.stop)
        spool.save_state(self.cfg, CID, {**spool.load_state(self.cfg, CID), "launch_fingerprint": "fp"})

    def tearDown(self):
        self._tmp.cleanup()

    def set_inflight(self, post_id, root_id):
        state = spool.load_state(self.cfg, CID)
        state.update(inflight={"post_id": post_id, "root_id": root_id, "text": "q"}, inflight_since=NOW)
        spool.save_state(self.cfg, CID, state)

    def write_reply(self, text):
        outbox = spool.channel_dir(self.cfg, CID) / "outbox"
        path = outbox / f"{time.time_ns()}.json"
        path.write_text(json.dumps({"session_id": "s", "text": text, "ts": NOW}), encoding="utf-8")


class ReplyWhereWrittenWorkerTests(_WorkerBase):
    def test_default_is_off(self):
        self.assertIs(config.DEFAULTS["reply_where_written"], False)

    def test_option_on_thread_post_replies_in_that_thread(self):
        self.cfg["reply_where_written"] = True
        self.set_inflight(post_id="p-reply", root_id="root-9")
        self.write_reply("in the thread")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, "root-9", "in the thread")

    def test_option_on_main_post_gets_top_level_reply(self):
        self.cfg["reply_where_written"] = True
        self.set_inflight(post_id="p-main", root_id=None)
        self.write_reply("top level")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, None, "top level")

    def test_option_on_overrides_reply_in_thread_for_main_post(self):
        self.cfg["reply_where_written"] = True
        self.cfg["reply_in_thread"] = True
        self.set_inflight(post_id="p-main", root_id=None)
        self.write_reply("still top level")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, None, "still top level")

    def test_option_off_thread_post_unchanged(self):
        self.set_inflight(post_id="p-reply", root_id="root-9")
        self.write_reply("in the thread")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, "root-9", "in the thread")

    def test_option_off_main_post_unchanged_top_level(self):
        self.set_inflight(post_id="p-main", root_id=None)
        self.write_reply("top level")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, None, "top level")

    def test_option_off_reply_in_thread_still_threads_main_post(self):
        self.cfg["reply_in_thread"] = True
        self.set_inflight(post_id="p-main", root_id=None)
        self.write_reply("threaded")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, "p-main", "threaded")


class ReplyWhereWrittenSenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["home"] = str(self.tmp / "claude-code-bridge")
        spool.save_state(self.cfg, CID, {**spool.load_state(self.cfg, CID), "platform": "mattermost"})
        self.rest_post = mock.patch.object(senders.poster, "post", return_value=True).start()
        self.adapter = mock.MagicMock(name="adapter")
        mock.patch.object(senders, "_adapter", return_value=self.adapter).start()
        mock.patch.object(senders, "_loop", object()).start()
        self.result = SimpleNamespace(success=True, error=None)
        self.run = mock.patch.object(asyncio, "run_coroutine_threadsafe",
                                     return_value=mock.MagicMock(result=mock.MagicMock(return_value=self.result))).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self._tmp.cleanup)

    def test_option_on_threaded_reply_goes_over_rest_with_root(self):
        self.cfg["reply_where_written"] = True
        self.assertTrue(senders.send(self.cfg, CID, "root-9", "hello"))
        self.rest_post.assert_called_once()
        self.assertEqual(self.rest_post.call_args.args[:2], (CID, "root-9"))
        self.adapter.send.assert_not_called()
        self.run.assert_not_called()

    def test_option_on_top_level_reply_keeps_adapter_path(self):
        self.cfg["reply_where_written"] = True
        self.assertTrue(senders.send(self.cfg, CID, None, "hello"))
        self.rest_post.assert_not_called()
        self.adapter.send.assert_called_once()
        self.assertIsNone(self.adapter.send.call_args.kwargs["reply_to"])

    def test_option_off_threaded_reply_keeps_adapter_path(self):
        self.assertTrue(senders.send(self.cfg, CID, "root-9", "hello"))
        self.rest_post.assert_not_called()
        self.adapter.send.assert_called_once()
        self.assertEqual(self.adapter.send.call_args.kwargs["reply_to"], "root-9")


if __name__ == "__main__":
    unittest.main()
