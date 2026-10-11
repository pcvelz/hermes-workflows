#!/usr/bin/env python3
"""Smoke tests for the mattermost-auto-vacate plugin.

No test touches the network: ``urllib.request.urlopen`` is replaced by a fake Mattermost
REST server (``FakeMattermost``) that routes the handful of endpoints the sweep uses. The
real ``Client`` and sweep code run against it, so URLs, methods and payloads are exercised.
A temporary HERMES_HOME holds config and state.
"""
import copy
import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "mattermost-auto-vacate"
sys.path.insert(0, str(PLUGIN_DIR))

import config  # noqa: E402
import vacate  # noqa: E402

BASE = "https://mm.example.test"
TOKEN = "test-token-not-real"
LOGGER = "mattermost_auto_vacate"


class _Resp:
    def __init__(self, body: bytes):
        self._body = body
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeMattermost:
    """Minimal in-memory Mattermost: bot user, humans, other bots, teams and channels."""

    def __init__(self):
        self.bot = "bot-user-id"
        self.users = {self.bot: True, "human-1": False, "human-2": False, "other-bot": True}
        self.teams = {"team-a": {"id": "team-a", "name": "team-a", "display_name": "Team A"}}
        self.channels = {}
        self.fail = []  # (method, path-prefix) pairs answered with HTTP 500
        self.calls = []  # (method, path) for every request
        self.deletes = []  # (channel_id, user_id) for every DELETE that succeeded

    def add_channel(self, cid, name, ctype="O", members=(), team="team-a"):
        self.channels[cid] = {
            "id": cid,
            "name": name,
            "display_name": name,
            "type": ctype,
            "team_id": team,
            "members": set(members) | {self.bot},
        }

    def _http_error(self, url, code):
        return urllib.error.HTTPError(url, code, "fake error", None, io.BytesIO(b"{}"))

    def urlopen(self, req, timeout=None):
        url = urllib.parse.urlsplit(req.full_url)
        path = url.path
        query = urllib.parse.parse_qs(url.query)
        method = req.get_method()
        self.calls.append((method, path))
        for fail_method, prefix in self.fail:
            if fail_method == method and path.startswith(prefix):
                raise self._http_error(req.full_url, 500)
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        parts = path.split("/")  # ['', 'api', 'v4', ...]

        if method == "GET" and path == "/api/v4/users/me":
            return _Resp(json.dumps({"id": self.bot, "is_bot": True}).encode())
        if method == "GET" and path == "/api/v4/users/me/teams":
            return _Resp(json.dumps(list(self.teams.values())).encode())
        if method == "GET" and len(parts) == 8 and parts[5] == "teams" and parts[7] == "channels":
            team = parts[6]
            rows = [
                {k: v for k, v in ch.items() if k != "members"}
                for ch in self.channels.values()
                if ch["team_id"] == team and self.bot in ch["members"]
            ]
            return _Resp(json.dumps(rows).encode())
        if method == "GET" and len(parts) == 6 and parts[3] == "channels" and parts[5] == "members":
            ch = self.channels.get(parts[4])
            if ch is None:
                raise self._http_error(req.full_url, 404)
            page = int(query.get("page", ["0"])[0])
            per_page = int(query.get("per_page", ["60"])[0])
            ordered = sorted(ch["members"])
            rows = [{"channel_id": parts[4], "user_id": u} for u in ordered[page * per_page:(page + 1) * per_page]]
            return _Resp(json.dumps(rows).encode())
        if method == "POST" and path == "/api/v4/users/ids":
            rows = [{"id": u, "is_bot": self.users[u]} for u in (body or []) if u in self.users]
            return _Resp(json.dumps(rows).encode())
        if method == "DELETE" and len(parts) == 7 and parts[3] == "channels" and parts[5] == "members":
            ch = self.channels.get(parts[4])
            if ch is None or parts[6] not in ch["members"]:
                raise self._http_error(req.full_url, 404)
            ch["members"].discard(parts[6])
            self.deletes.append((parts[4], parts[6]))
            return _Resp(b"")
        raise self._http_error(req.full_url, 404)

    def member_ids(self, cid):
        return set(self.channels[cid]["members"])


class VacateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.tmp / "hermes-home")
        self.addCleanup(self._restore_home)
        self.addCleanup(self._tmp.cleanup)
        self.fake = FakeMattermost()
        patcher = mock.patch("urllib.request.urlopen", side_effect=self.fake.urlopen)
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = 10_000.0
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.state_path = self.tmp / "hermes-home" / "mattermost-auto-vacate" / "state.json"

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home

    def sweep(self, at=None):
        if at is not None:
            self.clock = at
        return vacate.run_once(self.cfg, BASE, TOKEN, self.state_path, now=lambda: self.clock)

    def leave_deletes(self, cid):
        return [d for d in self.fake.deletes if d[0] == cid]


