#!/usr/bin/env python3
"""Smoke tests for claude-code-bridge MCP wiring: lib/mcp.py and the MCP argv in lib/session.py.

No tmux, no network, no real claude. The key values below are throwaway test strings.
"""
import copy
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
sys.path.insert(0, str(PLUGIN))

from lib import config, mcp, session  # noqa: E402

CID = "chan1"
TEST_KEY = "test-key-value-0123"
TOOLS = ["mcp__composio_gmail__GMAIL_FETCH_EMAILS", "mcp__composio_gmail__GMAIL_GET_PROFILE"]
URL = "https://backend.example.test/v3/mcp/server-id?user_id=user-id"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccb-mcp-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.keys = self.tmp / "keys"
        (self.keys / "composio-gmail-mcp").mkdir(parents=True)
        (self.keys / "composio-gmail-mcp" / "api-key").write_text(TEST_KEY + "\n", encoding="utf-8")
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg.update(
            claude_bin=str(self.tmp / "fake_claude"),
            home=str(self.tmp / "claude-code-bridge"),
            keys_dir=str(self.keys),
            mcp_servers={
                "composio_gmail": {
                    "type": "http",
                    "url": URL,
                    "headers": {"x-api-key": "keyfile:composio-gmail-mcp/api-key"},
                }
            },
            mcp_allowed_tools=TOOLS,
        )
        self.chan = self.tmp / "claude-code-bridge" / "channels" / CID

    def channel_dir(self):
        self.chan.mkdir(parents=True, exist_ok=True)
        return self.chan

    def state(self):
        return {"session_id": "11111111-2222-3333-4444-555555555555", "started_once": False}


class KeyfileTests(_Base):
    def test_reads_and_strips_trailing_newline(self):
        self.assertEqual(mcp.resolve_keyfile("keyfile:composio-gmail-mcp/api-key", self.keys), TEST_KEY)

    def test_malformed_references_are_refused(self):
        for ref in ("keyfile:../secret", "keyfile:a", "keyfile:a/b/c", "keyfile:a/..", "keyfile:/etc/passwd"):
            with self.subTest(ref=ref), self.assertRaises(mcp.KeyUnavailable):
                mcp.resolve_keyfile(ref, self.keys)

    def test_missing_or_empty_key_is_unavailable(self):
        with self.assertRaises(mcp.KeyUnavailable):
            mcp.resolve_keyfile("keyfile:composio-gmail-mcp/nope", self.keys)
        (self.keys / "composio-gmail-mcp" / "empty").write_text("  \n", encoding="utf-8")
        with self.assertRaises(mcp.KeyUnavailable):
            mcp.resolve_keyfile("keyfile:composio-gmail-mcp/empty", self.keys)

    def test_keys_dir_defaults_to_hermes_home_keys(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.tmp / "home")}):
            self.assertEqual(config.keys_dir(cfg), self.tmp / "home" / "keys")


class RenderTests(_Base):
    def test_render_resolves_key_and_is_mode_0600(self):
        d = self.channel_dir()
        self.assertTrue(mcp.render(self.cfg, d))
        path = d / ".mcp.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        server = doc["mcpServers"]["composio_gmail"]
        self.assertEqual(server["type"], "http")
        self.assertEqual(server["url"], URL)
        self.assertEqual(server["headers"], {"x-api-key": TEST_KEY})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse((d / ".mcp.json.tmp").exists())

    def test_no_servers_removes_stale_file(self):
        d = self.channel_dir()
        (d / ".mcp.json").write_text("{}", encoding="utf-8")
        self.cfg["mcp_servers"] = {}
        self.assertFalse(mcp.render(self.cfg, d))
        self.assertFalse((d / ".mcp.json").exists())

    def test_missing_key_leaves_server_out_and_logs_without_secret(self):
        d = self.channel_dir()
        self.cfg["mcp_servers"]["composio_gmail"]["headers"]["x-api-key"] = "keyfile:composio-gmail-mcp/gone"
        with self.assertLogs("claude_code_bridge", level="WARNING") as logs:
            self.assertFalse(mcp.render(self.cfg, d))
        self.assertFalse((d / ".mcp.json").exists())
        self.assertTrue(any("composio_gmail" in line for line in logs.output))
        self.assertFalse(any(TEST_KEY in line for line in logs.output))

    def test_one_missing_key_keeps_other_servers(self):
        d = self.channel_dir()
        self.cfg["mcp_servers"]["other"] = {"type": "http", "url": URL, "headers": {}}
        self.cfg["mcp_servers"]["composio_gmail"]["headers"]["x-api-key"] = "keyfile:composio-gmail-mcp/gone"
        with self.assertLogs("claude_code_bridge", level="WARNING"):
            self.assertTrue(mcp.render(self.cfg, d))
        doc = json.loads((d / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(list(doc["mcpServers"]), ["other"])

    def test_non_mapping_spec_is_left_out(self):
        d = self.channel_dir()
        self.cfg["mcp_servers"] = {"bad": "not-a-mapping"}
        with self.assertLogs("claude_code_bridge", level="WARNING"):
            self.assertFalse(mcp.render(self.cfg, d))


class ArgvTests(_Base):
    def test_no_mcp_config_means_no_mcp_flags(self):
        self.cfg["mcp_servers"] = {}
        session.prepare_dir(self.cfg, CID)
        argv = session.launch_argv(self.cfg, CID, self.state())
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--strict-mcp-config", argv)
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertEqual(allowed, ",".join(config.DEFAULTS["allowed_tools"]))

    def test_mcp_flags_and_allowed_tools_when_rendered(self):
        session.prepare_dir(self.cfg, CID)
        argv = session.launch_argv(self.cfg, CID, self.state())
        self.assertEqual(argv[argv.index("--mcp-config") + 1], str(self.chan / ".mcp.json"))
        self.assertIn("--strict-mcp-config", argv)
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        for tool in TOOLS:
            self.assertIn(tool, allowed)
        self.assertIn("Read", allowed)

    def test_missing_key_session_still_builds_argv_without_mcp(self):
        self.cfg["mcp_servers"]["composio_gmail"]["headers"]["x-api-key"] = "keyfile:composio-gmail-mcp/gone"
        with self.assertLogs("claude_code_bridge", level="WARNING"):
            session.prepare_dir(self.cfg, CID)
            argv = session.launch_argv(self.cfg, CID, self.state())
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--strict-mcp-config", argv)
        self.assertNotIn("mcp__composio_gmail__GMAIL_FETCH_EMAILS", argv[argv.index("--allowedTools") + 1])
        self.assertEqual(argv[argv.index("--session-id") + 1], self.state()["session_id"])

    def test_prepare_dir_denies_model_read_of_mcp_file(self):
        session.prepare_dir(self.cfg, CID)
        settings = json.loads((self.chan / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"], {"deny": ["Read(./.mcp.json)"]})

    def test_prepare_dir_without_mcp_has_no_permissions_block(self):
        self.cfg["mcp_servers"] = {}
        session.prepare_dir(self.cfg, CID)
        settings = json.loads((self.chan / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(set(settings), {"hooks"})


if __name__ == "__main__":
    unittest.main()
