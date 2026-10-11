"""Smoke tests for ``!reset clear`` (claude-code-bridge): a reset that also deletes the channel's posts since the
previous reset.

Covers: a plain ``!reset`` deletes nothing; ``!reset clear`` deletes the non-system posts since the stored reset
point (bot and user posts), keeps system posts, falls back to 24 h when no reset point is stored; a non-allowed
sender is ignored; delete and listing failures are logged; the paging stop rule. The session and the Mattermost
calls are mocked; never starts a real claude or calls Mattermost.
"""
import copy
import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN))

from lib import config, poster, spool, worker  # noqa: E402

CID = "chan1"
NOW = 1_700_000_000.0
DAY_MS = 86_400_000


def _load_plugin_init():
    spec = importlib.util.spec_from_file_location("ccb_plugin_init_reset_clear", PLUGIN / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_init()


def _event(text, user="ana"):
    source = SimpleNamespace(platform=SimpleNamespace(value="mattermost"), chat_id=CID, user_name=user, thread_id=None)
    return SimpleNamespace(source=source, text=text, message_id="p9", raw_message={"root_id": ""})


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg.update(enabled=True, claude_bin="/bin/echo", home=str(self.tmp / "ccb"),
                        allowed_channels=[CID], allowed_users=["ana"], operator_channel="op-channel")
        self.session = mock.MagicMock(name="session")
        self.session.exists.return_value = True
        self.session.is_idle.return_value = True
        self.session.inject.return_value = True
        self.session.fingerprint.return_value = "fp"
        self.post = mock.MagicMock(name="post_fn", return_value=True)
        self.listed = mock.MagicMock(name="channel_posts_since", return_value=[])
        self.delete = mock.MagicMock(name="delete_post", return_value=True)
        for patcher in (mock.patch.object(worker, "session", self.session),
                        mock.patch.object(poster, "channel_posts_since", self.listed),
                        mock.patch.object(poster, "delete_post", self.delete)):
            patcher.start()
            self.addCleanup(patcher.stop)
        spool.save_state(self.cfg, CID, {**spool.load_state(self.cfg, CID), "launch_fingerprint": "fp"})

    def tearDown(self):
        self._tmp.cleanup()

    def enqueue_reset(self, clear=False, ts=NOW):
        item = {"control": "reset", "user": "ana", "ts": ts, "post_id": "reset-post", "text": "!reset clear" if clear else "!reset"}
        if clear:
            item["clear"] = True
        spool.enqueue(self.cfg, CID, item)

    def set_last_reset(self, ts):
        spool.save_state(self.cfg, CID, {**spool.load_state(self.cfg, CID), "last_reset_ts": ts})


class DispatchTests(_Base):
    def test_plain_reset_is_a_reset_without_clear(self):
        plugin._decide(_event("!reset"), self.cfg, ensure_worker=False)
        item = json.loads(spool.pending(self.cfg, CID)[0].read_text(encoding="utf-8"))
        self.assertEqual(item["control"], "reset")
        self.assertNotIn("clear", item)

    def test_reset_clear_is_a_reset_with_clear(self):
        plugin._decide(_event("  !RESET   clear "), self.cfg, ensure_worker=False)
        item = json.loads(spool.pending(self.cfg, CID)[0].read_text(encoding="utf-8"))
        self.assertEqual(item["control"], "reset")
        self.assertTrue(item["clear"])

    def test_non_allowed_sender_cannot_clear(self):
        result = plugin._decide(_event("!reset clear", user="mallory"), self.cfg, ensure_worker=False)
        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(spool.pending(self.cfg, CID), [])


class PlainResetTests(_Base):
    def test_plain_reset_deletes_nothing_and_stores_the_reset_point(self):
        self.enqueue_reset(clear=False, ts=NOW)
        worker.tick(self.cfg, self.post, now=NOW)
        self.listed.assert_not_called()
        self.delete.assert_not_called()
        self.assertEqual(spool.load_state(self.cfg, CID)["last_reset_ts"], NOW)
        self.post.assert_not_called()  # nothing posted to chat


class ClearTests(_Base):
    def test_clear_deletes_posts_since_the_stored_reset_point(self):
        self.set_last_reset(NOW - 600)
        self.listed.return_value = [
            {"id": "bot-1", "type": "", "user_id": "bot", "create_at": int((NOW - 300) * 1000)},
            {"id": "user-1", "type": "", "user_id": "ana", "create_at": int((NOW - 200) * 1000)},
            {"id": "sys-1", "type": "system_join_channel", "user_id": "ana", "create_at": int((NOW - 100) * 1000)},
            {"id": "reset-post", "type": "", "user_id": "ana", "create_at": int(NOW * 1000)},
        ]
        self.enqueue_reset(clear=True, ts=NOW)
        worker.tick(self.cfg, self.post, now=NOW)
        self.listed.assert_called_once_with(CID, int((NOW - 600) * 1000))
        deleted = sorted(c.args[0] for c in self.delete.call_args_list)
        self.assertEqual(deleted, ["bot-1", "reset-post", "user-1"])  # bot, user and the command itself; system kept
        self.post.assert_not_called()
        self.assertEqual(spool.load_state(self.cfg, CID)["last_reset_ts"], NOW)

    def test_clear_keeps_posts_after_the_reset_clear_post(self):
        self.set_last_reset(NOW - 600)
        reset_at = int((NOW - 5) * 1000)  # the post's own create_at, before the gateway's ts
        self.listed.return_value = [
            {"id": "later-2", "type": "", "user_id": "ana", "create_at": reset_at + 1},
            {"id": "later-1", "type": "", "user_id": "bot", "create_at": reset_at + 3000},
            {"id": "reset-post", "type": "", "user_id": "ana", "create_at": reset_at},
            {"id": "user-1", "type": "", "user_id": "ana", "create_at": int((NOW - 200) * 1000)},
        ]
        self.enqueue_reset(clear=True, ts=NOW)
        worker.tick(self.cfg, self.post, now=NOW)
        deleted = sorted(c.args[0] for c in self.delete.call_args_list)
        self.assertEqual(deleted, ["reset-post", "user-1"])

    def test_clear_without_a_stored_reset_point_uses_the_24_hour_fallback(self):
        self.enqueue_reset(clear=True, ts=NOW)
        worker.tick(self.cfg, self.post, now=NOW)
        self.listed.assert_called_once_with(CID, int((NOW - 24 * 3600) * 1000))
        self.assertEqual(DAY_MS, 24 * 3600 * 1000)

    def test_clear_listing_failure_deletes_nothing_and_logs(self):
        self.listed.return_value = None
        self.enqueue_reset(clear=True, ts=NOW)
        with self.assertLogs("claude_code_bridge", level=logging.WARNING) as logs:
            worker.tick(self.cfg, self.post, now=NOW)
        self.delete.assert_not_called()
        self.assertTrue(any("could not list" in line for line in logs.output))
        self.assertEqual(spool.load_state(self.cfg, CID)["last_reset_ts"], NOW)

    def test_delete_failures_are_logged_and_not_retried(self):
        self.listed.return_value = [{"id": "u1", "type": "", "create_at": int(NOW * 1000)},
                                    {"id": "u2", "type": "", "create_at": int(NOW * 1000)}]
        self.delete.return_value = False
        self.enqueue_reset(clear=True, ts=NOW)
        with self.assertLogs("claude_code_bridge", level=logging.WARNING) as logs:
            worker.tick(self.cfg, self.post, now=NOW)
        self.assertEqual(self.delete.call_count, 2)  # each once, no retry
        self.assertEqual(sum("could not delete" in line for line in logs.output), 2)
        self.post.assert_not_called()


class ListingTests(unittest.TestCase):
    """poster.channel_posts_since: newest-first paging that stops at the first older post."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"MATTERMOST_URL": "https://mm.example.test", "MATTERMOST_TOKEN": "t"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _pages(self, pages):
        responses = []
        for page in pages:
            body = json.dumps(page).encode("utf-8")
            responses.append(io.BytesIO(body))
        opened = []

        def fake_urlopen(request, timeout=None):
            opened.append(request.full_url)
            return responses.pop(0)  # a BytesIO: usable as a context manager

        return fake_urlopen, opened

    def test_keeps_posts_at_or_after_since_and_stops_at_the_first_older_one(self):
        page = {"order": ["new2", "new1", "old1"],
                "posts": {"new2": {"id": "new2", "create_at": 2000}, "new1": {"id": "new1", "create_at": 1500},
                          "old1": {"id": "old1", "create_at": 500}}}
        fake, opened = self._pages([page])
        with mock.patch("urllib.request.urlopen", fake):
            got = poster.channel_posts_since(CID, 1000)
        self.assertEqual([p["id"] for p in got], ["new2", "new1"])
        self.assertEqual(len(opened), 1)  # stopped on the first page: an older post was reached
        self.assertIn("page=0", opened[0])

    def test_pages_on_while_the_page_is_full_and_all_new(self):
        full = {"order": [f"p{i}" for i in range(poster.POSTS_PAGE_SIZE)],
                "posts": {f"p{i}": {"id": f"p{i}", "create_at": 5000} for i in range(poster.POSTS_PAGE_SIZE)}}
        last = {"order": ["q1"], "posts": {"q1": {"id": "q1", "create_at": 4000}}}
        fake, opened = self._pages([full, last])
        with mock.patch("urllib.request.urlopen", fake):
            got = poster.channel_posts_since(CID, 1000)
        self.assertEqual(len(got), poster.POSTS_PAGE_SIZE + 1)  # the full page plus q1 (4000 >= 1000)
        self.assertEqual(len(opened), 2)
        self.assertIn("page=1", opened[1])

    def test_request_failure_returns_none_and_logs(self):
        def boom(request, timeout=None):
            raise OSError("network down")

        with mock.patch("urllib.request.urlopen", boom), self.assertLogs("claude_code_bridge", level=logging.WARNING):
            self.assertIsNone(poster.channel_posts_since(CID, 1000))

    def test_missing_credentials_return_none(self):
        with mock.patch.dict(os.environ, {"MATTERMOST_TOKEN": ""}), \
                self.assertLogs("claude_code_bridge", level=logging.WARNING):
            self.assertIsNone(poster.channel_posts_since(CID, 1000))


if __name__ == "__main__":
    unittest.main()
