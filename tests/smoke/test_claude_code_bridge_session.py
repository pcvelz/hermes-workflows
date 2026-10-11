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

from lib import config, mcp, session, spool  # noqa: E402

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
        self.assertEqual(argv[13:15], ["--disallowedTools", ",".join(session.BUILT_IN_DENIED)])
        self.assertEqual(argv[15], "--mcp-config")
        self.assertEqual(argv[17], "--strict-mcp-config")
        self.assertEqual(argv[18:], ["--session-id", state["session_id"]])
        self.assertNotIn("--resume", argv)

    def test_resume_uses_resume_flag(self):
        cfg = self.make_cfg()
        state = {"session_id": "abc-session", "started_once": True}
        argv = session.launch_argv(cfg, CID, state)
        self.assertEqual(argv[17], "--strict-mcp-config")
        self.assertEqual(argv[18:], ["--resume", "abc-session"])
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
        self.assertEqual(
            settings["permissions"],
            {"deny": ["Read(./.mcp.json)", f"Read(/{d}/.mcp.json)"]},
        )


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
        unknown = self.make_cfg(channels={CID: {"bogus": [1], "temperature": "high"}})
        self.assertEqual(self._argv(unknown, CID), self._argv(base, CID))
        malformed = self.make_cfg(channels={CID: "not-a-mapping"})
        self.assertEqual(self._argv(malformed, CID), self._argv(base, CID))


DENY_KEYS = "Read(//home/alice/.hermes/keys/**)"
DENY_GLOBAL = "Read(//srv/global/**)"
DENY_CHANNEL = "Grep(//srv/channel/**)"


class DisallowedToolsTests(SessionTestCase):
    """disallowed_tools -> --disallowedTools: global list plus the channel's own; no flag when empty."""

    def _argv(self, cfg, cid):
        return session.launch_argv(cfg, cid, {"session_id": "11111111-2222-3333-4444-555555555555",
                                              "started_once": False})

    def test_only_built_in_denies_when_nothing_else_is_denied(self):
        cfg = self.make_cfg()
        self.assertEqual(cfg["disallowed_tools"], [])
        self.assertEqual(_flag(self._argv(cfg, CID), "--disallowedTools"), ",".join(session.BUILT_IN_DENIED))

    def test_global_denies_are_passed_as_one_flag(self):
        cfg = self.make_cfg(disallowed_tools=[DENY_GLOBAL, DENY_KEYS])
        argv = self._argv(cfg, CID)
        self.assertEqual(_flag(argv, "--disallowedTools"),
                         ",".join(session.BUILT_IN_DENIED + [DENY_GLOBAL, DENY_KEYS]))
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--session-id", argv)

    def test_channel_denies_merge_with_global_without_duplicates(self):
        cfg = self.make_cfg(disallowed_tools=[DENY_GLOBAL])
        cfg["channels"] = {CID: {"disallowed_tools": [DENY_CHANNEL, DENY_GLOBAL]}}
        self.assertEqual(_flag(self._argv(cfg, CID), "--disallowedTools"),
                         ",".join(session.BUILT_IN_DENIED + [DENY_GLOBAL, DENY_CHANNEL]))
        # The global config itself is not changed by the channel view.
        self.assertEqual(cfg["disallowed_tools"], [DENY_GLOBAL])

    def test_channel_denies_apply_to_that_channel_only(self):
        cfg = self.make_cfg()
        cfg["channels"] = {CID: {"disallowed_tools": [DENY_CHANNEL]}}
        self.assertEqual(_flag(self._argv(cfg, CID), "--disallowedTools"),
                         ",".join(session.BUILT_IN_DENIED + [DENY_CHANNEL]))
        self.assertEqual(_flag(self._argv(cfg, OTHER), "--disallowedTools"), ",".join(session.BUILT_IN_DENIED))

    def test_denies_change_the_launch_fingerprint(self):
        base = self.make_cfg()
        denied = self.make_cfg()
        denied["channels"] = {CID: {"disallowed_tools": [DENY_CHANNEL]}}
        self.assertNotEqual(session.fingerprint(denied, CID), session.fingerprint(base, CID))
        self.assertEqual(session.fingerprint(denied, OTHER), session.fingerprint(base, OTHER))

    def test_malformed_denies_are_ignored(self):
        base = self.make_cfg()
        cfg = self.make_cfg(channels={CID: {"disallowed_tools": 42}})
        self.assertEqual(self._argv(cfg, CID), self._argv(base, CID))


