"""Smoke tests for chat via hooks (claude-code-bridge, opt-in per channel with ``chat_hooks: true``).

Covers: the texts file loader and its validation, the effective config (contract dropped, channel
override), the launch argv and settings written for a channel, the send script (run as a subprocess,
exactly as the session runs it), the Stop hook's chat branch, the worker's prompt prefix and the
per-turn reset. Never starts a real claude.
"""
import copy
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN))

from lib import chat_hooks, config, session, spool, worker  # noqa: E402

CID = "chan1"
SEND_REFUSED = "Not sent: {reason}. Rewrite it for {audience} and send again."
TEXTS = {
    "audience": "the people in this chat: plain words in their language",
    "ask_denied": "There is no one at this terminal. To ask, send one question with {send} and end your turn. Write for {audience}.",
    "stop_nothing_sent": "Nothing was sent. Send one message now with {send}. Then stop.",
    "send_refused": SEND_REFUSED,
    "prompt_context": "[chat · {sender} · {time}] Message from the chat. Reply with {send} when you're done.",
    "refuse_patterns": [
        {"pattern": r"(?i)try again later", "reason": "it gives up"},
        {"pattern": r"(?i)i('ll| will) (get back|let you know)", "reason": "it promises later work"},
    ],
    "max_stop_blocks": 2,
}
CONTRACT = "CONTRACT-TEXT: You are answering in a chat channel."


