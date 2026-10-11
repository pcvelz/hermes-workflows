"""Smoke tests for the team boundary (claude-code-bridge): a bridged channel's content never leaves its team.

Covers: the team lookup and the same-team rule (poster.same_team); a cross-team operator channel is never posted to
(WARNING, no content); a failed lookup fails closed; a debrief answer is always filed in the channel's spool and
posted to the operator channel only when it is in the same team; blocked-prompt notices follow the same rule; the
per-channel operator_channel override; the load-time warning for a global operator_channel. Mattermost is mocked;
never a real network call.
"""
import copy
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN))

from lib import config, poster, spool, worker  # noqa: E402

CID = "chan1"
OP_SAME = "op-same"
OP_OTHER = "op-other"
NOW = 1_700_000_000.0
ANSWER = "The answer text, which must stay in the file."


def _teams(mapping, fail=()):
    """urlopen fake for GET /api/v4/channels/{id}: returns {"team_id": mapping[id]}; ids in ``fail`` raise."""
    def fake(request, timeout=None):
        channel = request.full_url.rsplit("/", 1)[-1]
        if channel in fail:
            raise OSError("lookup refused")
        return io.BytesIO(json.dumps({"id": channel, "team_id": mapping.get(channel, "")}).encode("utf-8"))
    return fake


class _Env(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"MATTERMOST_URL": "https://mm.example.test", "MATTERMOST_TOKEN": "t"})
        env.start()
        self.addCleanup(env.stop)
        poster._team_ids.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class SameTeamTests(_Env):
    def test_same_team_is_allowed(self):
        with mock.patch("urllib.request.urlopen", _teams({CID: "T1", OP_SAME: "T1"})):
            self.assertTrue(poster.same_team(CID, OP_SAME))

    def test_cross_team_is_blocked_with_a_warning(self):
        with mock.patch("urllib.request.urlopen", _teams({CID: "T1", OP_OTHER: "T2"})), \
                self.assertLogs("claude_code_bridge", level=logging.WARNING) as logs:
            self.assertFalse(poster.same_team(CID, OP_OTHER))
        self.assertTrue(any("another team" in line for line in logs.output))

    def test_lookup_failure_fails_closed_with_a_warning(self):
        with mock.patch("urllib.request.urlopen", _teams({CID: "T1"}, fail=(OP_SAME,))), \
                self.assertLogs("claude_code_bridge", level=logging.WARNING) as logs:
            self.assertFalse(poster.same_team(CID, OP_SAME))
        self.assertTrue(any("fail closed" in line or "lookup failed" in line for line in logs.output))

    def test_a_direct_message_without_a_team_is_blocked(self):
        with mock.patch("urllib.request.urlopen", _teams({CID: "T1", OP_SAME: ""})), \
                self.assertLogs("claude_code_bridge", level=logging.WARNING):
            self.assertFalse(poster.same_team(CID, OP_SAME))

    def test_team_ids_are_cached_after_a_successful_lookup(self):
        fake = mock.MagicMock(side_effect=_teams({CID: "T1", OP_SAME: "T1"}))
        with mock.patch("urllib.request.urlopen", fake):
            poster.same_team(CID, OP_SAME)
            poster.same_team(CID, OP_SAME)
        self.assertEqual(fake.call_count, 2)  # one lookup per channel, then the cache


