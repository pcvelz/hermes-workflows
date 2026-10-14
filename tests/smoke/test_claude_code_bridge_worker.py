#!/usr/bin/env python3
"""Smoke tests for claude-code-bridge: the dispatch hook and the worker tick.

No test starts a real ``claude``, tmux or network call: the ``session`` module inside
the worker and the ``post_fn`` are mocks, and the worker thread is never started.
Each test uses a temporary HERMES_HOME.
"""
import copy
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN_DIR))

from lib import config, spool, worker  # noqa: E402
from lib import session as real_session  # noqa: E402

CID = "chan1"
NOW = 1_800_000_000.0


def _load_plugin_init():
    """Load plugins/claude-code-bridge/__init__.py as a standalone module (the dir name has a dash)."""
    spec = importlib.util.spec_from_file_location("claude_code_bridge_under_test", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Base(unittest.TestCase):
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
        self.session.inject.return_value = True
        self.session.start.return_value = True
        self.session.fingerprint.return_value = "fp"
        self.post = mock.MagicMock(name="post_fn")
        patcher = mock.patch.object(worker, "session", self.session)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The running session was started with launch fingerprint "fp" (matches the mock).
        spool.save_state(self.cfg, CID, {**spool.load_state(self.cfg, CID), "launch_fingerprint": "fp"})

    def tearDown(self):
        self._tmp.cleanup()

    def enqueue(self, text="hello", post_id="p1", root_id=None):
        return spool.enqueue(self.cfg, CID, {
            "post_id": post_id, "root_id": root_id, "user": "alice", "text": text, "ts": NOW,
        })

    def write_reply(self, text):
        outbox = spool.channel_dir(self.cfg, CID) / "outbox"
        path = outbox / f"{time.time_ns()}.json"
        path.write_text(json.dumps({"session_id": "s", "text": text, "ts": NOW}), encoding="utf-8")

    def set_state(self, **fields):
        state = spool.load_state(self.cfg, CID)
        state.update(fields)
        spool.save_state(self.cfg, CID, state)

    def state(self):
        return spool.load_state(self.cfg, CID)


class DispatchHookTests(_Base):
    def setUp(self):
        super().setUp()
        self.plugin = _load_plugin_init()
        self.hermes_home = self.tmp / "hermes-home"
        self.hermes_home.mkdir()
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.hermes_home)
        self.addCleanup(self._restore_home)
        self.start_thread = mock.patch.object(worker, "start_thread").start()
        self.addCleanup(mock.patch.stopall)

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home

    def _write_config(self, enabled=True, users=("alice",), channels=(CID,)):
        block = {
            "enabled": enabled,
            "allowed_users": list(users),
            "allowed_channels": list(channels),
            "home": self.cfg["home"],
        }
        lines = ["claude_code_bridge:"] + [f"  {k}: {json.dumps(v)}" for k, v in block.items()]
        path = self.hermes_home / "config.yaml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._stamp = getattr(self, "_stamp", 1_700_000_000) + 10
        os.utime(path, ns=(self._stamp * 10**9, self._stamp * 10**9))  # defeat the mtime cache between writes

    def _event(self, platform="mattermost", chat=CID, user="alice", text="hi", root_id="", thread_id=None):
        source = SimpleNamespace(platform=SimpleNamespace(value=platform), chat_id=chat,
                                 user_name=user, thread_id=thread_id)
        return SimpleNamespace(source=source, text=text, message_id="post-1",
                               raw_message={"root_id": root_id})

    def test_returns_none_when_disabled(self):
        self._write_config(enabled=False)
        self.assertIsNone(self.plugin._on_dispatch(event=self._event()))
        self.start_thread.assert_not_called()
        self.assertEqual(spool.pending(self.cfg, CID), [])

    def test_returns_none_for_non_allowed_channel(self):
        self._write_config(channels=(CID,))
        self.assertIsNone(self.plugin._on_dispatch(event=self._event(chat="other-channel")))
        self.start_thread.assert_not_called()

    def test_returns_none_without_platform(self):
        self._write_config()
        self.assertIsNone(self.plugin._on_dispatch(event=self._event(platform="")))
        self.start_thread.assert_not_called()

    def test_bridges_any_platform_on_an_allowed_channel(self):
        # The bridge is platform-agnostic since the multi-platform sender (lib/senders.py).
        self._write_config()
        self.assertEqual(self.plugin._on_dispatch(event=self._event(platform="telegram")), {"action": "skip"})

    def test_wildcard_user_admits_any_sender(self):
        self._write_config(users=("*",))
        result = self.plugin._on_dispatch(event=self._event(user="someone-else", text="ping"))
        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(len(spool.pending(dict(self.cfg), CID)), 1)
        self.start_thread.assert_called_once()

    def test_empty_user_list_admits_no_sender(self):
        self._write_config(users=())
        result = self.plugin._on_dispatch(event=self._event(user="alice", text="ping"))
        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(spool.pending(dict(self.cfg), CID), [])
        self.start_thread.assert_not_called()

    def test_named_user_list_admits_only_listed_senders(self):
        self._write_config(users=("alice",))
        self.plugin._on_dispatch(event=self._event(user="alice", text="ping"))
        self.assertEqual(len(spool.pending(dict(self.cfg), CID)), 1)
        self.plugin._on_dispatch(event=self._event(user="mallory", text="pong"))
        self.assertEqual(len(spool.pending(dict(self.cfg), CID)), 1)

    def test_skip_and_enqueue_for_allowed_user_and_channel(self):
        self._write_config()
        result = self.plugin._on_dispatch(event=self._event(text="ping", root_id="root-9"))
        self.assertEqual(result, {"action": "skip"})
        queued = spool.pending(dict(self.cfg), CID)
        self.assertEqual(len(queued), 1)
        item = spool.pop(queued[0])
        self.assertEqual(item["text"], "ping")
        self.assertEqual(item["user"], "alice")
        self.assertEqual(item["post_id"], "post-1")
        self.assertEqual(item["root_id"], "root-9")
        self.start_thread.assert_called_once()

    def test_skip_without_enqueue_for_unknown_user(self):
        self._write_config()
        result = self.plugin._on_dispatch(event=self._event(user="mallory"))
        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(spool.pending(dict(self.cfg), CID), [])
        self.start_thread.assert_not_called()

    def test_root_id_is_none_for_top_level_message_never_post_id(self):
        self._write_config()
        self.plugin._on_dispatch(event=self._event(root_id="", thread_id=None))
        item = spool.pop(spool.pending(dict(self.cfg), CID)[0])
        self.assertIsNone(item["root_id"])
        self.assertEqual(item["post_id"], "post-1")

    def test_root_id_from_source_thread_id_when_no_raw_root(self):
        self._write_config()
        self.plugin._on_dispatch(event=self._event(root_id="", thread_id="thread-5"))
        item = spool.pop(spool.pending(dict(self.cfg), CID)[0])
        self.assertEqual(item["root_id"], "thread-5")

    def test_register_starts_worker_when_enabled(self):
        self._write_config(enabled=True)
        ctx = mock.MagicMock(name="ctx")
        self.plugin.register(ctx)
        ctx.register_hook.assert_called_once_with("pre_gateway_dispatch", self.plugin._on_dispatch)
        self.start_thread.assert_called_once()

    def test_register_does_not_start_worker_when_disabled(self):
        self._write_config(enabled=False)
        ctx = mock.MagicMock(name="ctx")
        self.plugin.register(ctx)
        ctx.register_hook.assert_called_once_with("pre_gateway_dispatch", self.plugin._on_dispatch)
        self.start_thread.assert_not_called()


