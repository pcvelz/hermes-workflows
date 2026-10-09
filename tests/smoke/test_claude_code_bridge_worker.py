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

    def test_returns_none_for_non_mattermost_platform(self):
        self._write_config()
        self.assertIsNone(self.plugin._on_dispatch(event=self._event(platform="telegram")))
        self.start_thread.assert_not_called()

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

    def test_gives_up_after_the_attempt_cap(self):
        self.post.return_value = False
        self.set_state(inflight={"post_id": "p1", "root_id": None, "text": "q"}, inflight_since=NOW)
        self.write_reply("never delivered")

        for i in range(worker.MAX_REPLY_POST_ATTEMPTS):
            worker.tick(self.cfg, self.post, now=NOW + i)

        self.assertEqual(self.post.call_count, worker.MAX_REPLY_POST_ATTEMPTS)
        self.assertEqual(self._outbox(), [])
        self.assertIsNone(self.state()["inflight"])
        worker.tick(self.cfg, self.post, now=NOW + 99)
        self.assertEqual(self.post.call_count, worker.MAX_REPLY_POST_ATTEMPTS)


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
        for text in ("Investigate briefly: top cities", "quiero investigar el tema",
                     "I want to research zebras", "Deep Research on X", "investigación de mercado",
                     "Onderzoek de beste router", "kun je dit uitzoeken?", "zoek uit wat werkt"):
            with self.subTest(text=text):
                self.assertIsNone(self._decide(text))
        self.assertEqual(spool.pending(self.cfg, CID), [])

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


if __name__ == "__main__":
    unittest.main()