class BuiltInConnectorDenyTests(SessionTestCase):
    """The built-in Chrome connector is denied on every launch: --strict-mcp-config does not stop it."""

    def _argv(self, cfg, cid, has_mcp=False):
        return session._argv(cfg, cid, "11111111-2222-3333-4444-555555555555", has_mcp, False)

    def test_chrome_connector_is_denied_with_and_without_mcp_servers(self):
        cfg = self.make_cfg()
        for has_mcp in (False, True):
            denied = _flag(self._argv(cfg, CID, has_mcp), "--disallowedTools").split(",")
            self.assertIn("mcp__claude-in-chrome", denied)

    def test_chrome_connector_is_denied_once_when_channel_overrides_denies(self):
        cfg = self.make_cfg(disallowed_tools=[DENY_GLOBAL])
        cfg["channels"] = {CID: {"disallowed_tools": [DENY_CHANNEL]}}
        denied = _flag(self._argv(cfg, CID), "--disallowedTools").split(",")
        self.assertEqual(denied.count("mcp__claude-in-chrome"), 1)

    def test_chrome_connector_is_not_in_the_allow_list(self):
        cfg = self.make_cfg()
        allowed = _flag(self._argv(cfg, CID), "--allowedTools").split(",")
        self.assertFalse(any("claude-in-chrome" in t for t in allowed))