class ResetCommandTests(_Base):
    """``/reset`` and ``!reset``: detection in the dispatch hook, handling in the worker tick."""

    def setUp(self):
        super().setUp()
        self.plugin = _load_plugin_init()
        self.cfg["enabled"] = True

    def _decide(self, text, user="alice", channel=CID):
        source = SimpleNamespace(platform=SimpleNamespace(value="mattermost"), chat_id=channel,
                                 user_name=user, thread_id=None)
        event = SimpleNamespace(source=source, text=text, message_id="p9", raw_message={"root_id": ""})
        return self.plugin._decide(event, self.cfg, ensure_worker=False)

    def test_reset_commands_detected_case_and_whitespace_insensitive(self):
        for text in ("/reset", "!reset", "  !RESET ", "/Reset\n"):
            with self.subTest(text=text):
                self.assertEqual(self._decide(text), {"action": "skip"})
                queued = spool.pending(self.cfg, CID)
                self.assertEqual(len(queued), 1)
                self.assertEqual(spool.pop(queued[0])["control"], "reset")

    def test_text_containing_reset_is_an_ordinary_message(self):
        self.assertEqual(self._decide("please /reset the thing"), {"action": "skip"})
        item = spool.pop(spool.pending(self.cfg, CID)[0])
        self.assertNotIn("control", item)

    def test_reset_from_unknown_user_is_dropped(self):
        self.assertEqual(self._decide("!reset", user="mallory"), {"action": "skip"})
        self.assertEqual(spool.pending(self.cfg, CID), [])

    def test_reset_clears_inbox_up_to_control_and_keeps_later_messages(self):
        self.enqueue(text="old one", post_id="p1")
        spool.enqueue(self.cfg, CID, {"control": "reset", "user": "alice", "ts": NOW})
        self.enqueue(text="after reset", post_id="p3")
        self.write_reply("stale reply")
        self.set_state(session_id="old-sid", started_once=True, inflight={"post_id": "px"},
                       inflight_since=NOW - 5)

        worker.tick(self.cfg, self.post, now=NOW)

        self.session.stop.assert_called_once_with(self.cfg, CID)
        self.post.assert_not_called()
        # "after reset" survived the reset and was injected into the new session; "old one" was dropped.
        self.session.inject.assert_called_once_with(self.cfg, CID, f"[{_stamp(NOW)} · alice] after reset")
        self.assertEqual(spool.pending(self.cfg, CID), [])
        self.assertEqual(list((spool.channel_dir(self.cfg, CID) / "outbox").glob("*.json")), [])
        state = self.state()
        self.assertIsNone(state["session_id"])
        self.assertFalse(state["started_once"])
        self.assertEqual(state["inflight"]["post_id"], "p3")  # old in-flight turn was cleared

    def test_next_message_after_reset_starts_a_new_conversation(self):
        self.session.exists.return_value = False
        self.set_state(session_id="old-sid", started_once=True)
        spool.enqueue(self.cfg, CID, {"control": "reset", "user": "alice", "ts": NOW})
        self.enqueue(text="fresh start", post_id="p2")

        worker.tick(self.cfg, self.post, now=NOW)

        started_state = self.session.start.call_args.args[2]
        self.assertIsNotNone(started_state["session_id"])
        self.assertNotEqual(started_state["session_id"], "old-sid")
        self.assertFalse(started_state["started_once"])
        self.session.inject.assert_called_once_with(self.cfg, CID, f"[{_stamp(NOW)} · alice] fresh start")

    def test_failed_session_stop_keeps_reset_queued_for_retry(self):
        spool.enqueue(self.cfg, CID, {"control": "reset", "user": "alice", "ts": NOW})
        self.session.stop.side_effect = RuntimeError("tmux gone")

        worker.tick(self.cfg, self.post, now=NOW)  # error is logged per channel, not raised

        self.assertEqual(len(spool.pending(self.cfg, CID)), 1)
        self.post.assert_not_called()