def _write_texts(path: Path, texts: dict) -> None:
    path.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.texts_path = self.tmp / "chat-hooks.json"
        _write_texts(self.texts_path, TEXTS)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg.update(
            enabled=True,
            claude_bin="/bin/echo",
            home=str(self.tmp / "ccb"),
            append_system_prompt=CONTRACT,
            chat_hooks=True,
            chat_hooks_file=str(self.texts_path),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def on(self, **extra):
        return config.for_channel(self.cfg, CID) if not extra else config.for_channel({**self.cfg, **extra}, CID)


class TextsFileTests(_Base):
    def test_valid_file_loads_normalised(self):
        loaded = config.chat_hooks_texts(config.for_channel(self.cfg, CID))
        self.assertEqual(loaded["ask_denied"], TEXTS["ask_denied"])
        self.assertEqual(loaded["refuse_patterns"][0], TEXTS["refuse_patterns"][0])
        self.assertEqual(loaded["max_stop_blocks"], 2)

    def test_off_by_default_and_returns_none(self):
        self.assertFalse(config.DEFAULTS["chat_hooks"])
        self.assertEqual(config.DEFAULTS["chat_hooks_file"], "")
        self.cfg["chat_hooks"] = False
        self.assertIsNone(config.chat_hooks_texts(config.for_channel(self.cfg, CID)))

    def test_missing_file_turns_feature_off_and_keeps_contract(self):
        self.cfg["chat_hooks_file"] = str(self.tmp / "nope.json")
        eff = config.for_channel(self.cfg, CID)
        self.assertIsNone(config.chat_hooks_texts(eff))
        self.assertEqual(eff["append_system_prompt"], CONTRACT)

    def test_missing_key_is_rejected(self):
        broken = dict(TEXTS)
        del broken["ask_denied"]
        _write_texts(self.texts_path, broken)
        self.assertIsNone(config.chat_hooks_texts(config.for_channel(self.cfg, CID)))

    def test_invalid_refuse_pattern_is_rejected(self):
        broken = dict(TEXTS, refuse_patterns=[{"pattern": "(unclosed", "reason": "x"}])
        _write_texts(self.texts_path, broken)
        self.assertIsNone(config.chat_hooks_texts(config.for_channel(self.cfg, CID)))

    def test_negative_or_boolean_stop_cap_is_rejected(self):
        for bad in (-1, True, "2"):
            with self.subTest(value=bad):
                _write_texts(self.texts_path, dict(TEXTS, max_stop_blocks=bad))
                self.assertIsNone(config.chat_hooks_texts(config.for_channel(self.cfg, CID)))

    def test_relative_file_resolves_against_hermes_home(self):
        home = self.tmp / "hermes-home"
        home.mkdir()
        _write_texts(home / "chat-hooks.json", TEXTS)
        with mock.patch.dict("os.environ", {"HERMES_HOME": str(home)}):
            self.cfg["chat_hooks_file"] = "chat-hooks.json"
            self.assertIsNotNone(config.chat_hooks_texts(config.for_channel(self.cfg, CID)))


class EffectiveConfigTests(_Base):
    def test_on_drops_the_global_contract(self):
        self.assertEqual(self.on()["append_system_prompt"], "")

    def test_off_keeps_the_global_contract(self):
        self.cfg["chat_hooks"] = False
        self.assertEqual(config.for_channel(self.cfg, CID)["append_system_prompt"], CONTRACT)

    def test_channel_override_turns_it_on_for_that_channel_only(self):
        self.cfg["chat_hooks"] = False
        self.cfg["channels"] = {CID: {"chat_hooks": True, "chat_hooks_file": str(self.texts_path)}}
        self.assertEqual(config.for_channel(self.cfg, CID)["append_system_prompt"], "")
        self.assertEqual(config.for_channel(self.cfg, "other")["append_system_prompt"], CONTRACT)

    def test_channel_override_turns_it_off(self):
        self.cfg["channels"] = {CID: {"chat_hooks": False}}
        self.assertEqual(config.for_channel(self.cfg, CID)["append_system_prompt"], CONTRACT)

    def test_channel_soul_and_prompt_still_apply_when_on(self):
        soul = self.tmp / "soul.md"
        soul.write_text("SOUL-TEXT", encoding="utf-8")
        self.cfg["channels"] = {CID: {"append_system_prompt": "CHANNEL-RULE", "soul_file": str(soul)}}
        prompt = config.for_channel(self.cfg, CID)["append_system_prompt"]
        self.assertEqual(prompt, "CHANNEL-RULE\n\nSOUL-TEXT")
        self.assertNotIn(CONTRACT, prompt)

    def test_channel_block_without_cwd_or_soul_still_drops_contract(self):
        self.cfg["channels"] = {CID: {"model": "sonnet"}}
        self.assertEqual(config.for_channel(self.cfg, CID)["append_system_prompt"], "")


class RenderTests(_Base):
    def test_send_placeholder_renders_the_exact_command(self):
        d = self.tmp / "spool"
        command = chat_hooks.send_command(d)
        self.assertTrue(command.endswith(f" {d / 'chat_send.py'} \"<message>\""), command)
        self.assertIn(sys.executable.split("/")[-1], command.split(" ")[0].split("/")[-1])

    def test_render_fills_known_placeholders_and_leaves_others(self):
        out = chat_hooks.render("to {audience} via {send}; left {reason}", audience="readers", send="CMD")
        self.assertEqual(out, "to readers via CMD; left {reason}")

    def test_render_does_not_touch_braces_that_are_not_placeholders(self):
        self.assertEqual(chat_hooks.render("{\"a\": 1} {audience}", audience="x"), "{\"a\": 1} x")

    def test_prompt_prefix_fills_sender_time_and_send(self):
        hooks = config.chat_hooks_texts(config.for_channel(self.cfg, CID))
        ts = 1_700_000_000
        text = chat_hooks.prompt_text(hooks, self.tmp / "spool", {"user": "ana", "ts": ts, "text": "hello"})
        self.assertTrue(text.startswith("[chat · ana · "), text)
        self.assertIn("Message from the chat. Reply with ", text)
        self.assertTrue(text.endswith("hello"))
        self.assertNotIn("{sender}", text)
        self.assertNotIn("{send}", text)


class ArgvAndSettingsTests(_Base):
    def _argv(self, cfg=None):
        cfg = cfg or self.cfg
        return session._argv(cfg, CID, "11111111-2222-3333-4444-555555555555", False, False)

    def _flag(self, argv, flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    def test_no_contract_in_argv_when_on(self):
        argv = self._argv()
        self.assertNotIn(CONTRACT, argv)
        self.assertNotIn("--append-system-prompt", argv)

    def test_contract_in_argv_when_off(self):
        self.cfg["chat_hooks"] = False
        self.assertEqual(self._flag(self._argv(), "--append-system-prompt"), CONTRACT)

    def test_spool_allow_rule_added_for_the_send_command_when_on(self):
        allowed = self._flag(self._argv(), "--allowedTools").split(",")
        rule = f"Bash({chat_hooks.send_command(spool.channel_dir(self.cfg, CID))}".replace(" \"<message>\"", ":*)")
        self.assertIn(rule, allowed)

    def test_no_spool_allow_rule_when_off(self):
        self.cfg["chat_hooks"] = False
        self.assertFalse(any(t.startswith("Bash(") for t in self._flag(self._argv(), "--allowedTools").split(",")))

    def test_repo_channel_gets_no_allow_rule(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        self.cfg["channels"] = {CID: {"cwd": str(repo)}}
        argv = self._argv()
        self.assertNotIn("--allowedTools", argv)
        self.assertNotIn(CONTRACT, argv)

    def test_fingerprint_changes_when_texts_change(self):
        before = session.fingerprint(self.cfg, CID)
        _write_texts(self.texts_path, dict(TEXTS, audience="someone else"))
        self.assertNotEqual(before, session.fingerprint(self.cfg, CID))

    def test_fingerprint_of_unchanged_off_channel_is_stable(self):
        self.cfg["chat_hooks"] = False
        base = session.fingerprint(self.cfg, CID)
        self.cfg.pop("chat_hooks")
        self.cfg.pop("chat_hooks_file")
        self.assertEqual(base, session.fingerprint(self.cfg, CID))


class PrepareDirTests(_Base):
    def settings(self, d):
        return json.loads((d / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_on_writes_send_script_and_runtime_file(self):
        d = session.prepare_dir(self.cfg, CID)
        self.assertTrue((d / "chat_send.py").exists())
        runtime = json.loads((d / chat_hooks.RUNTIME_FILE).read_text(encoding="utf-8"))
        self.assertEqual(runtime["max_stop_blocks"], 2)
        self.assertEqual(runtime["refuse_patterns"][0]["reason"], "it gives up")
        self.assertEqual(runtime["send_refused"], SEND_REFUSED.replace("{audience}", TEXTS["audience"]))

    def test_on_removes_the_reply_guard_file(self):
        self.cfg["reply_guard"] = {"patterns": [r"(?i)cannot check"]}
        d = session.prepare_dir(self.cfg, CID)
        self.assertFalse((d / session.REPLY_GUARD_FILE).exists())

    def test_off_removes_runtime_files_and_keeps_old_behaviour(self):
        d = session.prepare_dir(self.cfg, CID)
        self.assertTrue((d / chat_hooks.RUNTIME_FILE).exists())
        self.cfg["chat_hooks"] = False
        d = session.prepare_dir(self.cfg, CID)
        self.assertFalse((d / chat_hooks.RUNTIME_FILE).exists())
        self.assertFalse((d / "chat_send.py").exists())

    def test_ask_denied_hook_prints_the_rendered_text_when_on(self):
        settings = self.settings(session.prepare_dir(self.cfg, CID))
        hook = [h for h in settings["hooks"]["PreToolUse"] if h["matcher"] == "AskUserQuestion"]
        self.assertEqual(len(hook), 1)
        command = hook[0]["hooks"][0]["command"]
        self.assertIn("exit 2", command)
        self.assertIn("There is no one at this terminal", command)
        self.assertNotIn("{send}", command)
        self.assertNotIn("{audience}", command)
        self.assertNotIn("Ask the user in your chat reply", command)

    def test_ask_denied_hook_present_when_on_even_without_deny_flag(self):
        self.cfg["deny_ask_user_question"] = False
        settings = self.settings(session.prepare_dir(self.cfg, CID))
        self.assertTrue(any(h["matcher"] == "AskUserQuestion" for h in settings["hooks"]["PreToolUse"]))

    def test_deny_flag_alone_keeps_the_old_message(self):
        self.cfg["chat_hooks"] = False
        self.cfg["deny_ask_user_question"] = True
        settings = self.settings(session.prepare_dir(self.cfg, CID))
        command = next(h for h in settings["hooks"]["PreToolUse"] if h["matcher"] == "AskUserQuestion")["hooks"][0]["command"]
        self.assertIn(session.ASK_USER_QUESTION_MESSAGE, command)

    def test_stop_hook_stays_registered_when_on(self):
        settings = self.settings(session.prepare_dir(self.cfg, CID))
        self.assertIn("Stop", settings["hooks"])


class SendScriptTests(_Base):
    def setUp(self):
        super().setUp()
        self.d = session.prepare_dir(self.cfg, CID)

    def send(self, *args):
        return subprocess.run(
            [sys.executable, str(self.d / "chat_send.py"), *args],
            capture_output=True, text=True, timeout=30, cwd=str(self.d),
        )

    def outbox(self):
        return sorted((self.d / "outbox").glob("*.json"))

    def turn(self):
        path = self.d / chat_hooks.TURN_FILE
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_ok_writes_one_outbox_item_and_marks_sent(self):
        proc = self.send("Your list is ready.")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Sent to the chat.", proc.stdout)
        items = [json.loads(p.read_text(encoding="utf-8")) for p in self.outbox()]
        self.assertEqual([i["text"] for i in items], ["Your list is ready."])
        self.assertEqual(self.turn()["sent"], 1)

    def test_refused_pattern_exits_nonzero_and_prints_send_refused(self):
        proc = self.send("Sorry, try again later.")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Not sent: it gives up. Rewrite it for the people in this chat", proc.stdout + proc.stderr)
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.turn().get("sent", 0), 0)

    def test_second_pattern_refuses_too(self):
        proc = self.send("I'll let you know tomorrow.")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("it promises later work", proc.stdout + proc.stderr)

    def test_multi_post_is_kept_as_one_item_with_marker(self):
        proc = self.send("Primero.\n---post---\nSegundo.")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        items = [json.loads(p.read_text(encoding="utf-8")) for p in self.outbox()]
        self.assertEqual(len(items), 1)
        self.assertEqual(worker.split_post_parts(items[0]["text"]), ["Primero.", "Segundo."])

    def test_refusal_in_any_part_refuses_the_whole_message(self):
        proc = self.send("Primero bien.\n---post---\nTry again later, sorry.")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.outbox(), [])

    def test_empty_message_is_refused(self):
        proc = self.send("   ")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.outbox(), [])

    def test_two_sends_count_twice(self):
        self.send("Uno.")
        self.send("Dos.")
        self.assertEqual(self.turn()["sent"], 2)
        self.assertEqual(len(self.outbox()), 2)


class StopHookChatTests(_Base):
    def setUp(self):
        super().setUp()
        self.d = session.prepare_dir(self.cfg, CID)

    def run_hook(self, text):
        payload = {"session_id": "s", "last_assistant_message": text, "stop_hook_active": False}
        proc = subprocess.run(
            [sys.executable, str(self.d / "stop_hook.py")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=30, cwd=str(self.d),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def outbox_items(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.d / "outbox").glob("*.json"))]

    def log(self):
        path = self.d / "reply_guard.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def set_turn(self, **fields):
        (self.d / chat_hooks.TURN_FILE).write_text(json.dumps(fields), encoding="utf-8")

    def test_sent_this_turn_allows_the_stop_and_posts_no_duplicate(self):
        self.set_turn(sent=1, blocks=0)
        self.assertEqual(self.run_hook("Final text the user already got.").strip(), "")
        items = self.outbox_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "")  # turn-end marker: nothing new to post
        self.assertTrue(items[0].get("turn_end"))

    def test_nothing_sent_blocks_with_stop_text(self):
        decision = json.loads(self.run_hook("I have the list ready."))
        self.assertEqual(decision["decision"], "block")
        self.assertEqual(decision["reason"], TEXTS["stop_nothing_sent"].replace("{send}", chat_hooks.send_command(self.d)))
        self.assertEqual(self.outbox_items(), [])
        self.assertEqual(self.turn_blocks(), 1)

    def turn_blocks(self):
        return json.loads((self.d / chat_hooks.TURN_FILE).read_text(encoding="utf-8"))["blocks"]

    def test_block_count_caps_then_final_text_is_delivered_with_warning(self):
        self.assertIn("block", self.run_hook("one"))
        self.assertIn("block", self.run_hook("two"))
        self.assertEqual(self.run_hook("three").strip(), "")
        self.assertEqual([i["text"] for i in self.outbox_items()], ["three"])
        self.assertIn("WARNING", self.log())

    def test_a_send_after_a_block_ends_the_turn_quietly(self):
        self.run_hook("first try")
        self.set_turn(sent=1, blocks=1)
        self.assertEqual(self.run_hook("second").strip(), "")
        self.assertEqual(self.outbox_items()[0]["text"], "")

    def test_no_reply_guard_block_when_chat_hooks_on(self):
        self.cfg["reply_guard"] = {"patterns": [r"(?i)cannot check"]}
        self.d = session.prepare_dir(self.cfg, CID)
        self.set_turn(sent=1, blocks=0)
        self.assertEqual(self.run_hook("Cannot check it.").strip(), "")

    def test_unreadable_runtime_file_falls_back_to_plain_delivery(self):
        (self.d / chat_hooks.RUNTIME_FILE).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.run_hook("plain reply").strip(), "")
        self.assertEqual([i["text"] for i in self.outbox_items()], ["plain reply"])


