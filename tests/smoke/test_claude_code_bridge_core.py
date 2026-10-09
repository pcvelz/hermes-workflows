"""Smoke tests for the claude-code-bridge core: config, spool, stop hook and poster.

No test starts Claude Code or touches the network. Temp directories stand in for
HERMES_HOME and the channel home; the Mattermost REST call is mocked.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from lib import config, poster, spool  # noqa: E402


class _TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class ConfigTests(_TempDirCase):
    def setUp(self):
        super().setUp()
        self.hermes_home = self.root / "hermes-home"
        self.hermes_home.mkdir()
        self._env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        super().tearDown()

    def test_defaults_are_fail_closed(self):
        cfg = config.load()  # no config.yaml yet -> pure defaults
        self.assertIs(cfg["enabled"], False)
        self.assertEqual(cfg["model"], "haiku")
        self.assertEqual(cfg["effort"], "medium")
        self.assertEqual(cfg["transport"], "tmux")
        self.assertEqual(cfg["session_prefix"], "claude-code-bridge-")
        self.assertEqual(cfg["idle_exit_minutes"], 60)
        self.assertEqual(cfg["permission_mode"], "dontAsk")
        self.assertEqual(cfg["allowed_tools"], ["Read", "Glob", "Grep", "WebSearch", "WebFetch"])
        self.assertEqual(cfg["max_reply_chars"], 15000)
        self.assertEqual(cfg["allowed_users"], [])
        self.assertEqual(cfg["allowed_channels"], [])
        self.assertEqual(cfg["wait_budget_minutes"], 360)
        self.assertEqual(cfg["tmux_socket"], "claude-code-bridge")
        self.assertEqual(cfg["poll_seconds"], 2)
        self.assertIn("plain text", cfg["append_system_prompt"])

    def test_load_returns_copy_not_shared_defaults(self):
        cfg = config.load()
        cfg["allowed_users"].append("mutated")
        self.assertEqual(config.DEFAULTS["allowed_users"], [])
        self.assertEqual(config.load()["allowed_users"], [])

    def test_overlay_from_hermes_home_config(self):
        (self.hermes_home / "config.yaml").write_text(
            "model: ignored-top-level\n"
            "claude_code_bridge:\n"
            "  enabled: true\n"
            "  model: sonnet\n"
            "  allowed_users: [alice]\n"
            "  allowed_channels: [chan-1]\n"
            "  max_reply_chars: 900\n",
            encoding="utf-8",
        )
        cfg = config.load()
        self.assertIs(cfg["enabled"], True)
        self.assertEqual(cfg["model"], "sonnet")
        self.assertEqual(cfg["allowed_users"], ["alice"])
        self.assertEqual(cfg["allowed_channels"], ["chan-1"])
        self.assertEqual(cfg["max_reply_chars"], 900)
        # keys not present in the file keep their defaults
        self.assertEqual(cfg["effort"], "medium")
        self.assertEqual(cfg["tmux_socket"], "claude-code-bridge")

    def test_missing_claude_code_bridge_key_yields_defaults(self):
        (self.hermes_home / "config.yaml").write_text("other: 1\n", encoding="utf-8")
        self.assertEqual(config.load(), config.DEFAULTS)

    def test_parse_error_yields_defaults_and_warns(self):
        (self.hermes_home / "config.yaml").write_text("claude_code_bridge: [unclosed\n", encoding="utf-8")
        with self.assertLogs("claude_code_bridge", level="WARNING"):
            cfg = config.load()
        self.assertEqual(cfg, config.DEFAULTS)

    def test_cache_refreshes_when_file_changes(self):
        path = self.hermes_home / "config.yaml"
        path.write_text("claude_code_bridge:\n  model: haiku\n", encoding="utf-8")
        self.assertEqual(config.load()["model"], "haiku")
        path.write_text("claude_code_bridge:\n  model: opus\n  effort: high\n", encoding="utf-8")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000_000))
        cfg = config.load()
        self.assertEqual(cfg["model"], "opus")
        self.assertEqual(cfg["effort"], "high")

    def test_home_defaults_under_hermes_home(self):
        cfg = config.load()
        self.assertEqual(config.home(cfg), self.hermes_home / "claude-code-bridge")

    def test_home_uses_configured_value(self):
        cfg = dict(config.DEFAULTS, home=str(self.root / "bridge"))
        self.assertEqual(config.home(cfg), self.root / "bridge")

    def test_claude_bin_prefers_configured_then_path(self):
        self.assertEqual(config.claude_bin({"claude_bin": "/opt/x/claude"}), "/opt/x/claude")
        with mock.patch("lib.config.shutil.which", return_value="/usr/bin/claude") as which:
            self.assertEqual(config.claude_bin({"claude_bin": ""}), "/usr/bin/claude")
            which.assert_called_once_with("claude")
        with mock.patch("lib.config.shutil.which", return_value=None):
            self.assertIsNone(config.claude_bin({"claude_bin": ""}))


class SpoolTests(_TempDirCase):
    def setUp(self):
        super().setUp()
        self.cfg = dict(config.DEFAULTS, home=str(self.root / "bridge"))

    def test_channel_dir_creates_inbox_and_outbox(self):
        d = spool.channel_dir(self.cfg, "chan-1")
        self.assertEqual(d, self.root / "bridge" / "channels" / "chan-1")
        self.assertTrue((d / "inbox").is_dir())
        self.assertTrue((d / "outbox").is_dir())

    def test_cid_validation(self):
        good = ["a", "Chan_1-x", "A" * 64]
        for cid in good:
            self.assertTrue(spool.channel_dir(self.cfg, cid).is_dir())
        bad = ["", "A" * 65, "has space", "../escape", "slash/x", "dot.ted", "new\nline", None, 7]
        for cid in bad:
            with self.subTest(cid=cid):
                with self.assertRaises(ValueError):
                    spool.channel_dir(self.cfg, cid)
        with self.assertRaises(ValueError):
            spool.enqueue(self.cfg, "../x", {"text": "nope"})

    def test_enqueue_writes_item_and_pending_is_oldest_first(self):
        first = spool.enqueue(self.cfg, "c1", {"post_id": "p1", "root_id": "", "user": "u", "text": "one", "ts": 1})
        second = spool.enqueue(self.cfg, "c1", {"post_id": "p2", "root_id": "r", "user": "u", "text": "two", "ts": 2})
        third = spool.enqueue(self.cfg, "c1", {"post_id": "p3", "root_id": None, "user": "u", "text": "three", "ts": 3})
        self.assertEqual(first.parent.name, "inbox")
        self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["text"], "one")
        self.assertEqual(spool.pending(self.cfg, "c1"), [first, second, third])
        # no temp files left behind
        self.assertEqual(sorted(p.name for p in first.parent.iterdir() if p.name.startswith(".")), [])

    def test_pop_reads_and_removes(self):
        path = spool.enqueue(self.cfg, "c1", {"post_id": "p1", "root_id": None, "user": "u", "text": "hi", "ts": 5})
        item = spool.pop(path)
        self.assertEqual(item["post_id"], "p1")
        self.assertEqual(item["text"], "hi")
        self.assertFalse(path.exists())
        self.assertEqual(spool.pending(self.cfg, "c1"), [])

    def test_pending_order_is_numeric_across_digit_lengths(self):
        inbox = spool.channel_dir(self.cfg, "c1") / "inbox"
        (inbox / "999.json").write_text(json.dumps({"text": "older"}), encoding="utf-8")
        (inbox / "1000000000000000000.json").write_text(json.dumps({"text": "newer"}), encoding="utf-8")
        self.assertEqual([p.stem for p in spool.pending(self.cfg, "c1")], ["999", "1000000000000000000"])

    def test_channels_lists_channel_dirs(self):
        self.assertEqual(spool.channels(self.cfg), [])
        spool.channel_dir(self.cfg, "beta")
        spool.channel_dir(self.cfg, "alpha")
        self.assertEqual(spool.channels(self.cfg), ["alpha", "beta"])

    def test_state_defaults_and_roundtrip(self):
        state = spool.load_state(self.cfg, "c1")
        self.assertEqual(state, {
            "session_id": None,
            "started_once": False,
            "inflight": None,
            "inflight_since": None,
            "last_activity": 0,
        })
        state["session_id"] = "abc"
        state["inflight"] = {"post_id": "p1"}
        spool.save_state(self.cfg, "c1", state)
        again = spool.load_state(self.cfg, "c1")
        self.assertEqual(again["session_id"], "abc")
        self.assertEqual(again["inflight"], {"post_id": "p1"})
        self.assertEqual(again["started_once"], False)

    def test_state_unreadable_falls_back_to_defaults(self):
        d = spool.channel_dir(self.cfg, "c1")
        (d / "state.json").write_text("{not json", encoding="utf-8")
        with self.assertLogs("claude_code_bridge", level="WARNING"):
            state = spool.load_state(self.cfg, "c1")
        self.assertIsNone(state["session_id"])

    def test_take_reply_returns_oldest_and_none_when_empty(self):
        self.assertIsNone(spool.take_reply(self.cfg, "c1"))
        outbox = spool.channel_dir(self.cfg, "c1") / "outbox"
        (outbox / "200.json").write_text(json.dumps({"text": "second"}), encoding="utf-8")
        (outbox / "100.json").write_text(json.dumps({"text": "first"}), encoding="utf-8")
        self.assertEqual(spool.take_reply(self.cfg, "c1")["text"], "first")
        self.assertEqual(spool.take_reply(self.cfg, "c1")["text"], "second")
        self.assertIsNone(spool.take_reply(self.cfg, "c1"))


class StopHookTests(_TempDirCase):
    def setUp(self):
        super().setUp()
        self.channel = self.root / "channel"
        self.channel.mkdir()
        shutil.copy(PLUGIN_DIR / "stop_hook.py", self.channel / "stop_hook.py")

    def _run(self, stdin_text):
        return subprocess.run(
            [sys.executable, str(self.channel / "stop_hook.py")],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=str(self.channel),
            timeout=30,
        )

    def test_writes_outbox_file_with_reply_text(self):
        payload = {
            "session_id": "11111111-2222-3333-4444-555555555555",
            "last_assistant_message": "Hello there, here is the answer.",
            "stop_hook_active": False,
            "effort": "medium",
        }
        result = self._run(json.dumps(payload))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        files = list((self.channel / "outbox").glob("*.json"))
        self.assertEqual(len(files), 1)
        written = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(written["text"], "Hello there, here is the answer.")
        self.assertEqual(written["session_id"], "11111111-2222-3333-4444-555555555555")
        self.assertIn("ts", written)

    def test_missing_message_writes_empty_text(self):
        result = self._run(json.dumps({"session_id": "s1"}))
        self.assertEqual(result.returncode, 0)
        files = list((self.channel / "outbox").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8"))["text"], "")

    def test_invalid_stdin_exits_zero_and_writes_nothing(self):
        result = self._run("not json at all")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        outbox = self.channel / "outbox"
        self.assertEqual(list(outbox.glob("*.json")) if outbox.exists() else [], [])


class PosterTests(_TempDirCase):
    ENV = {"MATTERMOST_URL": "https://chat.example.test", "MATTERMOST_TOKEN": "test-token-xyz"}

    def _ok_response(self, status=201):
        response = mock.MagicMock()
        response.status = status
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def test_truncate_passes_short_text_through(self):
        self.assertEqual(poster.truncate("short text", 100), "short text")
        self.assertEqual(poster.truncate("exactly", 7), "exactly")

    def test_truncate_cuts_at_last_space_and_appends_ellipsis(self):
        text = "alpha beta gamma delta epsilon"
        out = poster.truncate(text, 16)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 16)
        self.assertEqual(out, "alpha beta…")

    def test_truncate_prefers_newline_boundary(self):
        out = poster.truncate("first line\nsecond part", 12)
        self.assertEqual(out, "first line…")
        self.assertLessEqual(len(out), 12)

    def test_truncate_hard_cut_when_no_boundary(self):
        out = poster.truncate("x" * 50, 10)
        self.assertEqual(out, "x" * 9 + "…")
        self.assertEqual(len(out), 10)

    def test_post_sends_truncated_message_and_thread(self):
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch("urllib.request.urlopen", return_value=self._ok_response()) as urlopen:
            ok = poster.post("chan-1", "root-9", "word " * 40, 30)
        self.assertTrue(ok)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://chat.example.test/api/v4/posts")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token-xyz")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 20)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["channel_id"], "chan-1")
        self.assertEqual(body["root_id"], "root-9")
        self.assertLessEqual(len(body["message"]), 30)
        self.assertTrue(body["message"].endswith("…"))

    def test_post_omits_root_id_when_falsy(self):
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch("urllib.request.urlopen", return_value=self._ok_response()) as urlopen:
            self.assertTrue(poster.post("chan-1", None, "hello", 100))
        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertNotIn("root_id", body)
        self.assertEqual(body["message"], "hello")

    def test_empty_or_whitespace_text_never_posts(self):
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch("urllib.request.urlopen") as urlopen:
            self.assertFalse(poster.post("chan-1", "r", "", 100))
            self.assertFalse(poster.post("chan-1", "r", "   \n\t ", 100))
            self.assertFalse(poster.post("chan-1", "r", None, 100))
        urlopen.assert_not_called()

    def test_network_error_returns_false_without_leaking_token(self):
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("boom")), \
                self.assertLogs("claude_code_bridge", level="WARNING") as logs:
            self.assertFalse(poster.post("chan-1", "r", "hi", 100))
        self.assertFalse(any("test-token-xyz" in line for line in logs.output))

    def test_missing_credentials_never_posts(self):
        with mock.patch.dict(os.environ, {"MATTERMOST_URL": "", "MATTERMOST_TOKEN": ""}), \
                mock.patch("urllib.request.urlopen") as urlopen:
            self.assertFalse(poster.post("chan-1", "r", "hi", 100))
        urlopen.assert_not_called()


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