class ReplyPostRetryTests(_Base):
    """A failed reply post keeps the reply in the outbox and retries it, up to a cap."""

    def _outbox(self):
        return list((spool.channel_dir(self.cfg, CID) / "outbox").glob("*.json"))

    def test_failed_post_keeps_reply_in_outbox_and_inflight(self):
        self.post.return_value = False
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_reply("the answer")

        worker.tick(self.cfg, self.post, now=NOW + 1)

        self.post.assert_called_once_with(CID, "root-1", "the answer")
        self.assertEqual(len(self._outbox()), 1)
        self.assertIsNotNone(self.state()["inflight"])

    def test_retry_on_next_tick_delivers_the_kept_reply(self):
        self.post.side_effect = [False, True]
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_reply("the answer")

        worker.tick(self.cfg, self.post, now=NOW + 1)
        worker.tick(self.cfg, self.post, now=NOW + 3)

        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.post.call_args.args, (CID, "root-1", "the answer"))
        self.assertEqual(self._outbox(), [])
        self.assertIsNone(self.state()["inflight"])

    def test_raising_post_is_treated_as_failure_and_retried(self):
        self.post.side_effect = [RuntimeError("network down"), True]
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"}, inflight_since=NOW)
        self.write_reply("survives the crash")

        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.assertEqual(len(self._outbox()), 1)
        worker.tick(self.cfg, self.post, now=NOW + 3)

        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self._outbox(), [])
        self.assertIsNone(self.state()["inflight"])

    # --- ---post--- split: each part is its own post, in order, on the same thread ---

    def test_split_marker_posts_each_part_in_order_on_the_same_thread(self):
        self.post.return_value = True
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_reply("one\n---post---\ntwo\n---post---\nthree")

        worker.tick(self.cfg, self.post, now=NOW + 1)

        self.assertEqual([c.args for c in self.post.call_args_list],
                         [(CID, "root-1", "one"), (CID, "root-1", "two"), (CID, "root-1", "three")])
        self.assertEqual(self._outbox(), [])
        self.assertIsNone(self.state()["inflight"])

    def test_split_drops_empty_parts(self):
        self.post.return_value = True
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"}, inflight_since=NOW)
        self.write_reply("a\n---post---\n\n   \n---post---\nb\n---post---\n")

        worker.tick(self.cfg, self.post, now=NOW + 1)

        self.assertEqual([c.args[2] for c in self.post.call_args_list], ["a", "b"])

    def test_marker_must_be_a_whole_line(self):
        self.post.return_value = True
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"}, inflight_since=NOW)
        self.write_reply("use ---post--- inline, not a split")

        worker.tick(self.cfg, self.post, now=NOW + 1)

        self.post.assert_called_once_with(CID, None, "use ---post--- inline, not a split")

    def test_partial_failure_resumes_at_the_failed_part_without_reposting(self):
        self.post.side_effect = [True, False]
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_reply("a\n---post---\nb\n---post---\nc")

        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.assertEqual(len(self._outbox()), 1)
        self.assertIsNotNone(self.state()["inflight"])

        self.post.side_effect = [True, True]
        worker.tick(self.cfg, self.post, now=NOW + 3)

        self.assertEqual([c.args[2] for c in self.post.call_args_list], ["a", "b", "b", "c"])
        self.assertEqual(self._outbox(), [])
        self.assertIsNone(self.state()["inflight"])


def _stamp(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class FormatMessageTests(unittest.TestCase):
    def test_prefixes_local_time_and_sender(self):
        text = worker.format_message({"user": "alice", "text": "hi", "ts": NOW})
        self.assertEqual(text, f"[{_stamp(NOW)} · alice] hi")

    def test_falls_back_to_now_without_ts(self):
        before = time.time()
        text = worker.format_message({"user": "alice", "text": "hi"})
        after = time.time()
        self.assertIn(_stamp(before), (text[1:17], _stamp(after)))
        self.assertTrue(text.endswith("· alice] hi"))


class WorkerTickTests(_Base):
    def test_injects_formatted_text(self):
        self.enqueue(text="what is up")
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.inject.assert_called_once_with(self.cfg, CID, f"[{_stamp(NOW)} · alice] what is up")
        self.post.assert_not_called()

    def test_injects_when_idle(self):
        self.enqueue(text="what is up")
        worker.tick(self.cfg, self.post, now=NOW)
        self.post.assert_not_called()
        state = self.state()
        self.assertEqual(state["inflight"]["text"], "what is up")
        self.assertEqual(state["inflight_since"], NOW)
        self.assertEqual(spool.pending(self.cfg, CID), [])

    def test_does_not_inject_when_busy(self):
        self.session.is_idle.return_value = False
        self.enqueue()
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.inject.assert_not_called()
        self.assertEqual(len(spool.pending(self.cfg, CID)), 1)

    def test_posts_reply_to_root_id(self):
        self.set_state(inflight={"post_id": "p1", "root_id": "root-7", "text": "q"}, inflight_since=NOW)
        self.write_reply("the answer")
        worker.tick(self.cfg, self.post, now=NOW + 5)
        self.post.assert_called_once_with(CID, "root-7", "the answer")
        state = self.state()
        self.assertIsNone(state["inflight"])
        self.assertEqual(state["last_activity"], NOW + 5)

    def test_posts_top_level_when_no_root_and_not_reply_in_thread(self):
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"}, inflight_since=NOW)
        self.write_reply("second answer")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, None, "second answer")

    def test_posts_in_thread_on_post_id_when_reply_in_thread(self):
        self.cfg["reply_in_thread"] = True
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"}, inflight_since=NOW)
        self.write_reply("threaded answer")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, "p1", "threaded answer")

    def test_user_thread_root_wins_over_reply_in_thread_false(self):
        self.set_state(inflight={"post_id": "p2", "root_id": "root-8", "text": "q"}, inflight_since=NOW)
        self.write_reply("in their thread")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, "root-8", "in their thread")

    def test_wait_budget_failure_is_top_level_without_root(self):
        budget_s = self.cfg["wait_budget_minutes"] * 60
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"},
                       inflight_since=NOW - budget_s - 1)
        worker.tick(self.cfg, self.post, now=NOW)
        self.post.assert_called_once_with(CID, None, worker.WAIT_FAILURE_TEXT)

    def test_drops_reply_with_no_inflight(self):
        self.write_reply("stray")
        worker.tick(self.cfg, self.post, now=NOW)
        self.post.assert_not_called()
        self.assertEqual(list((spool.channel_dir(self.cfg, CID) / "outbox").glob("*.json")), [])

    def test_wait_budget_failure_post(self):
        budget_s = self.cfg["wait_budget_minutes"] * 60
        self.set_state(inflight={"post_id": "p1", "root_id": "root-3", "text": "q"},
                       inflight_since=NOW - budget_s - 1)
        worker.tick(self.cfg, self.post, now=NOW)
        self.post.assert_called_once_with(CID, "root-3", worker.WAIT_FAILURE_TEXT)
        self.assertIsNone(self.state()["inflight"])

    def test_no_failure_inside_wait_budget(self):
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"}, inflight_since=NOW)
        worker.tick(self.cfg, self.post, now=NOW + 60)
        self.post.assert_not_called()
        self.assertIsNotNone(self.state()["inflight"])

    def test_idle_exit_stops_session(self):
        idle_s = self.cfg["idle_exit_minutes"] * 60
        self.set_state(last_activity=NOW - idle_s - 1)
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.stop.assert_called_once_with(self.cfg, CID)

    def test_no_idle_exit_while_recent(self):
        self.set_state(last_activity=NOW - 10)
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.stop.assert_not_called()

    def test_first_item_starts_session(self):
        self.session.exists.return_value = False
        self.enqueue(text="first")
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.start.assert_called_once()
        args = self.session.start.call_args[0]
        self.assertEqual(args[1], CID)
        self.assertTrue(args[2]["session_id"])
        self.session.inject.assert_called_once_with(self.cfg, CID, f"[{_stamp(NOW)} · alice] first")
        self.assertTrue(self.state()["session_id"])

    def test_failed_start_leaves_item_queued(self):
        self.session.exists.return_value = False
        self.session.start.return_value = False
        self.enqueue(text="later")
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.inject.assert_not_called()
        self.assertEqual(len(spool.pending(self.cfg, CID)), 1)

    def test_failed_inject_requeues_item(self):
        self.session.inject.return_value = False
        self.enqueue(text="retry me")
        worker.tick(self.cfg, self.post, now=NOW)
        queued = spool.pending(self.cfg, CID)
        self.assertEqual(len(queued), 1)
        self.assertEqual(spool.pop(queued[0])["text"], "retry me")
        self.assertIsNone(self.state()["inflight"])

    def test_re_enqueued_item_keeps_original_order(self):
        self.session.inject.return_value = False
        first = self.enqueue(text="first", post_id="p1")
        second = self.enqueue(text="second", post_id="p2")
        worker.tick(self.cfg, self.post, now=NOW)
        queued = spool.pending(self.cfg, CID)
        self.assertEqual([p.name for p in queued], [first.name, second.name])
        self.assertEqual(spool.pop(queued[0])["text"], "first")


