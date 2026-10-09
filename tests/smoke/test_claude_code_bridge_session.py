"""Smoke tests for plugins/claude-code-bridge/lib/session.py.

Never starts a real `claude`: a fake claude_bin script stands in for it, running
inside a private tmux server (dedicated -L socket) that is killed in tearDown.
"""
import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN))

from lib import config, session  # noqa: E402

FAKE_TEMPLATE = r'''#!{python}
import sys, time
sys.stdout.reconfigure(encoding="utf-8")
LOG = {log!r}

def out(s):
    sys.stdout.write(s)
    sys.stdout.flush()

out("Fake Claude Code banner\n")
out("❯ ")
while True:
    line = sys.stdin.readline()
    if not line:
        break
    text = line.rstrip("\n")
    if text.strip() == "/exit":
        break
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    out("\nesc to interrupt\n")
    time.sleep(1)
    out("\x1b[2J\x1b[H")
    out("⏺ ok\n")
    out("❯ ")
'''

CID = "test-chan-1"


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccb-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "claude-code-bridge"
        self.sock = f"claude-code-bridge-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.log_file = self.tmp / "received.txt"
        self.fake = self.tmp / "fake_claude.py"
        self.fake.write_text(
            FAKE_TEMPLATE.format(python=sys.executable, log=str(self.log_file)),
            encoding="utf-8",
        )
        self.fake.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.tmp / "hermes-home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._kill_server)

    def _kill_server(self):
        subprocess.run(
            ["tmux", "-L", self.sock, "kill-server"],
            env=session.clean_env(),
            capture_output=True,
        )

    def make_cfg(self, **overrides):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg.update(
            enabled=True,
            claude_bin=str(self.fake),
            home=str(self.home),
            tmux_socket=self.sock,
        )
        cfg.update(overrides)
        return cfg

    def new_state(self):
        return {"session_id": None, "started_once": False, "inflight": None,
                "inflight_since": None, "last_activity": 0}


class LaunchArgvTests(SessionTestCase):
    def test_new_session_uses_session_id(self):
        cfg = self.make_cfg()
        state = {"session_id": "11111111-2222-3333-4444-555555555555", "started_once": False}
        argv = session.launch_argv(cfg, CID, state)
        self.assertEqual(argv[0], str(self.fake))
        self.assertEqual(argv[1:3], ["--model", "haiku"])
        self.assertEqual(argv[3:5], ["--effort", "medium"])
        self.assertEqual(argv[5:7], ["--setting-sources", "project,local"])
        self.assertEqual(argv[7:9], ["--permission-mode", "dontAsk"])
        self.assertEqual(argv[9:11], ["--allowedTools", "Read,Glob,Grep,WebSearch,WebFetch"])
        self.assertEqual(argv[11], "--append-system-prompt")
        self.assertEqual(argv[12], cfg["append_system_prompt"])
        self.assertEqual(argv[13], "--mcp-config")
        self.assertEqual(argv[15], "--strict-mcp-config")
        self.assertEqual(argv[16:], ["--session-id", state["session_id"]])
        self.assertNotIn("--resume", argv)

    def test_resume_uses_resume_flag(self):
        cfg = self.make_cfg()
        state = {"session_id": "abc-session", "started_once": True}
        argv = session.launch_argv(cfg, CID, state)
        self.assertEqual(argv[15], "--strict-mcp-config")
        self.assertEqual(argv[16:], ["--resume", "abc-session"])
        self.assertNotIn("--session-id", argv)

    def test_missing_session_id_generates_uuid(self):
        cfg = self.make_cfg()
        argv = session.launch_argv(cfg, CID, {"started_once": False})
        sid = argv[argv.index("--session-id") + 1]
        self.assertEqual(str(uuid.UUID(sid)), sid)


class CleanEnvTests(SessionTestCase):
    def test_drops_chat_and_provider_secrets(self):
        extra = {
            "MATTERMOST_TOKEN": "x-token",
            "MATTERMOST_URL": "x-url",
            "ANTHROPIC_API_KEY": "x-key",
            "ANTHROPIC_BASE_URL": "x-base",
            "OPENAI_API_KEY": "x-key2",
        }
        with mock.patch.dict(os.environ, extra):
            env = session.clean_env()
        for key in extra:
            self.assertNotIn(key, env)
        self.assertEqual(env["TERM"], "xterm-256color")
        self.assertTrue(set(env) <= {"HOME", "USER", "LOGNAME", "PATH", "SHELL",
                                     "LANG", "TMPDIR", "TERM"})


