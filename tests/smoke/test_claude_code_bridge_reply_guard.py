"""Smoke tests for the claude-code-bridge reply guard, which runs in the Claude Code Stop hook.

Covers the compiled guard file that session.prepare_dir writes per channel, the Stop hook script
(run as a subprocess, exactly as Claude Code runs it: JSON on stdin, decision on stdout), the
per-turn nudge counter, and the launch fingerprint. Never starts a real claude.
"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN))

from lib import config, session, spool  # noqa: E402

CID = "chan1"
NUDGE = "NUDGE-TEXT"
GIVE_UP = "Right now I cannot check it, please try again later."
CLEAN = "Here is the summary."
GUARD = {
    "patterns": [r"(?i)cannot check", r"(?i)later", r"(?i)try again later"],
    "nudge": NUDGE,
    "max_nudges": 2,
}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg.update(enabled=True, claude_bin="/bin/echo", home=str(self.tmp / "ccb"))
        self.cfg["reply_guard"] = copy.deepcopy(GUARD)
        self.d = session.prepare_dir(self.cfg, CID)  # the real channel layout: stop_hook.py, reply_guard.json

    def tearDown(self):
        self._tmp.cleanup()

    def run_hook(self, text, stop_hook_active=False):
        payload = {
            "session_id": "s",
            "effort": "medium",
            "last_assistant_message": text,
            "stop_hook_active": stop_hook_active,
            "transcript_path": "/tmp/transcript.jsonl",
        }
        proc = subprocess.run(
            [sys.executable, str(self.d / "stop_hook.py")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=30, cwd=str(self.d),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def outbox(self):
        return sorted((self.d / "outbox").glob("*.json"))

    def delivered_texts(self):
        return [json.loads(p.read_text(encoding="utf-8"))["text"] for p in self.outbox()]

    def count(self):
        path = self.d / spool.REPLY_GUARD_STATE
        return json.loads(path.read_text(encoding="utf-8"))["nudges"]

    def log_lines(self):
        path = self.d / "reply_guard.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def guard_file(self, d=None):
        return json.loads(((d or self.d) / session.REPLY_GUARD_FILE).read_text(encoding="utf-8"))

    def settings(self, d=None):
        return json.loads(((d or self.d) / ".claude" / "settings.json").read_text(encoding="utf-8"))


class StopHookGuardTests(_Base):
    def test_matching_reply_is_blocked_with_nudge_and_matched_phrase(self):
        decision = json.loads(self.run_hook(GIVE_UP))
        self.assertEqual(decision["decision"], "block")
        self.assertIn(NUDGE, decision["reason"])
        self.assertIn("cannot check", decision["reason"])

    def test_blocked_reply_is_not_written_to_the_outbox(self):
        self.run_hook(GIVE_UP)
        self.assertEqual(self.outbox(), [])

    def test_block_counts_one_nudge_and_logs_exactly_one_line(self):
        self.run_hook(GIVE_UP)
        self.assertEqual(self.count(), 1)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("blocked", lines[0])
        self.assertIn("nudge 1 of 2", lines[0])
        self.assertNotIn(GIVE_UP, lines[0])  # the reply text is never logged

    def test_clean_reply_is_written_to_the_outbox_and_not_blocked(self):
        self.assertEqual(self.run_hook(CLEAN).strip(), "")
        self.assertEqual(self.delivered_texts(), [CLEAN])
        self.assertEqual(self.count(), 0)

    def test_max_nudges_reached_delivers_the_reply_with_a_warning(self):
        (self.d / spool.REPLY_GUARD_STATE).write_text('{"nudges": 2}', encoding="utf-8")
        self.assertEqual(self.run_hook(GIVE_UP).strip(), "")
        self.assertEqual(self.delivered_texts(), [GIVE_UP])
        self.assertTrue(any("WARNING" in line and "gave up after 2 nudges" in line for line in self.log_lines()))
        self.assertEqual(self.count(), 0)

    def test_two_blocks_then_delivery_on_the_third_stop(self):
        self.assertIn("block", self.run_hook(GIVE_UP))
        self.assertIn("block", self.run_hook(GIVE_UP))
        self.assertEqual(self.run_hook(GIVE_UP).strip(), "")
        self.assertEqual(self.delivered_texts(), [GIVE_UP])
        self.assertEqual(self.count(), 0)

    def test_stop_hook_active_reentry_is_still_checked(self):
        self.assertEqual(json.loads(self.run_hook(GIVE_UP, stop_hook_active=True))["decision"], "block")

    def test_counter_reset_on_a_new_turn_allows_nudges_again(self):
        self.run_hook(GIVE_UP)
        self.run_hook(GIVE_UP)
        spool.reset_reply_guard_count(self.cfg, CID)  # what the worker does when it injects a new turn
        self.assertEqual(self.count(), 0)
        self.assertIn("block", self.run_hook(GIVE_UP))
        self.assertEqual(self.count(), 1)

    def test_no_guard_file_means_no_block(self):
        self.cfg["reply_guard"] = {}
        self.d = session.prepare_dir(self.cfg, CID)
        self.assertEqual(self.run_hook(GIVE_UP).strip(), "")
        self.assertEqual(self.delivered_texts(), [GIVE_UP])

    def test_broken_guard_file_delivers_and_warns(self):
        (self.d / session.REPLY_GUARD_FILE).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.run_hook(GIVE_UP).strip(), "")
        self.assertEqual(self.delivered_texts(), [GIVE_UP])
        self.assertTrue(any("WARNING" in line for line in self.log_lines()))

    def test_nudge_that_cannot_be_recorded_delivers_instead_of_blocking(self):
        (self.d / spool.REPLY_GUARD_STATE).mkdir()  # the counter cannot be replaced: no block without a record
        self.assertEqual(self.run_hook(GIVE_UP).strip(), "")
        self.assertEqual(self.delivered_texts(), [GIVE_UP])

    def test_non_object_input_writes_nothing(self):
        proc = subprocess.run([sys.executable, str(self.d / "stop_hook.py")], input="[]",
                              capture_output=True, text=True, timeout=30, cwd=str(self.d))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(self.outbox(), [])

    def test_give_up_phrases_are_caught_by_the_configured_patterns(self):
        cases = (
            ("I have several options; as soon as it is done I will send you the full list.",
             [r"(?i)as soon as (it is done|i finish)", r"(?i)i will send you (the list|when)"]),
            ("Let me try again, I can't right now", [r"(?i)can'?t right now"]),
        )
        for text, patterns in cases:
            with self.subTest(text=text[:24]):
                self.cfg["reply_guard"] = {"patterns": patterns, "nudge": NUDGE, "max_nudges": 2}
                self.d = session.prepare_dir(self.cfg, CID)
                (self.d / spool.REPLY_GUARD_STATE).unlink(missing_ok=True)
                self.assertEqual(json.loads(self.run_hook(text))["decision"], "block")


class ReplyGuardFileTests(_Base):
    def test_global_guard_written_with_default_nudge_and_cap(self):
        self.cfg["reply_guard"] = {"patterns": [r"(?i)cannot check"]}
        written = self.guard_file(session.prepare_dir(self.cfg, CID))
        self.assertEqual(written["patterns"], [r"(?i)cannot check"])
        self.assertEqual(written["nudge"], session.REPLY_GUARD_NUDGE)
        self.assertEqual(written["max_nudges"], session.REPLY_GUARD_MAX_NUDGES)

    def test_channel_keys_merge_over_global_for_that_channel_only(self):
        self.cfg["reply_guard"] = {"patterns": [r"(?i)cannot check"], "nudge": "GLOBAL", "max_nudges": 2}
        self.cfg["channels"] = {CID: {"reply_guard": {"nudge": "CHANNEL", "max_nudges": 5}}}
        written = self.guard_file(session.prepare_dir(self.cfg, CID))
        self.assertEqual((written["nudge"], written["max_nudges"]), ("CHANNEL", 5))
        self.assertEqual(written["patterns"], [r"(?i)cannot check"])
        other = self.guard_file(session.prepare_dir(self.cfg, "999999"))
        self.assertEqual((other["nudge"], other["max_nudges"]), ("GLOBAL", 2))

    def test_channel_only_patterns_enable_the_guard_for_that_channel_only(self):
        self.cfg["reply_guard"] = {}
        self.cfg["channels"] = {CID: {"reply_guard": {"patterns": [r"(?i)cannot check"]}}}
        self.assertTrue((session.prepare_dir(self.cfg, CID) / session.REPLY_GUARD_FILE).exists())
        self.assertFalse((session.prepare_dir(self.cfg, "999999") / session.REPLY_GUARD_FILE).exists())

    def test_invalid_patterns_are_dropped_and_all_invalid_means_off(self):
        self.cfg["reply_guard"] = {"patterns": ["(unclosed", r"(?i)cannot check"]}
        self.assertEqual(self.guard_file(session.prepare_dir(self.cfg, CID))["patterns"], [r"(?i)cannot check"])
        self.cfg["reply_guard"] = {"patterns": ["(unclosed"]}
        self.assertFalse((session.prepare_dir(self.cfg, CID) / session.REPLY_GUARD_FILE).exists())

    def test_removing_the_guard_removes_the_file(self):
        self.assertTrue((self.d / session.REPLY_GUARD_FILE).exists())
        self.cfg["reply_guard"] = {}
        d = session.prepare_dir(self.cfg, CID)
        self.assertFalse((d / session.REPLY_GUARD_FILE).exists())

    def test_the_guard_lives_in_the_stop_hook_only(self):
        self.assertEqual(set(self.settings()["hooks"]), {"Stop"})
        command = self.settings()["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn(str(self.d / "stop_hook.py"), command)

    def test_guard_config_changes_the_launch_fingerprint(self):
        self.cfg["reply_guard"] = {}
        base = session.fingerprint(self.cfg, CID)
        self.cfg["reply_guard"] = copy.deepcopy(GUARD)
        with_guard = session.fingerprint(self.cfg, CID)
        self.assertNotEqual(base, with_guard)
        self.cfg["reply_guard"]["max_nudges"] = 3
        self.assertNotEqual(with_guard, session.fingerprint(self.cfg, CID))

    def test_unconfigured_fingerprint_is_unchanged_by_the_guard_feature(self):
        self.cfg["reply_guard"] = {}
        without_key = session.fingerprint(self.cfg, CID)
        self.cfg.pop("reply_guard")
        self.assertEqual(without_key, session.fingerprint(self.cfg, CID))


class ResetCounterTests(_Base):
    def test_reset_writes_zero(self):
        (self.d / spool.REPLY_GUARD_STATE).write_text('{"nudges": 2}', encoding="utf-8")
        spool.reset_reply_guard_count(self.cfg, CID)
        self.assertEqual(self.count(), 0)


if __name__ == "__main__":
    unittest.main()