class WorkerChatTests(_Base):
    def test_injected_text_is_prefixed_with_prompt_context_when_on(self):
        item = {"user": "ana", "ts": 1_700_000_000, "text": "hello"}
        text = worker.inject_text(self.cfg, CID, item)
        self.assertTrue(text.startswith("[chat · ana · "))
        self.assertTrue(text.endswith("\nhello"))

    def test_injected_text_is_unchanged_when_off(self):
        self.cfg["chat_hooks"] = False
        item = {"user": "ana", "ts": 1_700_000_000, "text": "hello"}
        self.assertEqual(worker.inject_text(self.cfg, CID, item), worker.format_message(item))

    def test_reset_chat_turn_clears_the_per_turn_state(self):
        session.prepare_dir(self.cfg, CID)
        path = spool.channel_dir(self.cfg, CID) / chat_hooks.TURN_FILE
        path.write_text(json.dumps({"sent": 2, "blocks": 1}), encoding="utf-8")
        spool.reset_chat_turn(self.cfg, CID)
        self.assertFalse(path.exists())


class WorkerTurnTests(_Base):
    """The worker around a chat-via-hooks turn, with the session mocked (as in test_claude_code_bridge_worker)."""

    NOW = 1_700_000_000

    def setUp(self):
        super().setUp()
        session_mock = mock.MagicMock(name="session")
        session_mock.exists.return_value = True
        session_mock.is_idle.return_value = True
        session_mock.inject.return_value = True
        session_mock.fingerprint.return_value = "fp"
        self.session = session_mock
        self.post = mock.MagicMock(name="post_fn", return_value=True)
        patcher = mock.patch.object(worker, "session", session_mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg["allowed_users"] = ["ana"]
        self.cfg["allowed_channels"] = [CID]
        spool.save_state(self.cfg, CID, {**spool.load_state(self.cfg, CID), "launch_fingerprint": "fp"})

    def _outbox_item(self, text, **extra):
        path = spool.channel_dir(self.cfg, CID) / "outbox" / f"{time.time_ns()}.json"
        path.write_text(json.dumps({"session_id": None, "text": text, "ts": self.NOW, **extra}), encoding="utf-8")

    def _inflight(self):
        spool.save_state(self.cfg, CID, {
            **spool.load_state(self.cfg, CID),
            "inflight": {"post_id": "p1", "root_id": "root-1", "user": "ana", "text": "q", "ts": self.NOW},
            "inflight_since": self.NOW,
        })

    def test_interim_item_is_posted_and_the_turn_stays_in_flight(self):
        self._inflight()
        self._outbox_item("Looking for it now.", interim=True)
        worker.tick(self.cfg, self.post, now=self.NOW + 5)
        self.post.assert_called_once_with(CID, "root-1", "Looking for it now.")
        state = spool.load_state(self.cfg, CID)
        self.assertIsNotNone(state["inflight"])

    def test_final_item_after_interim_ends_the_turn(self):
        self._inflight()
        self._outbox_item("Looking for it now.", interim=True)
        worker.tick(self.cfg, self.post, now=self.NOW + 5)
        self._outbox_item("Here it is.", turn_end=False)
        worker.tick(self.cfg, self.post, now=self.NOW + 6)
        self.assertEqual([c.args[2] for c in self.post.call_args_list], ["Looking for it now.", "Here it is."])
        self.assertIsNone(spool.load_state(self.cfg, CID)["inflight"])

    def test_turn_end_marker_ends_the_turn_without_a_post(self):
        self._inflight()
        self._outbox_item("", turn_end=True)
        worker.tick(self.cfg, self.post, now=self.NOW + 5)
        self.post.assert_not_called()
        self.assertIsNone(spool.load_state(self.cfg, CID)["inflight"])

    def test_injection_uses_the_prompt_prefix_and_resets_the_turn(self):
        spool.enqueue(self.cfg, CID, {"post_id": "p1", "root_id": None, "user": "ana", "text": "hello", "ts": self.NOW})
        turn = spool.channel_dir(self.cfg, CID) / chat_hooks.TURN_FILE
        turn.write_text(json.dumps({"sent": 1, "blocks": 1}), encoding="utf-8")
        worker.tick(self.cfg, self.post, now=self.NOW)
        injected = self.session.inject.call_args.args[2]
        self.assertTrue(injected.startswith("[chat · ana · "), injected)
        self.assertTrue(injected.endswith("\nhello"))
        self.assertFalse(turn.exists())

    def test_turn_counts_written_during_the_inject_survive(self):
        # The counts are reset BEFORE the inject: a send the new turn makes at once is not wiped afterwards.
        spool.enqueue(self.cfg, CID, {"post_id": "p1", "root_id": None, "user": "ana", "text": "hello", "ts": self.NOW})
        turn = spool.channel_dir(self.cfg, CID) / chat_hooks.TURN_FILE
        turn.write_text(json.dumps({"sent": 5, "blocks": 5}), encoding="utf-8")  # the previous turn's counts

        def fast_turn(*_a, **_k):
            turn.write_text(json.dumps({"sent": 1, "blocks": 0}), encoding="utf-8")
            return True

        self.session.inject.side_effect = fast_turn
        worker.tick(self.cfg, self.post, now=self.NOW)
        self.assertEqual(json.loads(turn.read_text(encoding="utf-8")), {"sent": 1, "blocks": 0})


if __name__ == "__main__":
    unittest.main()