class PrepareDirTests(SessionTestCase):
    def test_writes_settings_and_hook_copy(self):
        cfg = self.make_cfg()
        d = session.prepare_dir(cfg, CID)
        self.assertEqual(d, self.home / "channels" / CID)
        self.assertTrue((d / "inbox").is_dir())
        self.assertTrue((d / "outbox").is_dir())

        hook = d / "stop_hook.py"
        src = PLUGIN / "stop_hook.py"
        self.assertTrue(hook.is_file())
        self.assertEqual(hook.read_bytes(), src.read_bytes())

        settings = json.loads((d / ".claude" / "settings.json").read_text(encoding="utf-8"))
        stop = settings["hooks"]["Stop"]
        self.assertEqual(len(stop), 1)
        entry = stop[0]["hooks"][0]
        self.assertEqual(entry["type"], "command")
        expected = f"{shlex.quote(sys.executable)} {shlex.quote(str(hook))}"
        self.assertEqual(entry["command"], expected)
        self.assertEqual(set(settings), {"hooks", "permissions"})
        self.assertEqual(settings["permissions"], {"deny": ["Read(./.mcp.json)"]})


class PaneParsingTests(unittest.TestCase):
    IDLE = "Fake banner\n\n❯ \n"
    TYPED = "Fake banner\n\n❯ draft text here\n"
    BUSY = "⏺ working\n✻ Thinking… (3s · esc to interrupt)\n❯ \n"
    NO_PROMPT = "Do you trust the files in this folder?\n1. Yes, proceed\n"
    BORDERED = "❯ hi there\n────────\n? for shortcuts\n"
    UPPER_BUSY = "ESC TO INTERRUPT"

    def test_is_busy(self):
        self.assertFalse(session.is_busy(self.IDLE))
        self.assertFalse(session.is_busy(self.TYPED))
        self.assertTrue(session.is_busy(self.BUSY))
        self.assertTrue(session.is_busy(self.UPPER_BUSY))
        self.assertFalse(session.is_busy(""))

    def test_box_text(self):
        self.assertEqual(session.box_text(self.IDLE), "")
        self.assertEqual(session.box_text(self.TYPED), "draft text here")
        self.assertEqual(session.box_text(self.BUSY), "")
        self.assertEqual(session.box_text(self.NO_PROMPT), "")
        self.assertEqual(session.box_text(self.BORDERED), "hi there")
        self.assertEqual(session.box_text(""), "")