class NoticeTests(_Base):
    """An outbox item with "notice": true is posted top-level with nothing in flight, state untouched."""

    def _outbox(self):
        return list((spool.channel_dir(self.cfg, CID) / "outbox").glob("*.json"))

    def write_notice(self, text):
        outbox = spool.channel_dir(self.cfg, CID) / "outbox"
        path = outbox / f"{time.time_ns()}.json"
        path.write_text(json.dumps({"text": text, "notice": True}), encoding="utf-8")

    def test_notice_posted_top_level_with_nothing_in_flight(self):
        self.write_notice("heads up")
        before = self.state()
        worker.tick(self.cfg, self.post, now=NOW)
        self.post.assert_called_once_with(CID, None, "heads up")
        self.assertEqual(self._outbox(), [])
        self.assertEqual(self.state(), before)

    def test_notice_does_not_consume_the_in_flight_reply(self):
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_notice("heads up")
        self.write_reply("the answer")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.assertEqual(self.post.call_args_list[0].args, (CID, None, "heads up"))
        self.assertEqual(self.post.call_args_list[1].args, (CID, "root-1", "the answer"))
        self.assertIsNone(self.state()["inflight"])

    def test_failed_notice_is_parked_at_the_cap_and_kept(self):
        self.post.return_value = False
        self.write_notice("retry me")
        now = NOW
        for _ in range(worker.MAX_REPLY_POST_ATTEMPTS):
            worker.tick(self.cfg, self.post, now=now)
            now += worker.POST_RETRY_MAX_S + 1
        self.assertEqual(self.post.call_count, worker.MAX_REPLY_POST_ATTEMPTS)
        self.assertEqual(len(self._outbox()), 1)
        worker.tick(self.cfg, self.post, now=now + 99)
        self.assertEqual(self.post.call_count, worker.MAX_REPLY_POST_ATTEMPTS)

    def test_notice_alone_is_not_a_reply(self):
        self.write_notice("heads up")
        self.assertIsNone(spool.take_reply(self.cfg, CID))
        self.assertEqual(len(self._outbox()), 1)


class AdapterWaitTests(_Base):
    """A missing adapter is a wait, not a failed post: the reply is held, no attempt is used."""

    def _outbox(self):
        return list((spool.channel_dir(self.cfg, CID) / "outbox").glob("*.json"))

    def _held(self):
        return json.loads(self._outbox()[0].read_text(encoding="utf-8"))

    def test_adapter_missing_for_30s_then_present_delivers_without_using_attempts(self):
        self.post.side_effect = [None] * 16 + [True]  # 2 s ticks: missing for t=0..30, live at t=32
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_reply("the answer")
        for i in range(16):
            worker.tick(self.cfg, self.post, now=NOW + 2 * i)
        self.assertEqual(len(self._outbox()), 1)
        self.assertIn(self._held().get("post_attempts"), (None, 0))
        self.assertIsNotNone(self.state()["inflight"])

        worker.tick(self.cfg, self.post, now=NOW + 32)

        self.assertEqual(self.post.call_count, 17)
        self.assertEqual(self.post.call_args.args, (CID, "root-1", "the answer"))
        self.assertEqual(self._outbox(), [])
        self.assertIsNone(self.state()["inflight"])

    def test_missing_adapter_never_parks_the_reply_inside_the_wait_budget(self):
        self.post.return_value = None
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"}, inflight_since=NOW)
        self.write_reply("held")
        for i in range(200):  # far more ticks than the attempt cap
            worker.tick(self.cfg, self.post, now=NOW + i)
        self.assertEqual(len(self._outbox()), 1)
        self.assertNotIn("parked", self._held())
        self.assertIsNotNone(self.state()["inflight"])

    def test_missing_adapter_past_the_wait_budget_parks_the_reply_with_one_error(self):
        self.cfg["wait_budget_minutes"] = 1
        self.post.return_value = None
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_reply("late")
        worker.tick(self.cfg, self.post, now=NOW)
        worker.tick(self.cfg, self.post, now=NOW + 30)
        self.assertIsNotNone(self.state()["inflight"])
        with self.assertLogs("claude_code_bridge", level="ERROR") as logs:
            worker.tick(self.cfg, self.post, now=NOW + 61)
        self.assertEqual(len(logs.records), 1)
        self.assertIsNone(self.state()["inflight"])
        held = self._held()
        self.assertEqual((held["text"], held["parked"]), ("late", True))
        worker.tick(self.cfg, self.post, now=NOW + 62)
        self.assertEqual(self.post.call_count, 3)

    def test_multipart_reply_resumes_at_the_part_whose_adapter_was_missing(self):
        self.post.side_effect = [True, None, True, True]
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_reply("a\n---post---\nb\n---post---\nc")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.assertEqual(self._held()["text"], "b\n---post---\nc")
        worker.tick(self.cfg, self.post, now=NOW + 2)
        self.assertEqual([c.args[2] for c in self.post.call_args_list], ["a", "b", "b", "c"])
        self.assertEqual(self._outbox(), [])
        self.assertIsNone(self.state()["inflight"])


