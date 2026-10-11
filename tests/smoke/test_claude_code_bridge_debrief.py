"""Smoke tests for the operator debrief (claude-code-bridge): "!debrief <question>".

Covers: routing (the command is queued for the session, never answered in the bridged channel), the sender
and operator_channel rules, the worker's operator prefix and reply routing (operator channel only), the
debrief marker lifecycle, the Stop hook (chat block and reply guard skipped during the debrief turn), and the
send script's refusal. The session is mocked in the worker tests; the hooks run as subprocesses. Never starts a real claude.
"""
import copy
import importlib.util
import json
import logging
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN))

from lib import config, poster, session, spool, worker  # noqa: E402

CID = "chan1"
OP = "op-channel"
NOW = 1_700_000_000
TEXTS = {
    "audience": "readers",
    "ask_denied": "No one at this terminal. Send one question with {send}.",
    "stop_nothing_sent": "Nothing was sent. Send one message with {send}.",
    "send_refused": "Not sent: {reason}. Rewrite it for {audience}.",
    "prompt_context": "[chat · {sender} · {time}] Reply with {send}.",
    "refuse_patterns": [{"pattern": "(?i)cannot check", "reason": "it gives up"}],
    "max_stop_blocks": 2,
}


def _load_plugin_init():
    spec = importlib.util.spec_from_file_location("ccb_plugin_init_debrief", PLUGIN / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_init()


def _event(text, user="ana", chat=CID):
    source = SimpleNamespace(platform=SimpleNamespace(value="mattermost"), chat_id=chat, user_name=user, thread_id=None)
    return SimpleNamespace(source=source, text=text, message_id="p9", raw_message={"root_id": ""})


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.texts_path = self.tmp / "chat-hooks.json"
        self.texts_path.write_text(json.dumps(TEXTS), encoding="utf-8")
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg.update(enabled=True, claude_bin="/bin/echo", home=str(self.tmp / "ccb"),
                        allowed_channels=[CID], allowed_users=["ana"], operator_channel=OP)

    def tearDown(self):
        self._tmp.cleanup()

    def marker(self, cid=CID):
        return spool.channel_dir(self.cfg, cid) / spool.DEBRIEF_STATE


class RoutingTests(_Base):
    def test_debrief_is_queued_for_the_session_and_skips_hermes(self):
        result = plugin._decide(_event("!debrief why did you stop?"), self.cfg, ensure_worker=False)
        self.assertEqual(result, {"action": "skip"})
        queued = [json.loads(p.read_text(encoding="utf-8")) for p in spool.pending(self.cfg, CID)]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["control"], "debrief")
        self.assertEqual(queued[0]["text"], "why did you stop?")
        self.assertEqual(queued[0]["post_id"], "p9")

    def test_command_word_is_case_insensitive_and_needs_a_boundary(self):
        plugin._decide(_event("!DEBRIEF   spaced question  "), self.cfg, ensure_worker=False)
        item = json.loads(spool.pending(self.cfg, CID)[0].read_text(encoding="utf-8"))
        self.assertEqual(item["text"], "spaced question")
        # "!debriefing" is not the command: it is an ordinary message (bridged as text).
        plugin._decide(_event("!debriefing is a word"), self.cfg, ensure_worker=False)
        second = [json.loads(p.read_text(encoding="utf-8")) for p in spool.pending(self.cfg, CID)][1]
        self.assertNotIn("control", second)

    def test_non_allowed_sender_is_ignored(self):
        result = plugin._decide(_event("!debrief hello", user="mallory"), self.cfg, ensure_worker=False)
        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(spool.pending(self.cfg, CID), [])

    def test_without_operator_channel_the_debrief_is_still_queued_and_filed(self):
        # The default sink is the channel's spool file: no operator channel is needed to ask.
        self.cfg["operator_channel"] = ""
        result = plugin._decide(_event("!debrief why?"), self.cfg, ensure_worker=False)
        self.assertEqual(result, {"action": "skip"})
        item = json.loads(spool.pending(self.cfg, CID)[0].read_text(encoding="utf-8"))
        self.assertEqual(item["control"], "debrief")

    def test_operator_channel_equal_to_the_bridged_channel_is_treated_as_unset(self):
        self.cfg["operator_channel"] = CID
        plugin._decide(_event("!debrief why?"), self.cfg, ensure_worker=False)
        item = json.loads(spool.pending(self.cfg, CID)[0].read_text(encoding="utf-8"))
        self.assertEqual(item["control"], "debrief")
        self.assertEqual(worker._operator(self.cfg, CID), "")

    def test_empty_question_is_dropped(self):
        with self.assertLogs("claude_code_bridge", level=logging.WARNING):
            plugin._decide(_event("!debrief   "), self.cfg, ensure_worker=False)
        self.assertEqual(spool.pending(self.cfg, CID), [])