class DimSpanTests(unittest.TestCase):
    # Claude Code renders its placeholder/ghost suggestion in dim (SGR 2) text.
    PLACEHOLDER = "\x1b[39m❯ \x1b[2mTry\x1b[0m \x1b[2m\"fix\x1b[0m"
    TYPED = "\x1b[39m❯ draft text\x1b[0m"

    def test_placeholder_dim_spans_leave_empty_box_text(self):
        stripped = session.strip_styled(self.PLACEHOLDER)
        self.assertNotIn("Try", stripped)
        self.assertNotIn("fix", stripped)
        self.assertEqual(session.box_text(stripped), "")

    def test_non_dim_typed_text_is_kept(self):
        stripped = session.strip_styled(self.TYPED)
        self.assertEqual(stripped, "❯ draft text")
        self.assertEqual(session.box_text(stripped), "draft text")

    def test_capture_drops_dim_spans_from_pane(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        raw = subprocess.CompletedProcess(args=[], returncode=0, stdout=self.PLACEHOLDER, stderr="")
        with mock.patch.object(session, "_tmux", return_value=raw):
            pane = session.capture(cfg, CID)
        self.assertNotIn("Try", pane)
        self.assertEqual(session.box_text(pane), "")


class TrustDialogTests(SessionTestCase):
    TRUST = "Do you trust the files in this folder?\n❯ No, exit\n  Yes, proceed\n"
    IDLE = "❯ \n"

    def test_no_exit_preselected_gets_down_before_enter(self):
        cfg = self.make_cfg()
        state = self.new_state()
        with mock.patch.object(session, "exists", return_value=True), \
                mock.patch.object(session, "capture", side_effect=[self.TRUST, self.IDLE]), \
                mock.patch.object(session, "is_idle", return_value=True), \
                mock.patch.object(session, "_tmux") as tmux, \
                mock.patch.object(session.time, "sleep"):
            self.assertTrue(session.start(cfg, CID, state))
        keys = [c.args for c in tmux.call_args_list if "send-keys" in c.args]
        self.assertEqual([a[-1] for a in keys], ["Down", "Enter"])
        for a in keys:
            self.assertEqual(a[1:4], ("send-keys", "-t", session._pane(cfg, CID)))
        self.assertTrue(state["started_once"])


class TmuxSessionTests(SessionTestCase):
    def test_start_idle_inject_busy_then_idle(self):
        cfg = self.make_cfg()
        state = self.new_state()

        self.assertTrue(session.start(cfg, CID, state))
        self.assertTrue(state["started_once"])
        self.assertTrue(state["session_id"])
        self.assertTrue(session.exists(cfg, CID))
        self.assertTrue(session.is_idle(cfg, CID))

        text = "hello from the chat side"
        self.assertTrue(session.inject(cfg, CID, text))

        saw_busy = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if session.is_busy(session.capture(cfg, CID)):
                saw_busy = True
                break
            time.sleep(0.1)
        self.assertTrue(saw_busy, "pane never showed the busy marker after inject")

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not session.is_idle(cfg, CID):
            time.sleep(0.2)
        self.assertTrue(session.is_idle(cfg, CID), "session did not return to idle")

        received = self.log_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(received, [text])

        session.stop(cfg, CID)

    def test_stop_kills_session(self):
        cfg = self.make_cfg()
        state = self.new_state()
        self.assertTrue(session.start(cfg, CID, state))
        self.assertTrue(session.exists(cfg, CID))

        session.stop(cfg, CID)
        self.assertFalse(session.exists(cfg, CID))


class LaunchFingerprintTests(unittest.TestCase):
    """session.fingerprint: stable for the same launch config, changes with launch-relevant config."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccb-fp-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        keys = self.tmp / "keys" / "svc"
        keys.mkdir(parents=True)
        (keys / "api-key").write_text("fp-test-key\n", encoding="utf-8")
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg.update(
            claude_bin=str(self.tmp / "fake_claude"),
            home=str(self.tmp / "claude-code-bridge"),
            keys_dir=str(self.tmp / "keys"),
            mcp_servers={"gmail": {"type": "http", "url": "https://mcp.example.test/a",
                                   "headers": {"x-api-key": "keyfile:svc/api-key"}}},
            mcp_allowed_tools=["mcp__gmail__FETCH"],
        )

    def test_same_config_same_fingerprint_regardless_of_session_state(self):
        fp = session.fingerprint(self.cfg, CID)
        self.assertEqual(fp, session.fingerprint(copy.deepcopy(self.cfg), CID))
        # Session identity and resume state are not launch config.
        session.launch_argv(self.cfg, CID, {"session_id": "abc", "started_once": True})
        self.assertEqual(fp, session.fingerprint(self.cfg, CID))

    def test_fingerprint_does_not_write_the_mcp_file(self):
        session.fingerprint(self.cfg, CID)
        self.assertFalse((self.tmp / "claude-code-bridge" / "channels" / CID / ".mcp.json").exists())

    def test_changed_allowed_tools_changes_fingerprint(self):
        fp = session.fingerprint(self.cfg, CID)
        self.cfg["allowed_tools"] = ["Read"]
        self.assertNotEqual(fp, session.fingerprint(self.cfg, CID))

    def test_changed_mcp_server_changes_fingerprint(self):
        fp = session.fingerprint(self.cfg, CID)
        self.cfg["mcp_servers"]["gmail"]["url"] = "https://mcp.example.test/b"
        self.assertNotEqual(fp, session.fingerprint(self.cfg, CID))

    def test_changed_mcp_key_name_changes_fingerprint(self):
        fp = session.fingerprint(self.cfg, CID)
        self.cfg["mcp_servers"] = {"gmail2": self.cfg["mcp_servers"]["gmail"]}
        self.assertNotEqual(fp, session.fingerprint(self.cfg, CID))

    def test_changed_key_file_content_changes_fingerprint(self):
        fp = session.fingerprint(self.cfg, CID)
        (self.tmp / "keys" / "svc" / "api-key").write_text("fp-test-key-rotated\n", encoding="utf-8")
        self.assertNotEqual(fp, session.fingerprint(self.cfg, CID))


OTHER = "test-chan-2"
OVERRIDE_TOOL = "Edit(./NOTES.md)"
OVERRIDE_PROMPT = "Your notes live in NOTES.md; keep them current."


def _flag(argv, flag):
    return argv[argv.index(flag) + 1]


class ChannelOverrideTests(SessionTestCase):
    """channels.<cid> in claude_code_bridge: extra allowed_tools / append_system_prompt for that channel only."""

    def _with_override(self, **override):
        cfg = self.make_cfg()
        cfg["channels"] = {CID: {"allowed_tools": [OVERRIDE_TOOL], "append_system_prompt": OVERRIDE_PROMPT,
                                 **override}}
        return cfg

    def _argv(self, cfg, cid):
        return session.launch_argv(cfg, cid, {"session_id": "11111111-2222-3333-4444-555555555555",
                                              "started_once": False})

    def test_no_channels_key_argv_unchanged(self):
        base = self.make_cfg()
        base.pop("channels", None)
        argv = self._argv(base, CID)
        self.assertEqual(_flag(argv, "--allowedTools"), "Read,Glob,Grep,WebSearch,WebFetch")
        self.assertEqual(_flag(argv, "--append-system-prompt"), config.DEFAULTS["append_system_prompt"])
        # An empty channels block is the same as no block.
        empty = self.make_cfg(channels={})
        self.assertEqual(self._argv(empty, CID), argv)
        self.assertEqual(self._argv(empty, OTHER), self._argv(base, OTHER))

    def test_override_channel_gets_extra_tools_and_prompt(self):
        cfg = self._with_override()
        argv = self._argv(cfg, CID)
        self.assertEqual(_flag(argv, "--allowedTools"),
                         "Read,Glob,Grep,WebSearch,WebFetch," + OVERRIDE_TOOL)
        self.assertEqual(_flag(argv, "--append-system-prompt"),
                         config.DEFAULTS["append_system_prompt"] + "\n\n" + OVERRIDE_PROMPT)
        # The global config is never mutated by the per-channel view.
        self.assertEqual(cfg["allowed_tools"], config.DEFAULTS["allowed_tools"])
        self.assertEqual(cfg["append_system_prompt"], config.DEFAULTS["append_system_prompt"])

    def test_other_channel_unaffected(self):
        cfg = self._with_override()
        base = self.make_cfg()
        base.pop("channels", None)
        self.assertEqual(self._argv(cfg, OTHER), self._argv(base, OTHER))
        self.assertNotIn(OVERRIDE_TOOL, _flag(self._argv(cfg, OTHER), "--allowedTools"))

    def test_fingerprint_differs_only_for_overridden_channel(self):
        cfg = self._with_override()
        base = self.make_cfg()
        base.pop("channels", None)
        self.assertNotEqual(session.fingerprint(cfg, CID), session.fingerprint(base, CID))
        self.assertEqual(session.fingerprint(cfg, OTHER), session.fingerprint(base, OTHER))

    def test_empty_prompt_override_keeps_global_prompt(self):
        cfg = self._with_override(append_system_prompt="")
        argv = self._argv(cfg, CID)
        self.assertEqual(_flag(argv, "--append-system-prompt"), config.DEFAULTS["append_system_prompt"])
        self.assertIn(OVERRIDE_TOOL, _flag(argv, "--allowedTools"))

    def test_unknown_subkeys_and_malformed_override_ignored(self):
        base = self.make_cfg()
        base.pop("channels", None)
        unknown = self.make_cfg(channels={CID: {"model": "opus", "bogus": [1]}})
        self.assertEqual(self._argv(unknown, CID), self._argv(base, CID))
        malformed = self.make_cfg(channels={CID: "not-a-mapping"})
        self.assertEqual(self._argv(malformed, CID), self._argv(base, CID))


class ForChannelTests(unittest.TestCase):
    def test_returns_copy_and_leaves_global_untouched(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["channels"] = {CID: {"allowed_tools": [OVERRIDE_TOOL]}}
        before = copy.deepcopy(cfg)
        eff = config.for_channel(cfg, CID)
        self.assertIn(OVERRIDE_TOOL, eff["allowed_tools"])
        self.assertEqual(cfg, before)
        self.assertIsNot(eff, cfg)


if __name__ == "__main__":
    unittest.main()