class HttpBackoffTests(_Base):
    """A real post failure backs off 2 s, 4 s, ... capped at 5 min, and is parked at the attempt cap."""

    def _outbox(self):
        return list((spool.channel_dir(self.cfg, CID) / "outbox").glob("*.json"))

    def _held(self):
        return json.loads(self._outbox()[0].read_text(encoding="utf-8"))

    def test_failures_wait_before_each_retry_and_double(self):
        self.post.return_value = False
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_reply("the answer")
        worker.tick(self.cfg, self.post, now=NOW)          # attempt 1 fails -> retry at +2 s
        worker.tick(self.cfg, self.post, now=NOW + 1)      # too early: no post
        self.assertEqual(self.post.call_count, 1)
        worker.tick(self.cfg, self.post, now=NOW + 2)      # attempt 2 fails -> retry at +4 s
        self.assertEqual(self.post.call_count, 2)
        worker.tick(self.cfg, self.post, now=NOW + 5)
        self.assertEqual(self.post.call_count, 2)
        worker.tick(self.cfg, self.post, now=NOW + 6)
        self.assertEqual(self.post.call_count, 3)

    def test_failure_delays_double_and_cap_at_five_minutes(self):
        self.post.return_value = False
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"}, inflight_since=NOW)
        self.write_reply("the answer")
        now = NOW
        delays = []
        for _ in range(12):
            worker.tick(self.cfg, self.post, now=now)
            retry_at = self._held()["retry_at"]
            delays.append(retry_at - now)
            now = retry_at
        self.assertEqual(delays, [2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300, 300])

    def test_raising_post_backs_off_like_a_failed_post(self):
        self.post.side_effect = RuntimeError("network down")
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"}, inflight_since=NOW)
        self.write_reply("the answer")
        worker.tick(self.cfg, self.post, now=NOW)
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.assertEqual(self.post.call_count, 1)

    def test_cap_keeps_reply_in_outbox_with_one_error_and_stops_posting(self):
        self.post.return_value = False
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_reply("kept for later")
        now = NOW
        with self.assertLogs("claude_code_bridge", level="ERROR") as logs:
            for _ in range(worker.MAX_REPLY_POST_ATTEMPTS):
                worker.tick(self.cfg, self.post, now=now)
                now += worker.POST_RETRY_MAX_S + 1
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(self.post.call_count, worker.MAX_REPLY_POST_ATTEMPTS)
        self.assertIsNone(self.state()["inflight"])
        held = self._held()
        self.assertEqual((held["text"], held["parked"], held["thread_root"]), ("kept for later", True, "root-1"))
        worker.tick(self.cfg, self.post, now=now + 10_000)
        self.assertEqual(self.post.call_count, worker.MAX_REPLY_POST_ATTEMPTS)

    def test_parked_reply_is_posted_on_its_thread_after_a_restart(self):
        self.post.return_value = False
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "text": "q"}, inflight_since=NOW)
        self.write_reply("kept for later")
        now = NOW
        for _ in range(worker.MAX_REPLY_POST_ATTEMPTS):
            worker.tick(self.cfg, self.post, now=now)
            now += worker.POST_RETRY_MAX_S + 1
        self.assertTrue(self._held()["parked"])

        self.post.return_value = True
        self.post.reset_mock()
        worker._unpark_all(self.cfg)
        worker.tick(self.cfg, self.post, now=now + 1)

        self.post.assert_called_once_with(CID, "root-1", "kept for later")
        self.assertEqual(self._outbox(), [])


class PassthroughTests(_Base):
    def setUp(self):
        super().setUp()
        self.plugin = _load_plugin_init()
        self.cfg["enabled"] = True

    def _decide(self, text, user="alice", channel=CID):
        source = SimpleNamespace(platform=SimpleNamespace(value="mattermost"), chat_id=channel,
                                 user_name=user, thread_id=None)
        event = SimpleNamespace(source=source, text=text, message_id="p9", raw_message={"root_id": ""})
        return self.plugin._decide(event, self.cfg, ensure_worker=False)

    def test_investigation_text_passes_through_without_enqueue(self):
        for text in ("Investigate briefly: top cities", "Investigation of the top cities",
                     "I want to research zebras", "Deep Research on X"):
            with self.subTest(text=text):
                self.assertIsNone(self._decide(text))
        self.assertEqual(spool.pending(self.cfg, CID), [])

    def test_other_languages_are_added_through_passthrough_patterns(self):
        # The default is English. A deployment adds its own trigger words in its config.
        self.assertIsNotNone(self._decide("please debrief-me on the router"), "default does not cover it")
        self.cfg["passthrough_patterns"] = list(config.DEFAULTS["passthrough_patterns"]) + [r"(?i)\bdebrief-me\b"]
        self.assertIsNone(self._decide("please debrief-me on the router"))

    def test_non_matching_text_is_bridged(self):
        self.assertEqual(self._decide("what time is dinner"), {"action": "skip"})
        self.assertEqual(len(spool.pending(self.cfg, CID)), 1)

    def test_custom_pattern_list_from_config(self):
        self.cfg["passthrough_patterns"] = [r"\bzebra\b"]
        self.assertIsNone(self._decide("a Zebra fact"))
        self.assertEqual(self._decide("investigate this"), {"action": "skip"})
        self.assertEqual(len(spool.pending(self.cfg, CID)), 1)

    def test_string_form_pattern_list_is_parsed(self):
        self.cfg["passthrough_patterns"] = config._as_list(r'["\\bzebra\\b"]')
        self.assertIsNone(self._decide("a zebra"))
        self.assertEqual(self._decide("investigate"), {"action": "skip"})

    def test_invalid_pattern_is_ignored(self):
        self.cfg["passthrough_patterns"] = ["(unclosed", r"\bzebra\b"]
        self.assertIsNone(self._decide("a zebra"))
        self.assertEqual(self._decide("hello"), {"action": "skip"})

    def _raw_event(self, text, root_id=""):
        source = SimpleNamespace(platform=SimpleNamespace(value="mattermost"), chat_id=CID,
                                 user_name="alice", thread_id=None)
        event = SimpleNamespace(source=source, text=text, message_id="p9", raw_message={"root_id": root_id})
        return event, source

    def test_passthrough_threads_reply_under_the_investigation_post(self):
        event, source = self._raw_event("investigate zebras")
        self.assertIsNone(self.plugin._decide(event, self.cfg, ensure_worker=False))
        self.assertEqual(source.thread_id, "p9")

    def test_passthrough_keeps_existing_thread_root(self):
        event, source = self._raw_event("investigate zebras", root_id="root7")
        self.assertIsNone(self.plugin._decide(event, self.cfg, ensure_worker=False))
        self.assertEqual(source.thread_id, "root7")

    def test_non_passthrough_message_does_not_set_thread(self):
        event, source = self._raw_event("what time is dinner")
        self.assertEqual(self.plugin._decide(event, self.cfg, ensure_worker=False), {"action": "skip"})
        self.assertIsNone(source.thread_id)

    def test_reset_is_not_affected_by_passthrough(self):
        self.cfg["passthrough_patterns"] = [r"reset"]
        self.assertEqual(self._decide("!reset"), {"action": "skip"})
        queued = spool.pending(self.cfg, CID)
        self.assertEqual(spool.pop(queued[0])["control"], "reset")

    def test_passthrough_reply_in_thread_default_threads_main_post(self):
        self.assertTrue(config.DEFAULTS["passthrough_reply_in_thread"])
        event, source = self._raw_event("investigate zebras")
        self.assertIsNone(self.plugin._decide(event, self.cfg, ensure_worker=False))
        self.assertEqual(source.thread_id, "p9")

    def test_passthrough_reply_where_written_main_post_stays_main(self):
        self.cfg["passthrough_reply_in_thread"] = False
        event, source = self._raw_event("investigate zebras")
        self.assertIsNone(self.plugin._decide(event, self.cfg, ensure_worker=False))
        self.assertIsNone(source.thread_id)

    def test_passthrough_reply_where_written_thread_post_stays_in_thread(self):
        self.cfg["passthrough_reply_in_thread"] = False
        event, source = self._raw_event("investigate zebras", root_id="root7")
        self.assertIsNone(self.plugin._decide(event, self.cfg, ensure_worker=False))
        self.assertEqual(source.thread_id, "root7")

    def test_passthrough_reply_where_written_thread_root_is_a_bot_post(self):
        # a user reply under a bot post: the thread root is the bot's post id and is kept
        self.cfg["passthrough_reply_in_thread"] = False
        event, source = self._raw_event("investigate zebras", root_id="botpost1")
        self.assertIsNone(self.plugin._decide(event, self.cfg, ensure_worker=False))
        self.assertEqual(source.thread_id, "botpost1")