class _WorkerBase(_Env):
    def setUp(self):
        super().setUp()
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg.update(enabled=True, claude_bin="/bin/echo", home=str(self.tmp / "ccb"),
                        allowed_channels=[CID], allowed_users=["ana"])
        self.session = mock.MagicMock(name="session")
        self.session.exists.return_value = True
        self.session.is_idle.return_value = True
        self.session.inject.return_value = True
        self.session.fingerprint.return_value = "fp"
        self.post = mock.MagicMock(name="post_fn", return_value=True)
        for patcher in (mock.patch.object(worker, "session", self.session),
                        mock.patch.object(poster, "delete_post", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        spool.save_state(self.cfg, CID, {**spool.load_state(self.cfg, CID), "launch_fingerprint": "fp"})

    def debrief_in_flight(self, operator):
        self.cfg["operator_channel"] = operator
        spool.enqueue(self.cfg, CID, {"post_id": "p9", "user": "ana", "platform": "mattermost",
                                      "text": "why?", "ts": NOW, "control": "debrief"})
        worker.tick(self.cfg, self.post, now=NOW)

    def answer(self, text=ANSWER):
        path = spool.channel_dir(self.cfg, CID) / "outbox" / f"{os.urandom(4).hex()}.json"
        path.write_text(json.dumps({"session_id": None, "text": text, "ts": NOW}), encoding="utf-8")

    def filed(self):
        return sorted((spool.channel_dir(self.cfg, CID) / "debrief").glob("*.md"))


class DebriefBoundaryTests(_WorkerBase):
    def test_cross_team_debrief_is_filed_and_never_posted_to_the_operator(self):
        with mock.patch("urllib.request.urlopen", _teams({CID: "T1", OP_OTHER: "T2"})), \
                self.assertLogs("claude_code_bridge", level=logging.WARNING) as logs:
            self.debrief_in_flight(OP_OTHER)
            self.answer()
            worker.tick(self.cfg, self.post, now=NOW + 5)
        self.post.assert_not_called()  # neither the operator channel nor the bridged channel
        files = self.filed()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(encoding="utf-8"), ANSWER)
        self.assertTrue(any("another team" in line for line in logs.output))
        self.assertIsNone(spool.load_state(self.cfg, CID)["inflight"])  # the turn still ends

    def test_same_team_debrief_is_filed_and_posted_to_the_operator(self):
        with mock.patch("urllib.request.urlopen", _teams({CID: "T1", OP_SAME: "T1"})):
            self.debrief_in_flight(OP_SAME)
            self.answer()
            worker.tick(self.cfg, self.post, now=NOW + 5)
        self.post.assert_called_once_with(OP_SAME, None, ANSWER)
        self.assertEqual(len(self.filed()), 1)

    def test_lookup_failure_files_the_debrief_and_posts_nothing(self):
        with mock.patch("urllib.request.urlopen", _teams({CID: "T1"}, fail=(OP_SAME,))), \
                self.assertLogs("claude_code_bridge", level=logging.WARNING):
            self.debrief_in_flight(OP_SAME)
            self.answer()
            worker.tick(self.cfg, self.post, now=NOW + 5)
        self.post.assert_not_called()
        self.assertEqual(len(self.filed()), 1)

    def test_debrief_without_an_operator_channel_is_only_filed(self):
        self.cfg["operator_channel"] = ""
        spool.enqueue(self.cfg, CID, {"post_id": "p9", "user": "ana", "platform": "mattermost",
                                      "text": "why?", "ts": NOW, "control": "debrief"})
        worker.tick(self.cfg, self.post, now=NOW)
        self.answer()
        worker.tick(self.cfg, self.post, now=NOW + 5)
        self.post.assert_not_called()
        self.assertEqual(len(self.filed()), 1)

    def test_the_file_is_written_once_even_when_the_turn_is_retried(self):
        self.cfg["operator_channel"] = ""
        spool.enqueue(self.cfg, CID, {"post_id": "p9", "user": "ana", "platform": "mattermost",
                                      "text": "why?", "ts": NOW, "control": "debrief"})
        worker.tick(self.cfg, self.post, now=NOW)
        self.answer()
        worker.tick(self.cfg, self.post, now=NOW + 5)
        self.assertEqual(len(self.filed()), 1)

    def test_the_log_names_the_file_but_never_its_content(self):
        self.cfg["operator_channel"] = ""
        spool.enqueue(self.cfg, CID, {"post_id": "p9", "user": "ana", "platform": "mattermost",
                                      "text": "why?", "ts": NOW, "control": "debrief"})
        worker.tick(self.cfg, self.post, now=NOW)
        self.answer()
        with self.assertLogs("claude_code_bridge", level=logging.INFO) as logs:
            worker.tick(self.cfg, self.post, now=NOW + 5)
        joined = "\n".join(logs.output)
        self.assertIn(str(self.filed()[0]), joined)
        self.assertNotIn(ANSWER, joined)


class BlockedNoticeBoundaryTests(_WorkerBase):
    def test_cross_team_blocked_notice_is_not_posted(self):
        self.cfg["operator_channel"] = OP_OTHER
        found = {"question": "Allow?", "options": ["1. Yes"], "exempt": None, "sig": "s"}
        with mock.patch.object(worker.poster, "post", return_value=True) as posted, \
                mock.patch("urllib.request.urlopen", _teams({CID: "T1", OP_OTHER: "T2"})), \
                self.assertLogs("claude_code_bridge", level=logging.WARNING):
            done = worker._notify_operator(self.cfg, CID, found, "notify")
        self.assertTrue(done)  # not retried: nothing is ever posted there
        posted.assert_not_called()

    def test_same_team_blocked_notice_is_posted(self):
        self.cfg["operator_channel"] = OP_SAME
        found = {"question": "Allow?", "options": ["1. Yes"], "exempt": None, "sig": "s"}
        with mock.patch.object(worker.poster, "post", return_value=True) as posted, \
                mock.patch("urllib.request.urlopen", _teams({CID: "T1", OP_SAME: "T1"})), \
                mock.patch.object(worker.poster, "channel_name", return_value="x"):
            worker._notify_operator(self.cfg, CID, found, "notify")
        self.assertEqual(posted.call_args.args[0], OP_SAME)


class OperatorConfigTests(_Env):
    def test_per_channel_override_wins_over_the_global_operator(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg.update(operator_channel="global-op", channels={CID: {"operator_channel": "team-op"}})
        self.assertEqual(config.for_channel(cfg, CID)["operator_channel"], "team-op")
        self.assertEqual(config.for_channel(cfg, "other")["operator_channel"], "global-op")
        self.assertEqual(worker._operator(cfg, CID), "team-op")

    def test_global_operator_channel_warns_at_load(self):
        home = self.tmp / "hermes-home"
        home.mkdir()
        (home / "config.yaml").write_text("claude_code_bridge:\n  operator_channel: some-op\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}), \
                self.assertLogs("claude_code_bridge", level=logging.WARNING) as logs:
            loaded = config.load()
        self.assertEqual(loaded["operator_channel"], "some-op")
        self.assertTrue(any("checked against it at send time" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