class SweepBehaviourTests(VacateTests):
    def test_alone_below_grace_stays(self):
        self.fake.add_channel("research-1", "research-1")
        self.sweep(at=10_000.0)
        self.sweep(at=10_000.0 + 300)  # 300 s < grace_s 600
        self.assertEqual(self.leave_deletes("research-1"), [])
        self.assertIn(self.fake.bot, self.fake.member_ids("research-1"))

    def test_alone_past_grace_leaves_once_and_logs_one_line(self):
        self.fake.add_channel("research-2", "research-2")
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.sweep(at=10_000.0)
            self.sweep(at=10_000.0 + 300)
            self.sweep(at=10_000.0 + 600)  # alone for 600 s == grace_s: leaves
            self.sweep(at=10_000.0 + 900)  # bot is gone from the channel: no second leave
        self.assertEqual(self.leave_deletes("research-2"), [("research-2", self.fake.bot)])
        left = [line for line in logs.output if "left channel" in line]
        self.assertEqual(len(left), 1)
        self.assertIn("left channel research-2 (team team-a), sole member for 600s", left[0])
        self.assertNotIn("research-2", json.loads(self.state_path.read_text()))

    def test_state_file_under_hermes_home(self):
        self.fake.add_channel("research-3", "research-3")
        self.sweep(at=10_000.0)
        self.assertTrue(self.state_path.exists())
        self.assertEqual(self.state_path.parent.name, "mattermost-auto-vacate")
        self.assertIn("research-3", json.loads(self.state_path.read_text()))

    def test_human_rejoining_resets_timer(self):
        self.fake.add_channel("research-4", "research-4")
        self.sweep(at=10_000.0)  # alone since t0
        self.fake.channels["research-4"]["members"].add("human-1")
        self.sweep(at=10_000.0 + 300)  # human present: timer reset
        self.fake.channels["research-4"]["members"].discard("human-1")
        self.sweep(at=10_000.0 + 600)  # alone again: timer restarts here
        self.sweep(at=10_000.0 + 1000)  # 400 s alone: below grace
        self.assertEqual(self.leave_deletes("research-4"), [])
        self.sweep(at=10_000.0 + 1200)  # 600 s alone since the restart
        self.assertEqual(self.leave_deletes("research-4"), [("research-4", self.fake.bot)])

    def test_excluded_names_and_direct_group_channels_are_skipped(self):
        self.fake.add_channel("ts", "town-square")
        self.fake.add_channel("ot", "off-topic")
        self.fake.add_channel("dm", "dm-channel", ctype="D")
        self.fake.add_channel("gm", "group-channel", ctype="G")
        self.sweep(at=10_000.0)
        self.sweep(at=10_000.0 + 3600)
        for cid in ("ts", "ot", "dm", "gm"):
            self.assertEqual(self.leave_deletes(cid), [], cid)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state, {})

    def test_other_bots_do_not_count_as_humans(self):
        self.fake.add_channel("research-5", "research-5", members={"other-bot"})
        self.sweep(at=10_000.0)
        self.sweep(at=10_000.0 + 600)
        self.assertEqual(self.leave_deletes("research-5"), [("research-5", self.fake.bot)])
        self.assertEqual(self.fake.member_ids("research-5"), {"other-bot"})  # the other bot is untouched

    def test_humans_present_keep_bot_and_other_members_untouched(self):
        self.fake.add_channel("chat-1", "chat-1", members={"human-1", "human-2", "other-bot"})
        self.sweep(at=10_000.0)
        self.sweep(at=10_000.0 + 3600)
        self.assertEqual(self.leave_deletes("chat-1"), [])
        self.assertEqual(self.fake.member_ids("chat-1"), {"human-1", "human-2", "other-bot", self.fake.bot})
        self.assertEqual(self.fake.deletes, [])

    def test_dry_run_logs_but_does_not_leave(self):
        self.cfg["dry_run"] = True
        self.fake.add_channel("research-6", "research-6")
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.sweep(at=10_000.0)
            self.sweep(at=10_000.0 + 600)
        self.assertEqual(self.leave_deletes("research-6"), [])
        self.assertIn(self.fake.bot, self.fake.member_ids("research-6"))
        self.assertTrue(any("would leave channel research-6" in line for line in logs.output))
        self.assertFalse(any("left channel" in line for line in logs.output))

    def test_rest_error_on_members_is_retried_next_sweep(self):
        self.fake.add_channel("research-7", "research-7")
        self.sweep(at=10_000.0)
        self.fake.fail.append(("GET", "/api/v4/channels/research-7/members"))
        with self.assertLogs(LOGGER, level="WARNING"):
            self.sweep(at=10_000.0 + 600)
        self.assertEqual(self.leave_deletes("research-7"), [])
        self.fake.fail.clear()
        self.sweep(at=10_000.0 + 900)  # the timer survived the failed sweep
        self.assertEqual(self.leave_deletes("research-7"), [("research-7", self.fake.bot)])

    def test_failed_leave_is_retried_next_sweep(self):
        self.fake.add_channel("research-8", "research-8")
        self.sweep(at=10_000.0)
        self.fake.fail.append(("DELETE", "/api/v4/channels/research-8/members"))
        with self.assertLogs(LOGGER, level="WARNING"):
            self.sweep(at=10_000.0 + 600)
        self.assertEqual(self.leave_deletes("research-8"), [])
        self.fake.fail.clear()
        self.sweep(at=10_000.0 + 900)
        self.assertEqual(self.leave_deletes("research-8"), [("research-8", self.fake.bot)])

    def test_team_listing_error_does_not_prune_state(self):
        self.fake.add_channel("research-9", "research-9")
        self.sweep(at=10_000.0)
        self.fake.fail.append(("GET", "/api/v4/users/me/teams"))
        with self.assertLogs(LOGGER, level="WARNING"):
            self.sweep(at=10_000.0 + 300)
        self.assertIn("research-9", json.loads(self.state_path.read_text()))
        self.assertEqual(self.leave_deletes("research-9"), [])


