"""Smoke tests for the claude-code-bridge browser guard (plugins/claude-code-bridge/browser_guard.py
and the session.prepare_dir / fingerprint wiring).

Never starts a real `claude`. The script's guard() takes an injectable clock and sleep; one test runs
the script as a subprocess to check the exit-code / stderr contract Claude Code relies on.
"""
import copy
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN))

from lib import config, session  # noqa: E402

_spec = importlib.util.spec_from_file_location("browser_guard", PLUGIN / "browser_guard.py")
bg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bg)

CID = "123456"
NET = "mcp__browser__browser_navigate"
SNAP = "mcp__browser__browser_snapshot"
SHOT = "mcp__browser__browser_take_screenshot"
BLOCK = 2


class Clock:
    """Fake clock: sleep() advances time and is recorded."""

    def __init__(self, t=1000.0):
        self.t = float(t)
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def guard_cfg(**overrides):
    cfg = {
        "tools": "mcp__browser__.*",
        "network_tools": "mcp__browser__browser_(navigate|navigate_back|click|press_key|select_option|fill_form)",
        "min_interval_s": 0.0,
        "max_per_hour": None,
        "max_per_day": None,
        "deny_url_patterns": [],
        "deny_tools": [],
    }
    cfg.update(overrides)
    return cfg


def call(tool, **tool_input):
    return {"tool_name": tool, "tool_input": tool_input}


class GuardScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "browser_guard_state.json"

    def run_guard(self, data, cfg, clock):
        return bg.guard(data, cfg, self.state, now=clock.now, sleep=clock.sleep)

    def recorded(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["calls"]

    def test_first_network_call_allowed_without_sleep_and_recorded(self):
        clock = Clock(1000)
        code, msg = self.run_guard(call(NET, url="https://a.example/"), guard_cfg(min_interval_s=5), clock)
        self.assertEqual((code, msg), (0, ""))
        self.assertEqual(clock.slept, [])
        self.assertEqual(self.recorded(), [1000.0])

    def test_interval_sleeps_the_remainder_then_allows(self):
        clock = Clock(1000)
        cfg = guard_cfg(min_interval_s=5)
        self.run_guard(call(NET, url="https://a.example/"), cfg, clock)
        clock.t = 1001
        code, _ = self.run_guard(call(NET, url="https://a.example/2"), cfg, clock)
        self.assertEqual(code, 0)
        self.assertEqual(clock.slept, [4])
        self.assertEqual(self.recorded(), [1000.0, 1005.0])

    def test_interval_not_slept_when_gap_already_long_enough(self):
        clock = Clock(1000)
        cfg = guard_cfg(min_interval_s=5)
        self.run_guard(call(NET, url="https://a.example/"), cfg, clock)
        clock.t = 1100
        self.run_guard(call(NET, url="https://a.example/2"), cfg, clock)
        self.assertEqual(clock.slept, [])

    def test_non_network_guarded_tool_neither_sleeps_nor_counts(self):
        clock = Clock(1000)
        cfg = guard_cfg(min_interval_s=5, max_per_hour=1)
        self.run_guard(call(NET, url="https://a.example/"), cfg, clock)
        clock.t = 1001
        code, _ = self.run_guard(call(SNAP), cfg, clock)
        self.assertEqual(code, 0)
        self.assertEqual(clock.slept, [])
        self.assertEqual(self.recorded(), [1000.0])

    def test_unguarded_tool_is_allowed_and_untouched(self):
        clock = Clock(1000)
        code, _ = self.run_guard(call("Read", file_path="/x"), guard_cfg(max_per_hour=0), clock)
        self.assertEqual(code, 0)
        self.assertFalse(self.state.exists())

    def test_hourly_cap_blocks_with_hour_reason(self):
        clock = Clock(0)
        cfg = guard_cfg(max_per_hour=2)
        self.assertEqual(self.run_guard(call(NET, url="u"), cfg, clock)[0], 0)
        clock.t = 10
        self.assertEqual(self.run_guard(call(NET, url="u"), cfg, clock)[0], 0)
        clock.t = 20
        code, msg = self.run_guard(call(NET, url="u"), cfg, clock)
        self.assertEqual(code, BLOCK)
        self.assertEqual(msg, "browser budget reached for this hour; report the results you have")
        self.assertEqual(self.recorded(), [0.0, 10.0])

    def test_hourly_window_rolls(self):
        clock = Clock(0)
        cfg = guard_cfg(max_per_hour=1)
        self.run_guard(call(NET, url="u"), cfg, clock)
        clock.t = 3601
        self.assertEqual(self.run_guard(call(NET, url="u"), cfg, clock)[0], 0)

    def test_daily_cap_blocks_with_day_reason(self):
        clock = Clock(0)
        cfg = guard_cfg(max_per_day=3)
        for t in (0, 7200, 14400):
            clock.t = t
            self.assertEqual(self.run_guard(call(NET, url="u"), cfg, clock)[0], 0)
        clock.t = 21600
        code, msg = self.run_guard(call(NET, url="u"), cfg, clock)
        self.assertEqual(code, BLOCK)
        self.assertEqual(msg, "browser budget reached for today; report the results you have")

    def test_entries_older_than_24h_are_pruned_and_do_not_count(self):
        self.state.write_text(json.dumps({"calls": [0.0, 1.0]}), encoding="utf-8")
        clock = Clock(90000)
        code, _ = self.run_guard(call(NET, url="u"), guard_cfg(max_per_day=2), clock)
        self.assertEqual(code, 0)
        self.assertEqual(self.recorded(), [90000.0])

    def test_url_deny_pattern_blocks_and_records_nothing(self):
        clock = Clock(0)
        cfg = guard_cfg(deny_url_patterns=["mode=map"], min_interval_s=5)
        code, msg = self.run_guard(call(NET, url="https://maps.example/?q=x&mode=map"), cfg, clock)
        self.assertEqual(code, BLOCK)
        self.assertTrue(msg)
        self.assertEqual(clock.slept, [])
        self.assertFalse(self.state.exists())

    def test_url_deny_pattern_does_not_match_other_urls(self):
        clock = Clock(0)
        cfg = guard_cfg(deny_url_patterns=["mode=map"])
        self.assertEqual(self.run_guard(call(NET, url="https://example.org/page"), cfg, clock)[0], 0)

    def test_deny_tool_blocks_even_without_network_or_url(self):
        clock = Clock(0)
        cfg = guard_cfg(deny_tools=[SHOT])
        code, msg = self.run_guard(call(SHOT), cfg, clock)
        self.assertEqual(code, BLOCK)
        self.assertTrue(msg)
        self.assertFalse(self.state.exists())

    def test_deny_tool_only_affects_the_named_tool(self):
        clock = Clock(0)
        cfg = guard_cfg(deny_tools=[SHOT])
        self.assertEqual(self.run_guard(call(SNAP), cfg, clock)[0], 0)

    def test_corrupt_state_is_treated_as_empty(self):
        self.state.write_text("{not json", encoding="utf-8")
        clock = Clock(500)
        code, _ = self.run_guard(call(NET, url="u"), guard_cfg(max_per_hour=1), clock)
        self.assertEqual(code, 0)
        self.assertEqual(self.recorded(), [500.0])

    def test_state_with_wrong_shape_is_treated_as_empty(self):
        self.state.write_text(json.dumps({"calls": "nope"}), encoding="utf-8")
        clock = Clock(500)
        self.assertEqual(self.run_guard(call(NET, url="u"), guard_cfg(max_per_hour=1), clock)[0], 0)

    def test_state_write_is_atomic_and_leaves_no_tmp_file(self):
        clock = Clock(0)
        self.run_guard(call(NET, url="u"), guard_cfg(), clock)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["browser_guard_state.json"])

    def test_misconfigured_guard_blocks_guarded_calls(self):
        clock = Clock(0)
        code, msg = self.run_guard(call(NET, url="u"), {"tools": "mcp__browser__.*", "misconfigured": "bad"}, clock)
        self.assertEqual(code, BLOCK)
        self.assertTrue(msg)