class LaunchFingerprintRestartTests(_Base):
    """The worker restarts an idle session with --resume when its launch fingerprint changed."""

    def setUp(self):
        super().setUp()
        # Use the real fingerprint so config changes are detected; session start/stop/inject stay mocked.
        self.session.fingerprint.side_effect = lambda cfg, cid: real_session.fingerprint(cfg, cid)
        keys = self.tmp / "keys" / "svc"
        keys.mkdir(parents=True)
        (keys / "api-key").write_text("key-one\n", encoding="utf-8")
        self.cfg["keys_dir"] = str(self.tmp / "keys")
        self.cfg["mcp_servers"] = {"gmail": {"type": "http", "url": "https://mcp.example.test/a",
                                             "headers": {"x-api-key": "keyfile:svc/api-key"}}}
        self.cfg["mcp_allowed_tools"] = ["mcp__gmail__FETCH"]
        self.set_state(launch_fingerprint=real_session.fingerprint(self.cfg, CID))

    def _tick_with_message(self):
        self.enqueue(text="next")
        worker.tick(self.cfg, self.post, now=NOW)

    def test_same_config_does_not_restart(self):
        self._tick_with_message()
        self.session.stop.assert_not_called()
        self.session.start.assert_not_called()
        self.session.inject.assert_called_once()

    def test_changed_allowed_tools_restarts_with_resume(self):
        self.cfg["allowed_tools"] = ["Read"]
        self.set_state(started_once=True)
        with self.assertLogs("claude_code_bridge", level="INFO") as logs:
            self._tick_with_message()
        self.session.stop.assert_called_once_with(self.cfg, CID)
        self.session.start.assert_called_once()
        self.assertTrue(self.session.start.call_args[0][2]["started_once"])
        self.assertTrue(any("launch config changed; restarting session (context kept)" in m
                            for m in logs.output))
        self.session.inject.assert_called_once()
        self.assertEqual(self.state()["launch_fingerprint"], real_session.fingerprint(self.cfg, CID))

    def test_changed_mcp_server_restarts(self):
        self.cfg["mcp_servers"]["gmail"]["url"] = "https://mcp.example.test/b"
        self.set_state(started_once=True)
        self._tick_with_message()
        self.session.stop.assert_called_once_with(self.cfg, CID)
        self.session.start.assert_called_once()

    def test_changed_mcp_key_name_restarts(self):
        self.cfg["mcp_servers"] = {"gmail2": self.cfg["mcp_servers"]["gmail"]}
        self.set_state(started_once=True)
        self._tick_with_message()
        self.session.stop.assert_called_once_with(self.cfg, CID)
        self.session.start.assert_called_once()

    def test_unknown_stored_fingerprint_restarts_once(self):
        self.set_state(launch_fingerprint=None, started_once=True)
        self._tick_with_message()
        self.session.stop.assert_called_once_with(self.cfg, CID)
        self.assertEqual(self.state()["launch_fingerprint"], real_session.fingerprint(self.cfg, CID))


