#!/usr/bin/env python3
"""Smoke tests for claude-code-bridge blocked-prompt handling.

A bridged session is unattended, so a dialog in its pane (a repo hook's confirmation, a permission
prompt) would wait forever. These tests cover the pane detector (lib/blocked.py), the worker's
settle / mode state machine (lib/worker.py: off, notify, decline, accept) and the AskUserQuestion deny
in the generated settings (lib/session.py).

No test starts a real ``claude``, tmux or network call: the session module, the operator poster and
the chat post function are mocks.
"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN_DIR))

from lib import blocked, config, spool, worker  # noqa: E402
from lib import session as real_session  # noqa: E402

CID = "chan1"
OPERATOR = "opchan"
NOW = 1_800_000_000.0
ASK_MESSAGE = "Ask the user in your chat reply instead; there is no interactive user here."

# The exact pane text from the live incident (after strip_styled).
BLOCKED_PANE = """ │ Hook PreToolUse:Bash requires confirmation for this command:
 │ Commit in the MAIN checkout (not a worktree) — shared branch, so confirm. Worktree commits on their own branch are auto-granted.
 settings.json to update hooks
 Do you want to proceed?
 ❯ 1. Yes
   2. No
 Esc to cancel · Tab to amend
"""

# A different dialog (another question, other options): a new signature.
OTHER_PANE = """ Do you want to make this edit to notes.md?
 ❯ 1. Yes
   2. Yes, allow all edits this session
   3. No
 Esc to cancel
"""

# Exempt dialogs: detected (they get notify) but never auto-answered.
AUQ_PANE = """ Which approach should I take?
 ❯ 1. Fast
   2. Thorough
   3. Type something.
 Enter to select · ↑/↓ to navigate · Esc to cancel
"""

REWIND_PANE = """ Rewind
 Restore code and conversation
 ❯ 1. Restore code and conversation
   2. Summarize from here
   3. Never mind
 Rewinding does not affect files edited manually or via bash
"""

MODEL_PANE = """ Select model
 Switch between Claude models. Applies to this session.
 ❯ 1. Default
   2. Haiku
 Enter to confirm · Esc to exit
"""

EXIT_PANE = """ Background work is running
 The following will stop when you exit:
  - monitor: tail logs
 ❯ 1. Exit and stop tasks
   2. Cancel
 Esc to cancel
