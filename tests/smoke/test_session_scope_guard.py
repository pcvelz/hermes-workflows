"""session-scope-guard: in chat only the asking user's own sessions may be read."""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "session-scope-guard" / "__init__.py"
spec = importlib.util.spec_from_file_location("session_scope_guard_under_test", PLUGIN)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def call(args, platform="mattermost", user="u-asker", owner="u-asker", owner_error=False):
    env = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_USER_ID": user}
    owner_patch = (mock.patch.object(g, "_owner", side_effect=RuntimeError("db down")) if owner_error
                   else mock.patch.object(g, "_owner", return_value=owner))
    with mock.patch.object(g, "_env", side_effect=lambda k: env.get(k, "")), owner_patch:
        return g._on_pre_tool_call(tool_name="session_search", args=args)


def blocked(res):
    return isinstance(res, dict) and res.get("action") == "block"


class SessionScopeGuardTest(unittest.TestCase):
    def test_discovery_and_browse_are_blocked_in_chat(self):
        self.assertTrue(blocked(call({"query": "ventilator"})))
        self.assertTrue(blocked(call({})))

    def test_profile_argument_is_blocked(self):
        self.assertTrue(blocked(call({"session_id": "s1", "profile": "work"})))

    def test_other_users_session_is_blocked_own_session_allowed(self):
        self.assertTrue(blocked(call({"session_id": "s1"}, owner="u-other")))
        self.assertIsNone(call({"session_id": "s1"}, owner="u-asker"))
        self.assertIsNone(call({"session_id": "s1", "around_message_id": 7}, owner="u-asker"))

    def test_unowned_session_is_blocked(self):
        self.assertTrue(blocked(call({"session_id": "s1"}, owner=None)))

    def test_outside_chat_everything_is_allowed(self):
        self.assertIsNone(call({"query": "x"}, platform=""))

    def test_errors_fail_closed_in_chat_open_outside(self):
        self.assertTrue(blocked(call({"session_id": "s1"}, owner_error=True)))
        self.assertIsNone(call({"session_id": "s1"}, platform="", owner_error=True))

    def test_block_logs_a_line(self):
        with self.assertLogs("session_scope_guard", level="WARNING") as cm:
            call({"query": "x"})
        self.assertIn("session-scope-guard: blocked session_search", cm.output[0])


class RealOwnerLookupTest(unittest.TestCase):
    """No mocks of _owner: the real SessionDB on a temp database, two owners."""

    def test_owner_reads_real_session_user_id(self):
        import tempfile
        import hermes_state
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            db = hermes_state.SessionDB(db_path=db_path)
            try:
                db.create_session("s-a", source="mattermost", user_id="u-a")
                db.create_session("s-b", source="mattermost", user_id="u-b")
            finally:
                db.close()
            # _owner() opens SessionDB() on the module default path; point that default at the temp db.
            with mock.patch.object(hermes_state, "DEFAULT_DB_PATH", db_path):
                self.assertEqual(g._owner("s-a"), "u-a")
                self.assertEqual(g._owner("s-b"), "u-b")
                self.assertIsNone(g._owner("no-such-session"))


if __name__ == "__main__":
    unittest.main()
