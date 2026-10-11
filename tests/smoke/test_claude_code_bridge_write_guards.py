"""Smoke tests for the claude-code-bridge write guards: repo_scope_guard.py and public_first_guard.py.

Ported from the reference hook suites (test-guard-repo-scope.sh, test-block-private-copy-of-public.sh): the same
case matrix, on temporary fixture trees (no real paths). Each guard runs as a subprocess with the hook JSON on
stdin, exactly as Claude Code runs it: exit 2 = denied (message on stderr), exit 0 = allowed. The session
install (lib/session.py) is covered too: the hooks written to settings.json, and the config keys.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "claude-code-bridge"
REPO_SCOPE = PLUGIN / "repo_scope_guard.py"
PUBLIC_FIRST = PLUGIN / "public_first_guard.py"
sys.path.insert(0, str(PLUGIN))

from lib import config, session  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def run_guard(script, args, payload):
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        input=json.dumps(payload), capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stderr


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.T = Path(os.path.realpath(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def git_init(self, path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)


class RepoScopeTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.C = self.T / "Code"
        self.C.mkdir()
        for repo in ("hermes", "hermes-workflows", "backend-repo", "config-hub", "home-repo"):
            self.git_init(self.C / repo)
        for d in ("hermes/deploy", "hermes-workflows/config", "backend-repo/llama",
                  "config-hub/projects/hermes/.claude", "config-hub/projects/home-repo/.claude/docs",
                  "config-hub/projects/_shared/.claude/hooks", "home-repo/custom"):
            (self.C / d).mkdir(parents=True, exist_ok=True)
        os.symlink(self.C / "config-hub/projects/hermes/.claude", self.C / "hermes/.claude")
        os.symlink(self.C / "config-hub/projects/home-repo/.claude", self.C / "home-repo/.claude")
        subprocess.run([*GIT, "-C", str(self.C / "hermes"), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(self.C / "hermes"), "worktree", "add", "-q", str(self.T / "wt-hermes"), "-b", "wt"],
                       check=True)
        self.H = self.C / "hermes"
        self.HA = self.C / "home-repo"

    def check(self, want, session_dir, tool, key, path, cwd=None, companions=None):
        args = ["--root", str(self.C), "--session-dir", str(session_dir)]
        for c in companions or []:
            args += ["--companion", str(c)]
        payload = {"cwd": str(cwd or session_dir), "tool_name": tool, "tool_input": {key: str(path)}}
        rc, _ = run_guard(REPO_SCOPE, args, payload)
        self.assertEqual(rc, want, f"{tool} {path} from {session_dir}")

    def hermes(self, *a, **k):
        return self.check(*a, companions=[self.C / "hermes-workflows"], **k)

    def test_hermes_session_foreign_repos_are_blocked(self):
        self.hermes(2, self.H, "Write", "file_path", self.C / "backend-repo/llama/x.sh")
        self.hermes(2, self.H, "Edit", "file_path", self.C / "config-hub/projects/_shared/.claude/hooks/x.sh")
        self.hermes(2, self.H, "Write", "file_path", self.C / "home-repo/custom/x.py")
        self.hermes(2, self.H, "NotebookEdit", "notebook_path", self.C / "backend-repo/n.ipynb")
        self.hermes(2, self.H, "Write", "file_path", f"{self.H}/../backend-repo/x")
        self.hermes(2, self.H, "Write", "file_path", self.C / "hermes-other/x")
        self.hermes(2, self.H / "deploy", "Write", "file_path", self.C / "backend-repo/x")

    def test_hermes_session_own_repo_and_companions_pass(self):
        self.hermes(0, self.H, "Write", "file_path", self.H / "deploy/x.yaml")
        self.hermes(0, self.H / "deploy", "Write", "file_path", self.H / "deploy/new/y.yaml")
        self.hermes(0, self.H, "Write", "file_path", self.C / "hermes-workflows/config/board.yaml")
        self.hermes(0, self.H, "Edit", "file_path", self.H / ".claude/CLAUDE.md")
        self.hermes(0, self.H, "Edit", "file_path", self.C / "config-hub/projects/hermes/.claude/CLAUDE.md")
        self.hermes(0, self.T / "wt-hermes", "Write", "file_path", self.H / "deploy/x")
        self.hermes(0, self.H, "Write", "file_path", "/tmp/x")
        self.hermes(0, self.H, "Write", "file_path", self.T / "elsewhere/x")
        self.hermes(0, self.H, "Write", "file_path", "")

    def test_home_repo_session(self):
        self.check(0, self.HA, "Write", "file_path", self.HA / ".claude/docs/a.md")
        self.check(0, self.HA, "Write", "file_path", self.C / "config-hub/projects/home-repo/.claude/docs/a.md")
        self.check(2, self.HA, "Write", "file_path", self.C / "config-hub/projects/hermes/.claude/CLAUDE.md")
        self.check(2, self.HA, "Write", "file_path", self.C / "config-hub/projects/_shared/.claude/hooks/x.sh")
        self.check(2, self.HA, "Write", "file_path", self.C / "hermes-workflows/x")

    def test_master_template_session_own_repo_only(self):
        mt = self.C / "config-hub"
        self.check(0, mt, "Write", "file_path", mt / "projects/_shared/.claude/hooks/x.sh")
        self.check(2, mt, "Write", "file_path", self.H / "deploy/x")

    def test_session_outside_any_repo_and_non_write_tools_fail_open(self):
        self.check(0, self.T / "elsewhere", "Write", "file_path", self.C / "backend-repo/x")
        self.check(0, self.H, "Read", "file_path", self.C / "backend-repo/x")

    def test_payload_cwd_does_not_move_the_session_scope(self):
        self.check(0, self.H, "Write", "file_path", self.H / "deploy/x", cwd=self.C / "backend-repo")

    def test_block_message_names_the_owning_repo(self):
        rc, err = run_guard(REPO_SCOPE, ["--root", str(self.C), "--session-dir", str(self.H)],
                            {"cwd": str(self.H), "tool_name": "Write", "tool_input": {"file_path": str(self.C / "backend-repo/llama/x.sh")}})
        self.assertEqual(rc, 2)
        self.assertIn("backend-repo", err)
        self.assertIn("owner of repo", err)

    def test_block_message_from_a_worktree_names_the_main_repo(self):
        rc, err = run_guard(REPO_SCOPE, ["--root", str(self.C), "--session-dir", str(self.T / "wt-hermes")],
                            {"cwd": str(self.T / "wt-hermes"), "tool_name": "Write",
                             "tool_input": {"file_path": str(self.C / "backend-repo/llama/x.sh")}})
        self.assertEqual(rc, 2)
        self.assertIn("belongs to repo 'hermes'", err)
        self.assertIn("repo 'backend-repo'", err)

    def test_missing_root_or_session_dir_fails_open(self):
        rc, _ = run_guard(REPO_SCOPE, [], {"cwd": str(self.H), "tool_name": "Write",
                                            "tool_input": {"file_path": str(self.C / "backend-repo/x")}})
        self.assertEqual(rc, 0)

    def test_garbage_input_fails_open(self):
        proc = subprocess.run([sys.executable, str(REPO_SCOPE), "--root", str(self.C), "--session-dir", str(self.H)],
                              input="not json", capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)


class PublicFirstTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.H = self.T / "hermes"
        self.W = self.T / "hermes-workflows"
        self.git_init(self.H)
        self.git_init(self.W)
        for d in ("skill-router", "diverged", "only-public", "real-dir", "loop"):
            (self.W / "plugins" / d).mkdir(parents=True)
        (self.H / "plugins/skill-router").mkdir(parents=True)
        (self.H / "plugins/diverged").mkdir(parents=True)
        (self.H / "plugins/loop").mkdir(parents=True)
        (self.H / "private").mkdir()
        for d in ("skill-router", "diverged", "only-public", "real-dir"):
            (self.W / "plugins" / d / "plugin.py").write_text("pub\n")
        (self.H / "private/notes.md").write_text("priv\n")
        (self.H / "plugins/diverged/plugin.py").write_text("copy\n")
        os.symlink(self.W / "plugins/skill-router/plugin.py", self.H / "plugins/skill-router/plugin.py")
        os.symlink(self.W / "plugins/real-dir", self.H / "plugins/linked-dir")
        (self.H / "plugins/loop-src").write_text("x\n")
        os.symlink(self.H / "plugins/loop-src", self.W / "plugins/loop/plugin.py")  # public counterpart is a symlink
        (self.H / "plugins/loop/plugin.py").write_text("x\n")

    def check(self, want, tool, key, path):
        args = ["--private-root", str(self.H), "--public-root", str(self.W)]
        rc, _ = run_guard(PUBLIC_FIRST, args, {"tool_name": tool, "tool_input": {key: str(path)}})
        self.assertEqual(rc, want, f"{tool} {path}")

    def test_must_pass(self):
        self.check(0, "Edit", "file_path", self.H / "plugins/skill-router/plugin.py")
        self.check(0, "Write", "file_path", self.H / "plugins/linked-dir/plugin.py")
        self.check(0, "Write", "file_path", self.H / "private/notes.md")
        self.check(0, "Write", "file_path", self.H / "private/new.md")
        self.check(0, "Edit", "file_path", self.W / "plugins/diverged/plugin.py")
        self.check(0, "Write", "file_path", self.T / "backend-repo/plugins/diverged/plugin.py")
        self.check(0, "Edit", "file_path", self.H / "plugins/loop/plugin.py")
        self.check(0, "Read", "file_path", self.H / "plugins/diverged/plugin.py")

    def test_must_block(self):
        self.check(2, "Edit", "file_path", self.H / "plugins/diverged/plugin.py")
        self.check(2, "Write", "file_path", self.H / "plugins/diverged/plugin.py")
        self.check(2, "Write", "file_path", self.H / "plugins/only-public/plugin.py")
        self.check(2, "Edit", "file_path", f"{self.H}/private/../plugins/diverged/plugin.py")
        self.check(2, "NotebookEdit", "notebook_path", self.H / "plugins/diverged/plugin.py")

    def test_block_message_is_exact(self):
        rc, err = run_guard(PUBLIC_FIRST, ["--private-root", str(self.H), "--public-root", str(self.W)],
                            {"tool_name": "Edit", "tool_input": {"file_path": str(self.H / "plugins/diverged/plugin.py")}})
        self.assertEqual(rc, 2)
        self.assertIn("Edit the public file and symlink it into the private repo.", err)

    def test_missing_roots_fail_open(self):
        rc, _ = run_guard(PUBLIC_FIRST, [], {"tool_name": "Edit",
                                              "tool_input": {"file_path": str(self.H / "plugins/diverged/plugin.py")}})
        self.assertEqual(rc, 0)


class _SessionFixture(_Fixture):
    def setUp(self):
        super().setUp()
        self.cfg = config.DEFAULTS.copy()
        self.cfg.update(enabled=True, claude_bin="/bin/echo", home=str(self.T / "ccb"),
                        allowed_channels=["chan1"], allowed_users=["ana"])
        self.repo = self.T / "Code" / "hermes"
        self.git_init(self.repo)
        self.C = self.T / "Code"

    def settings(self, cid="chan1"):
        return json.loads((session.prepare_dir(self.cfg, cid) / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def write_hooks(self, settings):
        return [h for h in settings.get("hooks", {}).get("PreToolUse", []) if h["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"]


class WriteGuardInstallTests(_SessionFixture):
    def test_off_by_default_installs_nothing(self):
        self.cfg["channels"] = {"chan1": {"cwd": str(self.repo)}}
        d = session.prepare_dir(self.cfg, "chan1")
        self.assertEqual(self.write_hooks(self.settings()), [])
        self.assertFalse((d / "repo_scope_guard.py").exists())
        self.assertFalse((d / "public_first_guard.py").exists())

    def test_repo_scope_installed_for_a_repo_channel_with_root(self):
        self.cfg.update(repo_scope_guard=True, repo_scope_root=str(self.C), repo_scope_companions=[str(self.T / "wf")])
        self.cfg["channels"] = {"chan1": {"cwd": str(self.repo)}}
        d = session.prepare_dir(self.cfg, "chan1")
        hooks = self.write_hooks(self.settings())
        self.assertEqual(len(hooks), 1)
        command = hooks[0]["hooks"][0]["command"]
        self.assertIn(str(d / "repo_scope_guard.py"), command)
        self.assertIn(f"--root {self.C}", command)
        self.assertIn(f"--session-dir {self.repo}", command)
        self.assertIn(f"--companion {self.T / 'wf'}", command)
        self.assertTrue((d / "repo_scope_guard.py").exists())

    def test_repo_scope_fails_open_without_cwd_or_root(self):
        self.cfg.update(repo_scope_guard=True, repo_scope_root=str(self.C))
        self.assertEqual(self.write_hooks(self.settings()), [])  # no cwd: spool channel
        self.cfg.update(repo_scope_root="")
        self.cfg["channels"] = {"chan1": {"cwd": str(self.repo)}}
        self.assertEqual(self.write_hooks(self.settings()), [])

    def test_public_first_installed_only_with_both_roots(self):
        self.cfg["channels"] = {"chan1": {"cwd": str(self.repo)}}
        self.cfg.update(public_first_guard=True, public_first={"private_root": str(self.repo)})
        self.assertEqual(self.write_hooks(self.settings()), [])
        self.cfg["public_first"] = {"private_root": str(self.repo), "public_root": str(self.T / "pub")}
        hooks = self.write_hooks(self.settings())
        self.assertEqual(len(hooks), 1)
        self.assertIn("--public-root", hooks[0]["hooks"][0]["command"])

    def test_write_guards_do_not_replace_the_browser_or_ask_hooks(self):
        self.cfg["channels"] = {"chan1": {"cwd": str(self.repo)}}
        self.cfg.update(repo_scope_guard=True, repo_scope_root=str(self.C), deny_ask_user_question=True,
                        browser_guard={"tools": "mcp__browser__.*", "min_interval_s": 1})
        pre = self.settings()["hooks"]["PreToolUse"]
        matchers = [h["matcher"] for h in pre]
        self.assertIn("mcp__browser__.*", matchers)
        self.assertIn("Write|Edit|MultiEdit|NotebookEdit", matchers)
        self.assertIn("AskUserQuestion", matchers)

    def test_write_guard_config_changes_the_fingerprint(self):
        self.cfg["channels"] = {"chan1": {"cwd": str(self.repo)}}
        base = session.fingerprint(self.cfg, "chan1")
        self.cfg.update(repo_scope_guard=True, repo_scope_root=str(self.C))
        self.assertNotEqual(base, session.fingerprint(self.cfg, "chan1"))

    def test_channel_override_adds_companions_and_turns_guard_on(self):
        self.cfg.update(repo_scope_guard=False, repo_scope_root=str(self.C), repo_scope_companions=["/g"])
        self.cfg["channels"] = {"chan1": {"repo_scope_guard": True, "repo_scope_companions": ["/h"]}}
        eff = config.for_channel(self.cfg, "chan1")
        self.assertTrue(eff["repo_scope_guard"])
        self.assertEqual(eff["repo_scope_companions"], ["/g", "/h"])
        self.assertFalse(config.for_channel(self.cfg, "other")["repo_scope_guard"])


class ScriptFingerprintTests(_Fixture):
    """A change to a copied script's body changes the launch fingerprint (the session relaunches with --resume)."""

    def setUp(self):
        super().setUp()
        self.plugin = self.T / "plugin"
        self.plugin.mkdir()
        for name in session.COPIED_SCRIPTS:
            (self.plugin / name).write_bytes((PLUGIN / name).read_bytes())
        patch = mock.patch.object(session, "PLUGIN_ROOT", self.plugin)
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = config.DEFAULTS.copy()
        self.cfg.update(enabled=True, claude_bin="/bin/echo", home=str(self.T / "ccb"))

    def test_changing_a_script_body_changes_the_fingerprint(self):
        before = session.fingerprint(self.cfg, "chan1")
        (self.plugin / "stop_hook.py").write_bytes((self.plugin / "stop_hook.py").read_bytes() + b"\n# changed\n")
        self.assertNotEqual(before, session.fingerprint(self.cfg, "chan1"))

    def test_every_copied_script_is_covered(self):
        digests = session.script_sha256()
        self.assertEqual(set(digests), set(session.COPIED_SCRIPTS))
        self.assertTrue(all(digests[name] for name in session.COPIED_SCRIPTS))

    def test_unchanged_scripts_keep_the_fingerprint(self):
        self.assertEqual(session.fingerprint(self.cfg, "chan1"), session.fingerprint(self.cfg, "chan1"))


class PreToolUseOrderingTests(_Fixture):
    """The browser guard, the write guards and the AskUserQuestion denial all live in PreToolUse; none may wipe another."""

    def test_browser_guard_installed_last_keeps_write_guards_and_ask_deny(self):
        repo = self.T / "Code" / "hermes"
        self.git_init(repo)
        d = self.T / "spool"
        d.mkdir()
        settings = {}
        session._install_write_guards(
            d, settings,
            {"repo_scope_guard": True, "repo_scope_root": str(self.T / "Code")},
            repo,
        )
        session._install_ask_deny(settings, {"deny_ask_user_question": True})
        session._install_browser_guard(d, settings, {"browser_guard": {"min_interval_s": 1}})
        matchers = [h["matcher"] for h in settings["hooks"]["PreToolUse"]]
        self.assertIn("Write|Edit|MultiEdit|NotebookEdit", matchers)
        self.assertIn("AskUserQuestion", matchers)
        self.assertIn("mcp__browser__.*", matchers)
        self.assertEqual(len(matchers), 3)


if __name__ == "__main__":
    unittest.main()