"""

BUSY_PANE = """⏺ Working on the commit message.
✳ Crystallizing… (2m 48s · ↓ 18.4k tokens
❯
  ⏵⏵ accept edits on (shift+tab to cycle)
"""

IDLE_PANE = """────────────────────────────
❯
────────────────────────────
  ? for shortcuts
"""

NOTE_PREFIX = "The dialog 'Do you want to proceed?' was declined because nobody can answer it here."


class DetectTests(unittest.TestCase):
    def test_incident_pane_is_blocked_with_question_and_options(self):
        found = blocked.detect(BLOCKED_PANE)
        self.assertIsNotNone(found)
        self.assertEqual(found["question"], "Do you want to proceed?")
        self.assertEqual(found["options"], ["❯ 1. Yes", "2. No"])
        self.assertIsNone(found["exempt"])

    def test_same_pane_has_same_signature(self):
        self.assertEqual(blocked.detect(BLOCKED_PANE)["sig"], blocked.detect(BLOCKED_PANE)["sig"])

    def test_different_options_give_a_different_signature(self):
        self.assertNotEqual(blocked.detect(BLOCKED_PANE)["sig"], blocked.detect(OTHER_PANE)["sig"])

    def test_idle_footer_does_not_veto_an_option_block(self):
        pane = BLOCKED_PANE + "  ⏵⏵ accept edits on (shift+tab to cycle)\n  ? for shortcuts\n"
        self.assertIsNotNone(blocked.detect(pane))

    def test_busy_spinner_is_not_blocked(self):
        self.assertIsNone(blocked.detect(BUSY_PANE))

    def test_idle_prompt_is_not_blocked(self):
        self.assertIsNone(blocked.detect(IDLE_PANE))

    def test_one_option_row_is_not_enough(self):
        self.assertIsNone(blocked.detect(BLOCKED_PANE.replace("   2. No\n", "")))

    def test_selector_line_is_required(self):
        self.assertIsNone(blocked.detect(BLOCKED_PANE.replace("❯ 1. Yes", "  1. Yes")))

    def test_only_the_bottom_60_lines_are_read(self):
        self.assertIsNone(blocked.detect(BLOCKED_PANE + "\n" * 60))

    def test_empty_pane_is_not_blocked(self):
        self.assertIsNone(blocked.detect(""))
        self.assertIsNone(blocked.detect(None))

    def test_exempt_dialogs_are_detected_with_their_reason(self):
        self.assertEqual(blocked.detect(AUQ_PANE)["exempt"], "AskUserQuestion")
        self.assertEqual(blocked.detect(REWIND_PANE)["exempt"], "rewind")
        self.assertEqual(blocked.detect(MODEL_PANE)["exempt"], "model selection")
        self.assertEqual(blocked.detect(EXIT_PANE)["exempt"], "exit confirmation")

    def test_mode_reads_string_and_mapping_forms_and_defaults_to_off(self):
        self.assertEqual(blocked.mode({}), "off")
        self.assertEqual(blocked.mode({"blocked_prompt": {"mode": "accept"}}), "accept")
        self.assertEqual(blocked.mode({"blocked_prompt": "notify"}), "notify")
        self.assertEqual(blocked.mode({"blocked_prompt": {"mode": "bogus"}}), "off")

    def test_default_config_is_off(self):
        self.assertEqual(config.DEFAULTS["blocked_prompt"]["mode"], "off")
        self.assertFalse(config.DEFAULTS["deny_ask_user_question"])


class _WorkerBase(unittest.TestCase):
    MODE = "decline"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["home"] = str(self.tmp / "claude-code-bridge")
        self.cfg["operator_channel"] = OPERATOR
        self.cfg["blocked_prompt"] = {"mode": self.MODE}
        self.session = mock.MagicMock(name="session")
        self.session.exists.return_value = True
        self.session.is_idle.return_value = True
        self.session.inject.return_value = True
        self.session.press.return_value = True
        self.session.fingerprint.return_value = "fp"
        patcher = mock.patch.object(worker, "session", self.session)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.post = mock.MagicMock(name="post_fn")
        self.operator = mock.patch.object(worker.poster, "post", return_value=True).start()
        mock.patch.object(worker.poster, "channel_name", return_value="test").start()
        # The operator channel is in the bridged channel's team (the team boundary is tested in
        # test_claude_code_bridge_team_boundary.py).
        mock.patch.object(worker.poster, "same_team", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        spool.save_state(self.cfg, CID, {
            "launch_fingerprint": "fp",
            "platform": "mattermost",
            "inflight": {"post_id": "p1", "root_id": None, "user": "alice", "text": "do it", "ts": NOW},
            "inflight_since": NOW,
        })
        self.session.capture.return_value = BLOCKED_PANE

    def tearDown(self):
        self._tmp.cleanup()

    def tick(self, at, pane=None):
        if pane is not None:
            self.session.capture.return_value = pane
        worker.tick(self.cfg, self.post, now=NOW + at)

    def state(self):
        return spool.load_state(self.cfg, CID)


class DefaultOffTests(_WorkerBase):
    MODE = "off"

    def test_default_off_does_nothing_at_all(self):
        for at in (0, 60, 600, 900):
            self.tick(at)
        self.session.capture.assert_not_called()
        self.operator.assert_not_called()
        self.session.press.assert_not_called()
        self.assertNotIn("blocked", self.state())

    def test_unset_blocked_prompt_is_off(self):
        self.cfg.pop("blocked_prompt")
        for at in (0, 60, 600):
            self.tick(at)
        self.operator.assert_not_called()
        self.session.press.assert_not_called()


class NotifyModeTests(_WorkerBase):
    MODE = "notify"

    def test_not_notified_before_the_settle_window(self):
        self.tick(0)
        self.tick(59)
        self.operator.assert_not_called()

    def test_notify_posts_question_and_options_once_to_operator(self):
        for at in (0, 30, 60, 62, 120):
            self.tick(at)
        self.operator.assert_called_once()
        channel, root, text = self.operator.call_args.args[:3]
        self.assertEqual(channel, OPERATOR)
        self.assertIsNone(root)
        self.assertIn("test", text)
        self.assertIn("Do you want to proceed?", text)
        self.assertIn("1. Yes", text)
        self.assertIn("2. No", text)
        self.post.assert_not_called()  # the family channel never gets the dialog

    def test_notify_never_presses_or_injects(self):
        for at in (0, 60, 600, 700):
            self.tick(at)
        self.session.press.assert_not_called()
        self.session.inject.assert_not_called()

    def test_busy_pane_is_never_blocked(self):
        for at in (0, 60, 120, 700):
            self.tick(at, pane=BUSY_PANE)
        self.operator.assert_not_called()

    def test_idle_pane_is_never_blocked(self):
        for at in (0, 60, 120, 700):
            self.tick(at, pane=IDLE_PANE)
        self.operator.assert_not_called()

    def test_new_signature_is_notified_again(self):
        self.tick(0)
        self.tick(60)
        self.tick(100, pane=OTHER_PANE)
        self.tick(160)
        self.assertEqual(self.operator.call_count, 2)

    def test_unset_operator_channel_logs_warning_only(self):
        self.cfg["operator_channel"] = ""
        self.tick(0)
        with self.assertLogs("claude_code_bridge", level="WARNING") as logs:
            self.tick(60)
        self.operator.assert_not_called()
        self.assertTrue(any("operator_channel" in line for line in logs.output))

    def test_operator_channel_equal_to_family_channel_is_never_used(self):
        self.cfg["operator_channel"] = CID
        self.tick(0)
        with self.assertLogs("claude_code_bridge", level="WARNING"):
            self.tick(60)
        self.operator.assert_not_called()

    def test_failed_operator_post_is_retried_on_the_next_tick(self):
        self.operator.side_effect = [False, True]
        for at in (0, 60, 62, 64):
            self.tick(at)
        self.assertEqual(self.operator.call_count, 2)

    def test_per_channel_mode_overrides_global(self):
        self.cfg["blocked_prompt"] = {"mode": "off"}
        self.cfg["channels"] = {CID: {"blocked_prompt": {"mode": "notify"}}}
        self.tick(0)
        self.tick(60)
        self.operator.assert_called_once()

    def test_per_channel_string_form_is_accepted(self):
        self.cfg["blocked_prompt"] = {"mode": "off"}
        self.cfg["channels"] = {CID: {"blocked_prompt": "notify"}}
        self.tick(0)
        self.tick(60)
        self.operator.assert_called_once()


class DeclineModeTests(_WorkerBase):
    MODE = "decline"

    def test_notify_then_escape_after_timeout_with_warning_and_note_on_next_idle(self):
        self.session.is_idle.return_value = False  # the turn is in flight: the pane is busy
        self.tick(0)
        self.tick(60)
        self.operator.assert_called_once()
        self.tick(599)
        self.session.press.assert_not_called()
        with self.assertLogs("claude_code_bridge", level="WARNING") as logs:
            self.tick(600)
        self.session.press.assert_called_once_with(self.cfg, CID, "Escape")
        self.assertTrue(any("declined" in line for line in logs.output))
        self.session.inject.assert_not_called()  # still in flight: the note waits for an idle session
        self.session.is_idle.return_value = True  # the decline cleared the dialog; the session is idle now
        self.tick(601, pane=IDLE_PANE)
        self.session.inject.assert_called_once()
        injected = self.session.inject.call_args.args[2]
        self.assertTrue(injected.startswith(NOTE_PREFIX), injected)
        self.assertIn("Finish the task without that step", injected)
        self.assertIsNone(self.state().get("blocked_note"))
        self.tick(700)
        self.session.press.assert_called_once()  # declined once per signature

    def test_new_signature_resets_timeout(self):
        self.tick(0)
        self.tick(60)
        self.tick(100, pane=OTHER_PANE)
        self.tick(160)
        self.tick(600)  # the first dialog's timeout would be due; the second has only 500 s
        self.session.press.assert_not_called()
        self.tick(700)  # 600 s after the second dialog was first seen
        self.session.press.assert_called_once()

    def test_escape_failure_is_retried_on_a_later_tick(self):
        self.session.press.side_effect = [False, True]
        for at in (0, 60, 600, 602):
            self.tick(at)
        self.assertEqual(self.session.press.call_count, 2)

    def test_no_check_when_nothing_in_flight_and_no_state(self):
        spool.save_state(self.cfg, CID, {"launch_fingerprint": "fp"})
        self.tick(0)
        self.session.capture.assert_not_called()


class AcceptModeTests(_WorkerBase):
    MODE = "accept"

    def test_settled_dialog_gets_enter_once_then_retries_after_settle_windows(self):
        self.tick(0)
        self.tick(59)
        self.session.press.assert_not_called()
        with self.assertLogs("claude_code_bridge", level="INFO") as logs:
            self.tick(60)
        self.session.press.assert_called_once_with(self.cfg, CID, "Enter")
        self.assertTrue(any("Do you want to proceed?" in line for line in logs.output))
        self.tick(61)  # same signature inside the settle window again: no second press
        self.assertEqual(self.session.press.call_count, 1)
        self.tick(120)  # the dialog is still there after a full window: first retry
        self.tick(180)  # second retry
        self.assertEqual(self.session.press.call_count, 3)
        self.operator.assert_not_called()

    def test_after_retries_fall_back_to_notify_without_more_presses(self):
        for at in (0, 60, 120, 180):
            self.tick(at)
        self.tick(240)
        self.assertEqual(self.session.press.call_count, 3)
        self.operator.assert_called_once()
        self.tick(300)
        self.tick(400)
        self.assertEqual(self.session.press.call_count, 3)
        self.assertEqual(self.operator.call_count, 1)

    def test_dialog_gone_after_accept_is_not_notified(self):
        self.tick(0)
        self.tick(60)
        self.tick(61, pane=IDLE_PANE)
        self.session.press.assert_called_once()
        self.operator.assert_not_called()

    def test_new_signature_after_accept_is_accepted_too(self):
        self.tick(0)
        self.tick(60)
        self.tick(100, pane=OTHER_PANE)
        self.tick(160)
        self.assertEqual(self.session.press.call_count, 2)
        self.operator.assert_not_called()

    def test_exempt_dialogs_are_never_answered_and_are_notified(self):
        for label, pane in (("AskUserQuestion", AUQ_PANE), ("rewind", REWIND_PANE),
                            ("model selection", MODEL_PANE), ("exit confirmation", EXIT_PANE)):
            with self.subTest(dialog=label):
                self.session.press.reset_mock()
                self.operator.reset_mock()
                spool.save_state(self.cfg, CID, {**self.state(), "blocked": None, "blocked_candidate": None})
                for at in (0, 60, 120):
                    self.tick(at, pane=pane)
                self.session.press.assert_not_called()
                self.operator.assert_called_once()
                self.assertIn(label, self.operator.call_args.args[2])


class AskUserQuestionDenyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["home"] = str(self.tmp / "claude-code-bridge")

    def tearDown(self):
        self._tmp.cleanup()

    def _settings(self):
        d = real_session.prepare_dir(self.cfg, CID)
        return json.loads((d / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def _matchers(self, settings):
        return [h.get("matcher") for h in settings.get("hooks", {}).get("PreToolUse", [])]

    def test_default_does_not_deny_ask_user_question(self):
        self.assertNotIn("AskUserQuestion", self._matchers(self._settings()))

    def test_enabled_denies_ask_user_question_with_the_bridge_message(self):
        self.cfg["deny_ask_user_question"] = True
        settings = self._settings()
        hooks = [h for h in settings["hooks"]["PreToolUse"] if h.get("matcher") == "AskUserQuestion"]
        self.assertEqual(len(hooks), 1)
        result = subprocess.run(hooks[0]["hooks"][0]["command"], shell=True, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn(ASK_MESSAGE, result.stderr)

    def test_browser_guard_hook_is_kept_next_to_the_ask_deny(self):
        self.cfg["deny_ask_user_question"] = True
        self.cfg["browser_guard"] = {"min_interval_s": 1}
        matchers = self._matchers(self._settings())
        self.assertIn("AskUserQuestion", matchers)
        self.assertIn(real_session.BROWSER_GUARD_TOOLS, matchers)

    def test_toggle_changes_the_launch_fingerprint(self):
        before = real_session.fingerprint(self.cfg, CID)
        self.cfg["deny_ask_user_question"] = True
        self.assertNotEqual(before, real_session.fingerprint(self.cfg, CID))


class PressTests(unittest.TestCase):
    def test_press_sends_the_key_to_the_channel_pane(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        with mock.patch.object(real_session, "_tmux") as tmux:
            tmux.return_value = subprocess.CompletedProcess([], 0, "", "")
            self.assertTrue(real_session.press(cfg, CID, "Escape"))
        tmux.assert_called_once_with(cfg, "send-keys", "-t", "=claude-code-bridge-chan1:", "Escape")


if __name__ == "__main__":
    unittest.main()
