"""Smoke tests for the claude-code-bridge save-claim guard, which runs in the Claude Code Stop hook.

A final reply that claims something was saved ("saved", "guardado", "opgeslagen", ...) is blocked once per turn
when no Write or Edit tool call succeeded in that turn. The turn's tool calls are read from the hook payload's
transcript_path (Claude Code's JSONL transcript). The hook runs as a subprocess, exactly as Claude Code runs it:
JSON on stdin, decision on stdout. Never starts a real claude.
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

import stop_hook  # noqa: E402
from lib import config, session, spool  # noqa: E402

CID = "chan1"
CLAIM_LOG = "claude-code-bridge: blocked unbacked save claim"
CLAIM_CASES = [
    "I've saved it to memory.",
    "This is already saved.",
    "I'll remember that for next time.",
    "Noted for next time.",
    "It is in memory now.",
    "Ya está guardado en tu memoria.",
    "Lo he guardado.",
    "Lo recordaré.",
    "Ik heb het opgeslagen.",
    "Ik zal het onthouden.",
]
CLEAN = "Here is the summary."


def _prompt(text="please remember this"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _say(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _call(tool_id, name="Write"):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}]}}


def _result(tool_id, is_error=False):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "x", "is_error": is_error}]}}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg.update(enabled=True, claude_bin="/bin/echo", home=str(self.tmp / "ccb"))
        self.cfg["save_claim_guard"] = True
        self.d = session.prepare_dir(self.cfg, CID)  # the real channel layout: stop_hook.py, save_claim_guard.json

    def tearDown(self):
        self._tmp.cleanup()

    def transcript(self, entries):
        path = self.tmp / "transcript.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        return str(path)

    def run_hook(self, text, entries, transcript_path=None):
        payload = {
            "session_id": "s",
            "last_assistant_message": text,
            "stop_hook_active": False,
            "transcript_path": transcript_path if transcript_path is not None else self.transcript(entries),
        }
        proc = subprocess.run(
            [sys.executable, str(self.d / "stop_hook.py")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=30, cwd=str(self.d),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def delivered_texts(self):
        return [json.loads(p.read_text(encoding="utf-8"))["text"] for p in sorted((self.d / "outbox").glob("*.json"))]

    def log_lines(self):
        path = self.d / "reply_guard.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def new_turn(self):
        spool.reset_reply_guard_count(self.cfg, CID)  # what the worker does when it injects a new turn


class SaveClaimGuardTests(_Base):
    def test_unbacked_claims_in_en_es_nl_are_blocked(self):
        for claim in CLAIM_CASES:
            with self.subTest(claim=claim):
                self.new_turn()
                out = self.run_hook(claim, [_prompt(), _say(claim)])
                self.assertEqual(json.loads(out)["decision"], "block")
                self.assertIn("nothing was written in this turn", json.loads(out)["reason"])
                self.assertEqual(self.delivered_texts(), [])
        self.assertEqual(sum(1 for line in self.log_lines() if line.endswith(CLAIM_LOG)), len(CLAIM_CASES))

    def test_block_reason_names_the_preference_file_and_forbids_internal_talk(self):
        reason = json.loads(self.run_hook("Saved.", [_prompt(), _say("Saved.")]))["reason"]
        self.assertIn(f"./{stop_hook.PREFERENCES_FILE}", reason)
        self.assertEqual(stop_hook.PREFERENCES_FILE, "PREFERENCES.md")
        self.assertIn("Never mention memory, files, file names, turns, sessions, earlier replies or this check.", reason)
        self.assertIn("in the same language your replies in this chat always use", reason)
        self.assertIn('Matched phrase: "Saved"', reason)

    def test_backed_claim_with_successful_write_is_allowed(self):
        entries = [_prompt(), _call("t1"), _result("t1", is_error=False), _say("Saved it.")]
        self.assertEqual(self.run_hook("Saved it.", entries).strip(), "")
        self.assertEqual(self.delivered_texts(), ["Saved it."])
        self.assertEqual(self.log_lines(), [])

    def test_backed_claim_with_successful_edit_is_allowed(self):
        entries = [_prompt(), _call("t1", "Edit"), _result("t1"), _say("Lo he guardado.")]
        self.assertEqual(self.run_hook("Lo he guardado.", entries).strip(), "")
        self.assertEqual(self.delivered_texts(), ["Lo he guardado."])

    def test_failed_write_does_not_back_the_claim(self):
        entries = [_prompt(), _call("t1"), _result("t1", is_error=True), _say("I've saved it.")]
        self.assertEqual(json.loads(self.run_hook("I've saved it.", entries))["decision"], "block")
        self.assertEqual(self.delivered_texts(), [])

    def test_write_in_an_earlier_turn_does_not_back_the_claim(self):
        entries = [_prompt("first"), _call("t1"), _result("t1"), _say("Saved."),
                   _prompt("second"), _say("Saved again.")]
        self.assertEqual(json.loads(self.run_hook("Saved again.", entries))["decision"], "block")

    def test_no_claim_is_allowed(self):
        self.assertEqual(self.run_hook(CLEAN, [_prompt(), _say(CLEAN)]).strip(), "")
        self.assertEqual(self.delivered_texts(), [CLEAN])
        self.assertEqual(self.log_lines(), [])

    def test_negated_claim_is_not_a_claim(self):
        text = "I have not saved anything yet."
        self.assertEqual(self.run_hook(text, [_prompt(), _say(text)]).strip(), "")
        self.assertEqual(self.delivered_texts(), [text])

    def test_negation_after_the_verb_is_not_a_claim(self):
        for text in ["Nothing has been saved.", "Nothing was saved.", "No se ha guardado nada.",
                     "Er is niets opgeslagen.", "I have saved nothing."]:
            with self.subTest(text=text):
                self.new_turn()
                self.assertEqual(self.run_hook(text, [_prompt(), _say(text)]).strip(), "")
        self.assertEqual(self.log_lines(), [])

    def test_per_channel_override_enables_the_guard_for_that_channel_only(self):
        self.cfg["save_claim_guard"] = False
        self.cfg["channels"] = {CID: {"save_claim_guard": True}}
        self.d = session.prepare_dir(self.cfg, CID)
        self.assertTrue((self.d / session.SAVE_CLAIM_FILE).exists())
        self.assertEqual(json.loads(self.run_hook("Saved.", [_prompt(), _say("Saved.")]))["decision"], "block")
        other = session.prepare_dir(self.cfg, "other-channel")
        self.assertFalse((other / session.SAVE_CLAIM_FILE).exists())

    def test_permission_denied_result_without_is_error_does_not_back_the_claim(self):
        entries = [_prompt(), _call("t1"), _result("t1", is_error=False), _say("Saved.")]
        entries[2]["message"]["content"][0]["content"] = "Permission denied: write blocked by policy"
        self.assertEqual(json.loads(self.run_hook("Saved.", entries))["decision"], "block")
        self.assertEqual(self.delivered_texts(), [])

    def test_second_block_in_the_same_turn_is_allowed_and_logged(self):
        entries = [_prompt(), _say("Saved.")]
        self.assertEqual(json.loads(self.run_hook("Saved.", entries))["decision"], "block")
        self.assertEqual(self.run_hook("Saved.", entries).strip(), "")
        self.assertEqual(self.delivered_texts(), ["Saved."])
        lines = self.log_lines()
        self.assertEqual(sum(1 for line in lines if line.endswith(CLAIM_LOG)), 1)
        self.assertTrue(any("unbacked save claim delivered after one block" in line for line in lines))

    def test_new_turn_allows_a_block_again(self):
        entries = [_prompt(), _say("Saved.")]
        self.run_hook("Saved.", entries)
        self.run_hook("Saved.", entries)
        self.new_turn()
        self.assertEqual(json.loads(self.run_hook("Saved.", entries))["decision"], "block")

    def test_guard_is_off_by_default(self):
        self.assertIs(config.DEFAULTS["save_claim_guard"], False)
        self.cfg["save_claim_guard"] = False
        self.d = session.prepare_dir(self.cfg, CID)
        self.assertFalse((self.d / session.SAVE_CLAIM_FILE).exists())
        self.assertEqual(self.run_hook("Saved.", [_prompt(), _say("Saved.")]).strip(), "")
        self.assertEqual(self.delivered_texts(), ["Saved."])

    def test_unreadable_transcript_delivers_with_a_warning(self):
        out = self.run_hook("Saved.", [], transcript_path=str(self.tmp / "missing.jsonl"))
        self.assertEqual(out.strip(), "")
        self.assertEqual(self.delivered_texts(), ["Saved."])
        self.assertTrue(any("WARNING save_claim" in line for line in self.log_lines()))

    def test_switch_file_is_written_only_when_on(self):
        self.assertEqual(json.loads((self.d / session.SAVE_CLAIM_FILE).read_text(encoding="utf-8")), {"enabled": True})
        self.cfg["save_claim_guard"] = False
        self.d = session.prepare_dir(self.cfg, CID)
        self.assertFalse((self.d / session.SAVE_CLAIM_FILE).exists())


if __name__ == "__main__":
    unittest.main()