class RecordUserInputTests(_Base):
    FILE = "CLAUDE.md"
    START = "<!-- @user-input:start -->"
    END = "<!-- @user-input:end -->"

    def setUp(self):
        super().setUp()
        self.cfg["channels"] = {CID: {"record_user_input": {"file": self.FILE, "exclude_users": ["bot"]}}}
        self.doc = spool.channel_dir(self.cfg, CID) / self.FILE
        self.doc.write_text(
            "# Channel\n\nIntro text.\n\n## Parent instructions\n"
            f"{self.START}\n- (none yet)\n{self.END}\n\n## Tail\nkeep me\n",
            encoding="utf-8",
        )

    def body(self):
        text = self.doc.read_text(encoding="utf-8")
        return text[text.index(self.START) + len(self.START):text.index(self.END)]

    def stamp(self):
        return _stamp(NOW)

    def test_appends_line_and_replaces_none_yet(self):
        self.assertTrue(worker.record_user_input(self.cfg, CID, {"user": "parent", "text": "Set the date  now", "ts": NOW}))
        self.assertEqual(self.body(), f"\n- {self.stamp()} @user-input (parent): Set the date now\n")

    def test_second_line_is_appended_and_surroundings_kept(self):
        worker.record_user_input(self.cfg, CID, {"user": "parent", "text": "uno", "ts": NOW})
        worker.record_user_input(self.cfg, CID, {"user": "parent", "text": "dos", "ts": NOW + 60})
        lines = [ln for ln in self.body().splitlines() if ln.strip()]
        self.assertEqual(lines, [f"- {self.stamp()} @user-input (parent): uno",
                                 f"- {_stamp(NOW + 60)} @user-input (parent): dos"])
        text = self.doc.read_text(encoding="utf-8")
        self.assertIn("Intro text.", text)
        self.assertTrue(text.endswith("## Tail\nkeep me\n"))

    def test_excluded_user_is_not_recorded(self):
        self.assertFalse(worker.record_user_input(self.cfg, CID, {"user": "bot", "text": "x", "ts": NOW}))
        self.assertIn("(none yet)", self.body())

    def test_control_item_is_not_recorded(self):
        self.assertFalse(worker.record_user_input(self.cfg, CID, {"user": "parent", "text": "/reset", "control": "reset", "ts": NOW}))
        self.assertIn("(none yet)", self.body())


    def test_no_rule_means_no_write(self):
        self.cfg["channels"] = {}
        before = self.doc.read_text(encoding="utf-8")
        self.assertFalse(worker.record_user_input(self.cfg, CID, {"user": "parent", "text": "x", "ts": NOW}))
        self.assertEqual(self.doc.read_text(encoding="utf-8"), before)

    def test_missing_markers_write_nothing(self):
        before = "no markers here\n"
        self.doc.write_text(before, encoding="utf-8")
        self.assertFalse(worker.record_user_input(self.cfg, CID, {"user": "parent", "text": "x", "ts": NOW}))
        self.assertEqual(self.doc.read_text(encoding="utf-8"), before)

    def test_missing_file_writes_nothing(self):
        self.doc.unlink()
        self.assertFalse(worker.record_user_input(self.cfg, CID, {"user": "parent", "text": "x", "ts": NOW}))
        self.assertFalse(self.doc.exists())

    def test_same_item_recorded_once_when_requeued(self):
        self.enqueue(text="once more", post_id="p9")
        self.session.inject.return_value = False  # first tick: inject fails, item re-queued
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.inject.return_value = True
        worker.tick(self.cfg, self.post, now=NOW + 5)
        lines = [ln for ln in self.body().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("(alice): once more"))

    def test_tick_records_then_injects(self):
        self.enqueue(text="remind me")
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.inject.assert_called_once_with(self.cfg, CID, f"[{_stamp(NOW)} · alice] remind me")
        self.assertIn(f"- {self.stamp()} @user-input (alice): remind me", self.body())


class StartBackoffTests(_Base):
    """A failed session start backs off per channel (2 s, 4 s, ... capped at 300 s) and resets on success."""

    def setUp(self):
        super().setUp()
        self.session.exists.return_value = False
        self.session.start.return_value = False
        self.enqueue(text="queued")

    def test_failed_start_schedules_retry_after_two_seconds(self):
        worker.tick(self.cfg, self.post, now=NOW)
        state = self.state()
        self.assertEqual(state["start_failures"], 1)
        self.assertEqual(state["start_retry_at"], NOW + 2)

    def test_no_start_attempt_before_retry_time(self):
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.start.reset_mock()
        worker.tick(self.cfg, self.post, now=NOW + 1.9)
        self.session.start.assert_not_called()

    def test_retry_happens_when_timer_expires(self):
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.start.reset_mock()
        worker.tick(self.cfg, self.post, now=NOW + 2)
        self.session.start.assert_called_once()

    def test_delay_doubles_on_each_failure(self):
        now = NOW
        delays = []
        for _ in range(4):
            worker.tick(self.cfg, self.post, now=now)
            retry_at = self.state()["start_retry_at"]
            delays.append(retry_at - now)
            now = retry_at
        self.assertEqual(delays, [2, 4, 8, 16])

    def test_delay_capped_at_five_minutes(self):
        self.set_state(start_failures=20)
        worker.tick(self.cfg, self.post, now=NOW)
        self.assertEqual(self.state()["start_retry_at"] - NOW, 300)

    def test_success_resets_backoff(self):
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.start.return_value = True
        worker.tick(self.cfg, self.post, now=NOW + 2)
        state = self.state()
        self.assertEqual(state["start_failures"], 0)
        self.assertIsNone(state["start_retry_at"])
        self.session.inject.assert_called_once()


