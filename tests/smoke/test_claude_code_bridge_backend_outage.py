#!/usr/bin/env python3
"""Smoke tests for claude-code-bridge: recovery of a turn in flight across a model-backend outage.

A busy or restarted llama-swap backend can leave a Claude Code session alive with a hung request:
it never goes idle, so queued messages wait behind it for the whole wait budget. The worker must
notice the outage while a turn is in flight and, once the backend answers again, relaunch the
session on the same conversation and redeliver that turn exactly once.

No test starts a real ``claude``, tmux or network call: the ``session`` module and the backend
probe are mocks, and each test uses a temporary HERMES_HOME.
"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parents[1] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN_DIR))

from lib import config, spool, worker  # noqa: E402

CID = "chan-outage"
NOW = 1_800_000_000.0
BEFORE_OUTAGE = NOW - 600  # the turn was sent to a live backend ten minutes before the outage
DURING_OUTAGE = NOW + 60   # a turn that starts while the backend is down


class BackendOutageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["home"] = str(Path(self._tmp.name) / "claude-code-bridge")
        self.cfg["allowed_users"] = ["alice"]
        self.cfg["allowed_channels"] = [CID]
        self.session = mock.MagicMock(name="session")
        self.session.exists.return_value = True
        self.session.is_idle.return_value = False  # the turn is still running, as on a hung request
        self.session.inject.return_value = True
        self.session.start.return_value = True
        self.session.fingerprint.return_value = "fp"
        self.post = mock.MagicMock(name="post_fn")
        self.backend = mock.patch.object(worker, "_backend_up", return_value=True)
        self.backend_up = self.backend.start()
        self.addCleanup(self.backend.stop)
        patcher = mock.patch.object(worker, "session", self.session)
        patcher.start()
        self.addCleanup(patcher.stop)
        spool.save_state(self.cfg, CID, {"launch_fingerprint": "fp", "session_id": "sid-1", "started_once": True})

    def set_inflight(self, ts=BEFORE_OUTAGE, **extra):
        item = {"post_id": "p1", "root_id": None, "user": "alice", "text": "first", "ts": ts}
        item.update(extra)
        state = spool.load_state(self.cfg, CID)
        state.update(inflight=item, inflight_since=ts, inflight_redelivered_at=None, backend_down_at=None)
        spool.save_state(self.cfg, CID, state)
        return item

    def state(self):
        return spool.load_state(self.cfg, CID)

    # --- detection -----------------------------------------------------------------------------

    def test_outage_during_turn_is_recorded(self):
        self.set_inflight()
        self.backend_up.return_value = False
        state = self.state()
        worker._note_backend(self.cfg, CID, state, NOW)
        self.assertEqual(state["backend_down_at"], NOW)

    def test_outage_mark_is_kept_at_first_failure(self):
        self.set_inflight()
        self.backend_up.return_value = False
        state = self.state()
        state["backend_down_at"] = NOW - 120
        worker._note_backend(self.cfg, CID, state, NOW)
        self.assertEqual(state["backend_down_at"], NOW - 120)

    def test_no_turn_in_flight_clears_the_outage_mark(self):
        self.backend_up.return_value = False
        state = {"inflight": None, "backend_down_at": NOW}
        worker._note_backend(self.cfg, CID, state, NOW)
        self.assertIsNone(state["backend_down_at"])

    def test_healthy_backend_records_no_outage(self):
        self.set_inflight()
        state = self.state()
        worker._note_backend(self.cfg, CID, state, NOW)
        self.assertIsNone(state["backend_down_at"])

    # --- recovery ------------------------------------------------------------------------------

    def test_recovery_relaunches_and_redelivers_once(self):
        item = self.set_inflight()
        state = self.state()
        state["backend_down_at"] = NOW
        worker._recover_backend_outage(self.cfg, CID, state, NOW + 30)
        self.session.stop.assert_called_once_with(self.cfg, CID)
        self.session.start.assert_called_once()
        self.session.inject.assert_called_once()
        injected = self.session.inject.call_args.args[2]
        self.assertIn("first", injected)
        self.assertEqual(state["inflight_redelivered_at"], NOW + 30)
        self.assertIsNone(state["backend_down_at"])
        self.assertEqual(item["text"], "first")

    def test_recovery_waits_while_backend_is_still_down(self):
        self.set_inflight()
        self.backend_up.return_value = False
        state = self.state()
        state["backend_down_at"] = NOW
        worker._recover_backend_outage(self.cfg, CID, state, NOW + 30)
        self.session.stop.assert_not_called()
        self.session.inject.assert_not_called()
        self.assertEqual(state["backend_down_at"], NOW)

    def test_turn_started_during_the_outage_is_not_redelivered(self):
        self.set_inflight(ts=DURING_OUTAGE)
        state = self.state()
        state["backend_down_at"] = NOW
        worker._recover_backend_outage(self.cfg, CID, state, NOW + 30)
        self.session.stop.assert_not_called()
        self.session.inject.assert_not_called()

    def test_a_second_outage_never_redelivers_the_same_turn_again(self):
        self.set_inflight()
        state = self.state()
        state["backend_down_at"] = NOW
        worker._recover_backend_outage(self.cfg, CID, state, NOW + 30)
        self.session.inject.reset_mock()
        self.session.stop.reset_mock()
        state["backend_down_at"] = NOW + 900
        worker._recover_backend_outage(self.cfg, CID, state, NOW + 960)
        self.session.stop.assert_not_called()
        self.session.inject.assert_not_called()

    def test_failed_redelivery_inject_requeues_the_message(self):
        self.set_inflight()
        self.session.inject.return_value = False
        state = self.state()
        state["backend_down_at"] = NOW
        worker._recover_backend_outage(self.cfg, CID, state, NOW + 30)
        self.assertIsNone(state.get("inflight"))
        queued = spool.pending(self.cfg, CID)
        self.assertEqual(len(queued), 1)
        self.assertEqual(spool.pop(queued[0])["text"], "first")

    # --- through the real tick path -------------------------------------------------------------

    def test_tick_relaunches_a_hung_turn_after_the_backend_returns(self):
        self.set_inflight()
        spool.save_state(self.cfg, CID, {**self.state(), "backend_down_at": NOW})
        worker.tick(self.cfg, self.post, now=NOW + 30)
        self.session.stop.assert_called_once()
        self.session.inject.assert_called_once()
        self.post.assert_not_called()

    def test_tick_does_not_redeliver_a_turn_that_already_finished(self):
        self.set_inflight()
        spool.save_state(self.cfg, CID, {**self.state(), "backend_down_at": NOW})
        self.session.is_idle.return_value = True
        worker.tick(self.cfg, self.post, now=NOW + 30)
        # Idle with no reply: the turn is not hung, so the recovery must leave the session alone.
        self.session.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
