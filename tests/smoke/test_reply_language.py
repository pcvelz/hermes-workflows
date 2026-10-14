"""reply-language: pre_llm_call injects the answer language from the channel's team."""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "reply-language" / "__init__.py"
spec = importlib.util.spec_from_file_location("reply_language_under_test", PLUGIN)
rl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rl)

TEAMS = {"team-a": "English", "team-b": "Spanish"}


def run(team, platform="mattermost", chat="c1", message="¿cuánto cuesta?"):
    rl._TEAM_CACHE.clear()
    with mock.patch.object(rl, "_teams", return_value=TEAMS), \
            mock.patch.object(rl, "_chat_id", return_value=chat), \
            mock.patch.object(rl, "_team_of_channel", return_value=team) as lookup:
        out = rl._on_pre_llm_call(platform=platform, user_message=message)
    return out, lookup


class ReplyLanguageTest(unittest.TestCase):
    def test_english_team(self):
        out, _ = run("team-a")
        self.assertIn("English", out["context"])

    def test_spanish_team(self):
        out, _ = run("team-b")
        self.assertIn("español", out["context"])

    def test_unmapped_team_gets_nothing(self):
        out, _ = run("team-c")
        self.assertIsNone(out)

    def test_other_platform_gets_nothing(self):
        out, lookup = run("team-a", platform="telegram")
        self.assertIsNone(out)
        lookup.assert_not_called()

    def test_lookup_failure_never_raises(self):
        rl._TEAM_CACHE.clear()
        with mock.patch.object(rl, "_teams", return_value=TEAMS), \
                mock.patch.object(rl, "_chat_id", return_value="c9"), \
                mock.patch.object(rl, "_team_of_channel", side_effect=RuntimeError("down")):
            self.assertIsNone(rl._on_pre_llm_call(platform="mattermost", user_message="hi"))

    def test_team_lookup_is_cached(self):
        rl._TEAM_CACHE.clear()
        with mock.patch.object(rl, "_teams", return_value=TEAMS), \
                mock.patch.object(rl, "_chat_id", return_value="c2"), \
                mock.patch.object(rl, "_team_of_channel", return_value="team-a") as lookup:
            rl._on_pre_llm_call(platform="mattermost", user_message="a")
            rl._on_pre_llm_call(platform="mattermost", user_message="b")
        self.assertEqual(lookup.call_count, 1)


if __name__ == "__main__":
    unittest.main()