class ForChannelTests(unittest.TestCase):
    def test_channel_disallowed_tools_string_form_is_split(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["channels"] = {CID: {"disallowed_tools": "Read(//a/**), Grep(//b/**)"}}
        self.assertEqual(config.for_channel(cfg, CID)["disallowed_tools"],
                         ["Read(//a/**)", "Grep(//b/**)"])

    def test_returns_copy_and_leaves_global_untouched(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["channels"] = {CID: {"allowed_tools": [OVERRIDE_TOOL]}}
        before = copy.deepcopy(cfg)
        eff = config.for_channel(cfg, CID)
        self.assertIn(OVERRIDE_TOOL, eff["allowed_tools"])
        self.assertEqual(cfg, before)
        self.assertIsNot(eff, cfg)


class ChannelModelEffortArgvTests(SessionTestCase):
    """channels.<cid>.model / effort reach the launch argv and the launch fingerprint for that channel only."""

    def _cfg(self, **override):
        cfg = self.make_cfg(effort="high")
        if override:
            cfg["channels"] = {CID: override}
        return cfg

    def _argv(self, cfg, cid):
        return session.launch_argv(cfg, cid, {"session_id": "11111111-2222-3333-4444-555555555555",
                                              "started_once": False})

    def test_overridden_channel_argv_has_model_and_effort(self):
        argv = self._argv(self._cfg(model="sonnet", effort="medium"), CID)
        i = argv.index("--model")
        self.assertEqual(argv[i:i + 4], ["--model", "sonnet", "--effort", "medium"])

    def test_other_channel_keeps_global_model_and_effort(self):
        argv = self._argv(self._cfg(model="sonnet", effort="medium"), OTHER)
        self.assertEqual(_flag(argv, "--model"), config.DEFAULTS["model"])
        self.assertEqual(_flag(argv, "--effort"), "high")

    def test_fingerprint_differs_when_only_model_changes(self):
        base = self._cfg(effort="high")
        changed = self._cfg(model="sonnet", effort="high")
        self.assertNotEqual(session.fingerprint(changed, CID), session.fingerprint(base, CID))
        self.assertEqual(session.fingerprint(changed, OTHER), session.fingerprint(base, OTHER))


class SoulArgvTests(unittest.TestCase):
    """A soul_file's text reaches claude as one intact --append-system-prompt value, also through the shell tmux uses."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccb-soul-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hermes_home = self.tmp / "hermes-home"
        self.hermes_home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_multi_kb_soul_with_quotes_and_newlines_survives_the_shell(self):
        soul = (
            "Quotes ' and \" and $HOME and `ticks`, backslash \\, semicolon ; pipe | ampersand &.\n"
            + "Unicode ✓ guidance paragraph for the channel. " * 120
            + "\n\n- first bullet\n- second bullet\n"
        )
        self.assertGreater(len(soul.encode("utf-8")), 4000)
        (self.hermes_home / "soul.md").write_text(soul, encoding="utf-8")
        fake = self.tmp / "fake_claude.py"
        fake.write_text(
            f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8"
        )
        fake.chmod(0o755)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg.update(
            claude_bin=str(fake),
            home=str(self.tmp / "bridge"),
            channels={CID: {"soul_file": "soul.md"}},
        )
        state = {"session_id": "11111111-2222-3333-4444-555555555555", "started_once": False}
        argv = session.launch_argv(cfg, CID, state)
        expected = config.for_channel(cfg, CID)["append_system_prompt"]
        self.assertIn(soul.strip(), expected)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], expected)
        # The same string tmux new-session receives: shlex.join, then run by the shell.
        out = subprocess.run(
            ["sh", "-c", shlex.join(argv)],
            env=session.clean_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        received = json.loads(out)
        self.assertEqual(received[received.index("--append-system-prompt") + 1], expected)


class LargePromptLaunchTests(SessionTestCase):
    """A large per-channel soul reaches claude through a launch script, so the tmux command stays short."""

    def setUp(self):
        super().setUp()
        self.soul = "Guidance line with 'quotes', $dollars and `ticks`.\n" * 400
        self.assertGreaterEqual(len(self.soul.encode("utf-8")), 20_000)
        (self.tmp / "hermes-home").mkdir(parents=True, exist_ok=True)
        (self.tmp / "hermes-home" / "soul.md").write_text(self.soul, encoding="utf-8")

    def test_twenty_kb_prompt_gives_short_tmux_command_and_script_carries_prompt(self):
        cfg = self.make_cfg(channels={CID: {"soul_file": "soul.md"}})
        real_tmux = session._tmux
        calls = []

        def record(c, *args, check=False):
            calls.append(args)
            return real_tmux(c, *args, check=check)

        with mock.patch.object(session, "_tmux", side_effect=record):
            self.assertTrue(session.start(cfg, CID, self.new_state()))
        new_session = next(a for a in calls if a and a[0] == "new-session")
        command = new_session[-1]
        self.assertLess(len(command), 300)
        self.assertEqual(shlex.split(command)[0], "/bin/sh")
        script_path = Path(shlex.split(command)[1])
        self.assertEqual(script_path.name, "launch.sh")
        self.assertEqual(script_path.stat().st_mode & 0o777, 0o700)
        body = script_path.read_text(encoding="utf-8")  # the prompt spans many lines, so split the whole text
        self.assertEqual(body.count("\nexec "), 1)
        argv = shlex.split(body.split("\nexec ", 1)[1])
        expected = config.for_channel(cfg, CID)["append_system_prompt"]
        self.assertIn(self.soul.strip(), expected)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], expected)

    def test_failed_new_session_logs_truncated_stderr(self):
        cfg = self.make_cfg()
        real_tmux = session._tmux
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="command too long " + "x" * 500)

        def fake(c, *args, check=False):
            if args and args[0] == "new-session":
                return failed
            return real_tmux(c, *args, check=check)

        with mock.patch.object(session, "_tmux", side_effect=fake), \
                self.assertLogs("claude_code_bridge", "WARNING") as logs:
            self.assertFalse(session.start(cfg, CID, self.new_state()))
        message = "\n".join(logs.output)
        self.assertIn("new-session failed for channel " + CID, message)
        self.assertIn("command too long", message)
        self.assertNotIn("x" * 201, message)


class LargePromptFingerprintTests(unittest.TestCase):
    """The launch fingerprint covers the full prompt text, not just the launch script path."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccb-fp-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hermes_home = self.tmp / "hermes-home"
        self.hermes_home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_change_inside_twenty_kb_soul_changes_fingerprint(self):
        soul = "Guidance line for the channel.\n" * 700
        self.assertGreaterEqual(len(soul.encode("utf-8")), 20_000)
        (self.hermes_home / "soul.md").write_text(soul, encoding="utf-8")
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg.update(claude_bin="/usr/bin/true", home=str(self.tmp / "bridge"),
                   channels={CID: {"soul_file": "soul.md"}})
        before = session.fingerprint(cfg, CID)
        self.assertEqual(before, session.fingerprint(cfg, CID))
        (self.hermes_home / "soul.md").write_text(soul[:10_000] + "Z" + soul[10_001:], encoding="utf-8")
        self.assertNotEqual(before, session.fingerprint(cfg, CID))


class RepoChannelArgvTests(SessionTestCase):
    """A channel with a cwd is a normal Claude Code session in that repo: no bridge narrowing flags."""

    STATE = {"session_id": "11111111-2222-3333-4444-555555555555", "started_once": False}

    def _repo(self):
        repo = self.tmp / "repo"
        (repo / ".claude" / "docs").mkdir(parents=True)
        return repo

    def _cfg(self, repo, mcp_servers=None, **extra):
        channels = {CID: {"cwd": str(repo), **extra}}
        return self.make_cfg(channels=channels, allowed_tools=["Read"], disallowed_tools=["Bash(rm:*)"],
                             mcp_servers=mcp_servers or {})

    def test_repo_channel_is_plain_claude_with_no_permission_or_setting_sources_flags(self):
        argv = session.launch_argv(self._cfg(self._repo()), CID, self.STATE)
        self.assertNotIn("--setting-sources", argv)
        self.assertNotIn("--permission-mode", argv)  # the user's own settings decide the rights
        self.assertNotIn("--allowedTools", argv)

    def test_repo_channel_denies_only_the_built_in_connectors_and_monitor(self):
        argv = session.launch_argv(self._cfg(self._repo()), CID, self.STATE)
        self.assertEqual(_flag(argv, "--disallowedTools"), ",".join(session.BUILT_IN_DENIED))
        self.assertNotIn("Bash(rm:*)", _flag(argv, "--disallowedTools"))  # the channel's disallowed_tools is not applied

    def test_repo_channel_mcp_servers_get_no_allow_rule_from_the_bridge(self):
        servers = {"composio": {"type": "http", "url": "https://example.invalid/mcp"},
                   "browser": {"type": "http", "url": "https://example.invalid/b"}}
        argv = session.launch_argv(self._cfg(self._repo(), mcp_servers=servers), CID, self.STATE)
        self.assertNotIn("--allowedTools", argv)
        self.assertIn("--mcp-config", argv)  # the channel's servers still load; their tools need the user's settings

    def test_repo_channel_keeps_settings_mcp_and_soul(self):
        cfg = self._cfg(self._repo(), append_system_prompt="SOUL-TEXT")
        argv = session.launch_argv(cfg, CID, {"session_id": "11111111-2222-3333-4444-555555555555", "started_once": False})
        spool_dir = spool.channel_dir(cfg, CID)
        self.assertEqual(_flag(argv, "--settings"), str(spool_dir / ".claude" / "settings.json"))
        self.assertEqual(_flag(argv, "--mcp-config"), str(spool_dir / mcp.MCP_FILE))
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("SOUL-TEXT", _flag(argv, "--append-system-prompt"))

    def test_repo_channel_stop_hook_is_registered_in_the_settings_it_loads(self):
        cfg = self._cfg(self._repo())
        d = session.prepare_dir(cfg, CID)
        settings = json.loads((d / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("Stop", settings["hooks"])
        self.assertIn("stop_hook.py", settings["hooks"]["Stop"][0]["hooks"][0]["command"])

    def test_spool_channel_keeps_the_narrowing_flags(self):
        cfg = self.make_cfg(allowed_tools=["Read"])
        argv = session.launch_argv(cfg, CID, {"session_id": "11111111-2222-3333-4444-555555555555", "started_once": False})
        self.assertEqual(_flag(argv, "--setting-sources"), "project,local")
        self.assertEqual(_flag(argv, "--permission-mode"), cfg["permission_mode"])
        self.assertIn("Read", _flag(argv, "--allowedTools"))
        self.assertNotIn("--settings", argv)

    def test_missing_cwd_falls_back_to_spool_flags(self):
        cfg = self._cfg(self.tmp / "does-not-exist")
        argv = session.launch_argv(cfg, CID, {"session_id": "11111111-2222-3333-4444-555555555555", "started_once": False})
        self.assertEqual(_flag(argv, "--setting-sources"), "project,local")
        self.assertNotIn("--settings", argv)


class RepoPermissionModeTests(SessionTestCase):
    """repo_permission_mode: unset = plain repo launch (user's settings decide); a valid mode adds --permission-mode
    to repo channels only. Spool channels keep their locked dontAsk launch whatever the knob says."""

    STATE = {"session_id": "11111111-2222-3333-4444-555555555555", "started_once": False}

    def _repo(self, name="repo"):
        repo = self.tmp / name
        repo.mkdir(exist_ok=True)
        return repo

    def test_unset_is_a_plain_repo_launch(self):
        self.assertEqual(config.DEFAULTS["repo_permission_mode"], "")
        argv = session.launch_argv(self.make_cfg(channels={CID: {"cwd": str(self._repo())}}), CID, self.STATE)
        self.assertNotIn("--permission-mode", argv)

    def test_global_valid_mode_is_passed_to_repo_channels(self):
        cfg = self.make_cfg(repo_permission_mode="bypassPermissions", channels={CID: {"cwd": str(self._repo())}})
        argv = session.launch_argv(cfg, CID, self.STATE)
        self.assertEqual(_flag(argv, "--permission-mode"), "bypassPermissions")

    def test_channel_override_applies_to_that_channel_only(self):
        cfg = self.make_cfg(channels={CID: {"cwd": str(self._repo("a"))}, "999999": {"cwd": str(self._repo("b"))}})
        cfg["channels"][CID]["repo_permission_mode"] = "acceptEdits"
        self.assertEqual(_flag(session.launch_argv(cfg, CID, self.STATE), "--permission-mode"), "acceptEdits")
        self.assertNotIn("--permission-mode", session.launch_argv(cfg, "999999", self.STATE))

    def test_unknown_mode_is_warned_once_and_ignored(self):
        cfg = self.make_cfg(repo_permission_mode="yolo", channels={CID: {"cwd": str(self._repo())}})
        with self.assertLogs("claude_code_bridge", level="WARNING") as logs:
            argv = session.launch_argv(cfg, CID, self.STATE)
            session.launch_argv(cfg, CID, self.STATE)
        self.assertNotIn("--permission-mode", argv)
        self.assertEqual(sum("yolo" in m for m in logs.output), 1)

    def test_spool_channel_stays_locked_even_when_the_knob_is_set(self):
        cfg = self.make_cfg(repo_permission_mode="bypassPermissions", allowed_tools=["Read"])
        argv = session.launch_argv(cfg, CID, self.STATE)
        self.assertEqual(_flag(argv, "--setting-sources"), "project,local")
        self.assertEqual(_flag(argv, "--permission-mode"), cfg["permission_mode"])
        self.assertNotEqual(_flag(argv, "--permission-mode"), "bypassPermissions")
        self.assertIn("Read", _flag(argv, "--allowedTools"))


class ClaudeArgsTests(SessionTestCase):
    """claude_args: a list appended verbatim to the launch argv, globally and per channel (added together)."""

    STATE = {"session_id": "11111111-2222-3333-4444-555555555555", "started_once": False}

    def test_default_is_empty(self):
        self.assertEqual(config.DEFAULTS["claude_args"], [])

    def test_appended_verbatim_before_the_session_id_on_a_repo_channel(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        cfg = self.make_cfg(claude_args=["--permission-mode", "acceptEdits"],
                            channels={CID: {"cwd": str(repo)}})
        argv = session.launch_argv(cfg, CID, self.STATE)
        i = argv.index("--permission-mode")
        self.assertEqual(argv[i:i + 2], ["--permission-mode", "acceptEdits"])
        self.assertLess(i, argv.index("--session-id"))

    def test_appended_before_resume_on_a_spool_channel(self):
        cfg = self.make_cfg(claude_args=["--add-dir", "/tmp/x"])
        argv = session.launch_argv(cfg, CID, {**self.STATE, "started_once": True})
        self.assertLess(argv.index("--add-dir"), argv.index("--resume"))
        self.assertEqual(argv[-2:], ["--resume", self.STATE["session_id"]])

    def test_channel_args_add_to_global_for_that_channel_only(self):
        cfg = self.make_cfg(claude_args=["--global-flag"], channels={CID: {"claude_args": ["--channel-flag"]}})
        eff = config.for_channel(cfg, CID)["claude_args"]
        self.assertEqual(eff, ["--global-flag", "--channel-flag"])
        self.assertEqual(config.for_channel(cfg, "999999")["claude_args"], ["--global-flag"])

    def test_claude_args_change_the_launch_fingerprint(self):
        base = session.fingerprint(self.make_cfg(), CID)
        self.assertNotEqual(base, session.fingerprint(self.make_cfg(claude_args=["--x"]), CID))


if __name__ == "__main__":
    unittest.main()