class WorkerDebriefTests(_Base):
    """The worker around one debrief turn, with the session mocked."""

    def setUp(self):
        super().setUp()
        self.session = mock.MagicMock(name="session")
        self.session.exists.return_value = True
        self.session.is_idle.return_value = True
        self.session.inject.return_value = True
        self.session.fingerprint.return_value = "fp"
        self.post = mock.MagicMock(name="post_fn", return_value=True)
        for patcher in (mock.patch.object(worker, "session", self.session),
                        mock.patch.object(poster, "delete_post", return_value=True),
                        mock.patch.object(poster, "same_team", return_value=True)):  # team boundary: see team_boundary tests
            patcher.start()
            self.addCleanup(patcher.stop)
        self.delete = poster.delete_post
        spool.save_state(self.cfg, CID, {**spool.load_state(self.cfg, CID), "launch_fingerprint": "fp"})

    def enqueue_debrief(self, question="why did you stop?"):
        spool.enqueue(self.cfg, CID, {"post_id": "p9", "root_id": None, "user": "ana", "platform": "mattermost",
                                      "text": question, "ts": NOW, "control": "debrief"})

    def write_reply(self, text, cid=CID):
        path = spool.channel_dir(self.cfg, cid) / "outbox" / f"{time.time_ns()}.json"
        path.write_text(json.dumps({"session_id": None, "text": text, "ts": NOW}), encoding="utf-8")

    def test_debrief_turn_is_injected_with_the_operator_prefix_and_marked(self):
        self.enqueue_debrief()
        worker.tick(self.cfg, self.post, now=NOW)
        injected = self.session.inject.call_args.args[2]
        self.assertTrue(injected.startswith(worker.DEBRIEF_PREFIX), injected)
        self.assertTrue(injected.endswith("why did you stop?"))
        self.assertTrue(self.marker().exists())

    def test_the_command_post_is_deleted_and_the_operator_channel_is_routed(self):
        self.enqueue_debrief()
        worker.tick(self.cfg, self.post, now=NOW)
        self.delete.assert_called_once_with("p9")
        self.assertEqual(spool.load_state(self.cfg, OP)["platform"], "mattermost")

    def test_the_answer_goes_to_the_operator_channel_only_and_ends_the_turn(self):
        self.enqueue_debrief()
        worker.tick(self.cfg, self.post, now=NOW)
        self.write_reply("I stopped because the tool was denied.")
        worker.tick(self.cfg, self.post, now=NOW + 5)
        self.post.assert_called_once_with(OP, None, "I stopped because the tool was denied.")
        self.assertFalse(any(c.args[0] == CID for c in self.post.call_args_list))
        self.assertFalse(self.marker().exists())
        self.assertIsNone(spool.load_state(self.cfg, CID)["inflight"])

    def test_a_normal_message_after_a_debrief_is_not_marked(self):
        self.enqueue_debrief()
        worker.tick(self.cfg, self.post, now=NOW)
        self.write_reply("done")
        worker.tick(self.cfg, self.post, now=NOW + 5)
        spool.enqueue(self.cfg, CID, {"post_id": "p10", "root_id": None, "user": "ana", "text": "hello", "ts": NOW})
        worker.tick(self.cfg, self.post, now=NOW + 10)
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.session.inject.call_args.args[2].startswith(worker.DEBRIEF_PREFIX))

    def test_a_stale_marker_is_cleared_by_a_normal_injection(self):
        self.marker().write_text(json.dumps({"debrief": True}), encoding="utf-8")
        spool.enqueue(self.cfg, CID, {"post_id": "p10", "root_id": None, "user": "ana", "text": "hello", "ts": NOW})
        worker.tick(self.cfg, self.post, now=NOW)
        self.assertFalse(self.marker().exists())


class StopHookDebriefTests(_Base):
    """During a debrief turn the Stop hook never blocks and never runs the reply guard."""

    def setUp(self):
        super().setUp()
        self.cfg["chat_hooks"] = True
        self.cfg["chat_hooks_file"] = str(self.texts_path)
        self.cfg["reply_guard"] = {"patterns": [r"(?i)cannot check"]}
        self.d = session.prepare_dir(self.cfg, CID)

    def run_hook(self, text):
        payload = {"session_id": "s", "last_assistant_message": text, "stop_hook_active": False}
        proc = subprocess.run([sys.executable, str(self.d / "stop_hook.py")], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=30, cwd=str(self.d))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def outbox_texts(self):
        return [json.loads(p.read_text(encoding="utf-8"))["text"] for p in sorted((self.d / "outbox").glob("*.json"))]

    def test_nothing_sent_during_a_debrief_is_not_blocked_and_is_delivered(self):
        (self.d / spool.DEBRIEF_STATE).write_text(json.dumps({"debrief": True}), encoding="utf-8")
        self.assertEqual(self.run_hook("Here is why.").strip(), "")
        self.assertEqual(self.outbox_texts(), ["Here is why."])

    def test_a_give_up_during_a_debrief_bypasses_the_reply_guard(self):
        (self.d / spool.DEBRIEF_STATE).write_text(json.dumps({"debrief": True}), encoding="utf-8")
        self.assertEqual(self.run_hook("Cannot check it.").strip(), "")
        self.assertEqual(self.outbox_texts(), ["Cannot check it."])

    def test_without_the_marker_the_same_turn_is_blocked(self):
        self.assertEqual(json.loads(self.run_hook("Nothing sent yet."))["decision"], "block")


class SendRefusalDebriefTests(_Base):
    def setUp(self):
        super().setUp()
        self.cfg["chat_hooks"] = True
        self.cfg["chat_hooks_file"] = str(self.texts_path)
        self.d = session.prepare_dir(self.cfg, CID)

    def send(self, text):
        return subprocess.run([sys.executable, str(self.d / "chat_send.py"), text],
                              capture_output=True, text=True, timeout=30, cwd=str(self.d))

    def test_send_is_refused_during_a_debrief_turn(self):
        (self.d / spool.DEBRIEF_STATE).write_text(json.dumps({"debrief": True}), encoding="utf-8")
        proc = self.send("Hello everyone.")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Debrief turn: answer in your final text, not in the chat.", proc.stdout)
        self.assertEqual(list((self.d / "outbox").glob("*.json")) if (self.d / "outbox").exists() else [], [])

    def test_send_works_outside_a_debrief_turn(self):
        proc = self.send("Hello everyone.")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Sent to the chat.", proc.stdout)


if __name__ == "__main__":
    unittest.main()