class GuardScriptSubprocessTests(unittest.TestCase):
    """The exit-code and stderr contract Claude Code's PreToolUse hook sees."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bg-sub-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        shutil.copyfile(PLUGIN / "browser_guard.py", self.dir / "browser_guard.py")
        (self.dir / "browser_guard.json").write_text(json.dumps(guard_cfg(max_per_hour=1)), encoding="utf-8")

    def run_hook(self, data):
        return subprocess.run(
            [sys.executable, str(self.dir / "browser_guard.py")],
            input=json.dumps(data),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_allow_exits_0_silently_block_exits_2_with_reason_on_stderr(self):
        first = self.run_hook(call(NET, url="https://a.example/"))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        second = self.run_hook(call(NET, url="https://a.example/2"))
        self.assertEqual(second.returncode, BLOCK, second.stderr)
        self.assertIn("browser budget reached for this hour", second.stderr)
        self.assertEqual(second.stdout, "")

    def test_unreadable_config_fails_closed_with_exit_2(self):
        (self.dir / "browser_guard.json").write_text("{broken", encoding="utf-8")
        result = self.run_hook(call(NET, url="u"))
        self.assertEqual(result.returncode, BLOCK)
        self.assertTrue(result.stderr.strip())


class PrepareDirGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg-prep-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.tmp / "hermes-home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg.update(enabled=True, claude_bin="/bin/echo", home=str(self.tmp / "ccb"))

    def settings(self, d):
        return json.loads((d / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_no_hook_and_no_files_when_not_configured(self):
        d = session.prepare_dir(self.cfg, CID)
        self.assertEqual(set(self.settings(d)["hooks"]), {"Stop"})
        self.assertFalse((d / "browser_guard.py").exists())
        self.assertFalse((d / "browser_guard.json").exists())

    def test_hook_registered_with_guard_matcher_when_configured(self):
        self.cfg["browser_guard"] = guard_cfg(max_per_hour=120)
        d = session.prepare_dir(self.cfg, CID)
        pre = self.settings(d)["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0]["matcher"], "mcp__browser__.*")
        entry = pre[0]["hooks"][0]
        self.assertEqual(entry["type"], "command")
        self.assertEqual(
            entry["command"],
            f"{shlex.quote(sys.executable)} {shlex.quote(str(d / 'browser_guard.py'))}",
        )
        self.assertEqual((d / "browser_guard.py").read_bytes(), (PLUGIN / "browser_guard.py").read_bytes())
        self.assertIn("Stop", self.settings(d)["hooks"])

    def test_config_file_written_with_normalised_values(self):
        self.cfg["browser_guard"] = {
            "max_per_hour": 120,
            "max_per_day": 400,
            "min_interval_s": 5,
            "deny_url_patterns": ["mode=map"],
            "deny_tools": ["mcp__browser__browser_evaluate"],
        }
        d = session.prepare_dir(self.cfg, CID)
        written = json.loads((d / "browser_guard.json").read_text(encoding="utf-8"))
        self.assertEqual(written["tools"], "mcp__browser__.*")
        self.assertEqual(written["max_per_hour"], 120)
        self.assertEqual(written["max_per_day"], 400)
        self.assertEqual(written["min_interval_s"], 5.0)
        self.assertEqual(written["deny_url_patterns"], ["mode=map"])
        self.assertEqual(written["deny_tools"], ["mcp__browser__browser_evaluate"])
        self.assertEqual(written["network_tools"], "")

    def test_channel_override_merges_over_global_for_that_channel_only(self):
        self.cfg["browser_guard"] = {"max_per_day": 400}
        self.cfg["channels"] = {CID: {"browser_guard": {"max_per_hour": 10}}}
        d = session.prepare_dir(self.cfg, CID)
        written = json.loads((d / "browser_guard.json").read_text(encoding="utf-8"))
        self.assertEqual((written["max_per_hour"], written["max_per_day"]), (10, 400))
        other = session.prepare_dir(self.cfg, "999999")
        other_written = json.loads((other / "browser_guard.json").read_text(encoding="utf-8"))
        self.assertEqual((other_written["max_per_hour"], other_written["max_per_day"]), (None, 400))

    def test_channel_only_guard_leaves_other_channels_without_hook(self):
        self.cfg["channels"] = {CID: {"browser_guard": {"max_per_hour": 10}}}
        d = session.prepare_dir(self.cfg, CID)
        self.assertIn("PreToolUse", self.settings(d)["hooks"])
        other = session.prepare_dir(self.cfg, "999999")
        self.assertNotIn("PreToolUse", self.settings(other)["hooks"])
        self.assertFalse((other / "browser_guard.py").exists())

    def test_removing_guard_drops_hook_and_config_file(self):
        self.cfg["browser_guard"] = guard_cfg(max_per_hour=5)
        d = session.prepare_dir(self.cfg, CID)
        self.assertTrue((d / "browser_guard.json").exists())
        self.cfg["browser_guard"] = {}
        session.prepare_dir(self.cfg, CID)
        self.assertNotIn("PreToolUse", self.settings(d)["hooks"])
        self.assertFalse((d / "browser_guard.json").exists())

    def test_invalid_numeric_value_writes_misconfigured_marker(self):
        self.cfg["browser_guard"] = {"max_per_hour": "lots"}
        d = session.prepare_dir(self.cfg, CID)
        written = json.loads((d / "browser_guard.json").read_text(encoding="utf-8"))
        self.assertIn("misconfigured", written)
        self.assertIn("PreToolUse", self.settings(d)["hooks"])

    def test_guard_config_changes_the_launch_fingerprint(self):
        base = session.fingerprint(self.cfg, CID)
        self.cfg["browser_guard"] = guard_cfg(max_per_hour=120)
        with_guard = session.fingerprint(self.cfg, CID)
        self.assertNotEqual(base, with_guard)
        self.cfg["browser_guard"] = guard_cfg(max_per_hour=121)
        self.assertNotEqual(with_guard, session.fingerprint(self.cfg, CID))

    def test_unconfigured_fingerprint_is_unchanged_by_the_guard_feature(self):
        without = copy.deepcopy(self.cfg)
        self.cfg["browser_guard"] = {}
        self.assertEqual(session.fingerprint(self.cfg, CID), session.fingerprint(without, CID))


if __name__ == "__main__":
    unittest.main()