class ConfigAndPluginTests(VacateTests):
    def _write_config(self, text):
        home = Path(os.environ["HERMES_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(text, encoding="utf-8")

    def test_defaults_when_block_missing(self):
        self._write_config("model: {}\n")
        cfg = config.load()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["interval_s"], 300)
        self.assertEqual(cfg["grace_s"], 600)
        self.assertEqual(cfg["exclude"], ["town-square", "off-topic"])
        self.assertFalse(cfg["dry_run"])

    def test_block_overrides_defaults(self):
        self._write_config(
            "mattermost_auto_vacate:\n  enabled: false\n  interval_s: 60\n  grace_s: 120\n"
            "  exclude: [lobby]\n  dry_run: true\n"
        )
        cfg = config.load()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["interval_s"], 60)
        self.assertEqual(cfg["grace_s"], 120)
        self.assertEqual(cfg["exclude"], ["lobby"])
        self.assertTrue(cfg["dry_run"])

    def test_unparseable_config_fails_closed(self):
        self._write_config("mattermost_auto_vacate: [unclosed\n")
        with self.assertLogs(LOGGER, level="WARNING"):
            cfg = config.load()
        self.assertFalse(cfg["enabled"])

    def _load_plugin(self):
        spec = importlib.util.spec_from_file_location(
            "mattermost_auto_vacate_plugin", PLUGIN_DIR / "__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_disabled_makes_no_calls(self):
        self._write_config("mattermost_auto_vacate:\n  enabled: false\n")
        plugin = self._load_plugin()
        with mock.patch.dict(os.environ, {"MATTERMOST_URL": BASE, "MATTERMOST_TOKEN": TOKEN}):
            plugin.register(object())  # no thread is started when disabled
            self.assertEqual(plugin._cycle(), 300.0)  # an idle cycle only, no REST calls
        self.urlopen.assert_not_called()
        self.assertIsNone(plugin._thread)

    def test_missing_credentials_warn_once_and_stop_without_crash(self):
        self._write_config("model: {}\n")
        plugin = self._load_plugin()
        with mock.patch.dict(os.environ, {"MATTERMOST_URL": "", "MATTERMOST_TOKEN": ""}):
            with self.assertLogs(LOGGER, level="WARNING") as logs:
                self.assertIsNone(plugin._cycle())
        self.assertEqual(len([line for line in logs.output if "WARNING" in line]), 1)
        self.urlopen.assert_not_called()

    def test_cycle_runs_a_sweep_with_env_credentials(self):
        self._write_config("model: {}\n")
        self.fake.add_channel("research-10", "research-10")
        plugin = self._load_plugin()
        with mock.patch.dict(os.environ, {"MATTERMOST_URL": BASE, "MATTERMOST_TOKEN": TOKEN}):
            self.assertEqual(plugin._cycle(), 300)
        self.assertTrue(self.urlopen.called)
        self.assertEqual(self.leave_deletes("research-10"), [])


if __name__ == "__main__":
    unittest.main()