class InflightRecoveryTests(_Base):
    """An in-flight turn whose tmux session is gone (reboot, dead tmux server) is resumed once."""

    def setUp(self):
        super().setUp()
        self.session.exists.return_value = False
        self.inflight = {"post_id": "i1", "root_id": None, "user": "alice", "text": "Yes", "ts": NOW}
        self.set_state(inflight=self.inflight, inflight_since=NOW - 600, session_id="sid-1", started_once=True)
        self.expected_text = f"[{_stamp(NOW)} · alice] Yes"

    def test_session_gone_relaunches_resuming_and_injects_once(self):
        with self.assertLogs("claude_code_bridge", level="INFO") as logs:
            worker.tick(self.cfg, self.post, now=NOW)
        self.session.start.assert_called_once()
        args = self.session.start.call_args[0]
        self.assertEqual(args[2]["session_id"], "sid-1")
        self.assertTrue(args[2]["started_once"])
        self.session.inject.assert_called_once_with(self.cfg, CID, self.expected_text)
        self.assertTrue(any(f"resumed in-flight turn on channel {CID} after session loss" in m
                            for m in logs.output))
        state = self.state()
        self.assertEqual(state["inflight"], self.inflight)
        self.assertEqual(state["inflight_redelivered_at"], NOW)
        self.post.assert_not_called()

    def test_second_tick_does_not_inject_again(self):
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.start.reset_mock()
        self.session.inject.reset_mock()
        worker.tick(self.cfg, self.post, now=NOW + 2)
        self.session.start.assert_not_called()
        self.session.inject.assert_not_called()

    def test_second_session_loss_after_redelivery_does_not_inject_again(self):
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.start.reset_mock()
        self.session.inject.reset_mock()
        worker.tick(self.cfg, self.post, now=NOW + 60)
        self.session.inject.assert_not_called()
        self.assertIsNotNone(self.state()["inflight"])

    def test_session_present_does_nothing(self):
        self.session.exists.return_value = True
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.start.assert_not_called()
        self.session.inject.assert_not_called()
        self.assertIsNone(self.state().get("inflight_redelivered_at"))
        self.assertEqual(self.state()["inflight"], self.inflight)

    def test_no_inflight_and_session_gone_does_nothing(self):
        self.set_state(inflight=None, inflight_since=None)
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.start.assert_not_called()
        self.session.inject.assert_not_called()

    def test_reply_in_outbox_is_posted_not_redelivered(self):
        self.write_reply("the answer")
        worker.tick(self.cfg, self.post, now=NOW)
        self.post.assert_called_once_with(CID, None, "the answer")
        self.session.start.assert_not_called()
        self.session.inject.assert_not_called()
        self.assertIsNone(self.state()["inflight"])

    def test_reply_flows_back_after_redelivery_and_clears_inflight(self):
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.exists.return_value = True
        self.write_reply("resumed answer")
        worker.tick(self.cfg, self.post, now=NOW + 30)
        self.post.assert_called_once_with(CID, None, "resumed answer")
        state = self.state()
        self.assertIsNone(state["inflight"])
        self.assertIsNone(state["inflight_since"])

    def test_backoff_is_respected(self):
        self.set_state(start_retry_at=NOW + 10)
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.start.assert_not_called()
        self.session.inject.assert_not_called()
        worker.tick(self.cfg, self.post, now=NOW + 10)
        self.session.start.assert_called_once()
        self.session.inject.assert_called_once_with(self.cfg, CID, self.expected_text)

    def test_failed_relaunch_backs_off_and_does_not_inject(self):
        self.session.start.return_value = False
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.inject.assert_not_called()
        state = self.state()
        self.assertEqual(state["start_failures"], 1)
        self.assertEqual(state["start_retry_at"], NOW + 2)
        self.assertIsNone(state.get("inflight_redelivered_at"))
        self.assertEqual(state["inflight"], self.inflight)

    def test_failed_redelivery_inject_requeues_the_item_and_clears_marker(self):
        self.session.inject.return_value = False
        worker.tick(self.cfg, self.post, now=NOW)
        state = self.state()
        self.assertIsNone(state["inflight"])
        self.assertIsNone(state["inflight_redelivered_at"])
        queued = spool.pending(self.cfg, CID)
        self.assertEqual(len(queued), 1)
        self.assertEqual(spool.pop(queued[0])["text"], "Yes")

    def test_new_normal_inject_resets_the_redelivery_marker(self):
        worker.tick(self.cfg, self.post, now=NOW)
        self.session.exists.return_value = True
        self.write_reply("done")
        worker.tick(self.cfg, self.post, now=NOW + 30)
        self.enqueue(text="next", post_id="p9")
        worker.tick(self.cfg, self.post, now=NOW + 40)
        self.assertEqual(self.state()["inflight"]["text"], "next")
        self.assertIsNone(self.state()["inflight_redelivered_at"])


GIVE_UP = "Right now I cannot check it, please try again later."
NUDGE = "Your reply was not sent: it gives up instead of delivering. Do not apologise or explain. Continue the task now and post the best result you can in the required format."
GUARD = {
    "patterns": [r"(?i)cannot check", r"(?i)cannot get it", r"(?i)in a while", r"(?i)later",
                 r"(?i)try again later", r"(?i)can'?t right now"],
    "nudge": NUDGE,
    "max_nudges": 2,
}


class ReplyGuardWorkerTests(_Base):
    """The worker has no reply guard: the Stop hook blocks a give-up reply before it reaches the outbox."""

    def setUp(self):
        super().setUp()
        self.cfg["reply_guard"] = copy.deepcopy(GUARD)
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "user": "alice", "text": "q"},
                       inflight_since=NOW)

    def test_worker_posts_a_give_up_reply_unchanged_and_never_nudges(self):
        self.write_reply(GIVE_UP)
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, "root-1", GIVE_UP)
        self.session.inject.assert_not_called()
        self.assertIsNone(self.state()["inflight"])

    def test_worker_hold_budget_is_gone(self):
        self.assertFalse(hasattr(worker, "REPLY_GUARD_HOLD_S"))
        self.assertFalse(hasattr(worker, "_reply_guard_verdict"))

    def test_injecting_a_new_turn_resets_the_hook_nudge_count(self):
        self.set_state(inflight=None, inflight_since=None)
        counter = spool.channel_dir(self.cfg, CID) / spool.REPLY_GUARD_STATE
        counter.write_text('{"nudges": 2}', encoding="utf-8")
        self.enqueue(text="next", post_id="p9")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.session.inject.assert_called_once()
        self.assertEqual(json.loads(counter.read_text(encoding="utf-8"))["nudges"], 0)


class SelfInitiatedTurnTests(_Base):
    """A reply with no turn in flight (a background task woke the session) goes to the last thread the channel replied in."""

    def test_successful_delivery_records_its_thread(self):
        self.set_state(inflight={"post_id": "p1", "root_id": "root-1", "user": "alice", "text": "q"}, inflight_since=NOW)
        self.write_reply("Listo.")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.assertEqual(self.state()["last_reply_to"], {"root_id": "root-1"})

    def test_self_initiated_reply_is_posted_to_the_last_thread_in_parts(self):
        self.set_state(last_reply_to={"root_id": "root-9"})
        self.write_reply("Final list:\n---post---\nHotel 1\n---post---\nHotel 2")
        with self.assertLogs("claude_code_bridge", level="INFO") as logs:
            worker.tick(self.cfg, self.post, now=NOW + 1)
        self.assertEqual(self.post.call_args_list, [
            mock.call(CID, "root-9", "Final list:"),
            mock.call(CID, "root-9", "Hotel 1"),
            mock.call(CID, "root-9", "Hotel 2"),
        ])
        self.assertTrue(any("delivered self-initiated turn" in m for m in logs.output))
        self.assertEqual(list((spool.channel_dir(self.cfg, CID) / "outbox").glob("*.json")), [])

    def test_self_initiated_reply_to_a_top_level_channel_posts_top_level(self):
        self.set_state(last_reply_to={"root_id": None})
        self.write_reply("Hecho.")
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, None, "Hecho.")

    def test_reply_with_no_turn_and_no_prior_delivery_is_still_dropped(self):
        self.write_reply("Hello.")
        with self.assertLogs("claude_code_bridge", level="INFO") as logs:
            worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_not_called()
        self.assertTrue(any("dropped a reply" in m for m in logs.output))

    def test_self_initiated_give_up_reply_is_posted_by_the_worker(self):
        self.cfg["reply_guard"] = copy.deepcopy(GUARD)
        self.set_state(last_reply_to={"root_id": "root-9"})
        self.write_reply(GIVE_UP)
        worker.tick(self.cfg, self.post, now=NOW + 1)
        self.post.assert_called_once_with(CID, "root-9", GIVE_UP)
        self.session.inject.assert_not_called()


if __name__ == "__main__":
    unittest.main()
