#!/usr/bin/env python3
"""End-to-end tests for plugins/kanban-harness against the REAL hermes-agent kanban_db.

Runs in a scratch HERMES_HOME (hard-refuses the real ~/.hermes). Registers the plugin
through the real PluginContext, then:

  * attempts every forbidden transition / side door and expects a refusal that comes
    back through the real pre_tool_call pipeline, with the task unchanged;
  * drives the allowed path end to end with the shipped example matrix:
      dispatcher spawns coding -> coding hands off -> dispatcher spawns QA ->
      QA hands off -> task parked in the human lane (nothing spawned) ->
      the human completes it via the real CLI; plus the human's rework path;
  * for each configuration option (per-board matrices, sub-task gate, shell verbs,
    human-lane status, the table being the only source of permissions)
    checks one refused and one allowed case; and that `mode:` / `enabled:` in a
    config fail it closed rather than reviving the removed warn/disable switches.

Needs the hermes-agent source (HERMES_AGENT_SRC, default ~/.hermes/hermes-agent) and a
python that can import it (run with that install's venv). Exit 77 = prerequisites
missing (the wrapper reports SKIP).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "kanban-harness"
AGENT_SRC = Path(os.path.expanduser(
    os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent"))).resolve()

# --- isolation: scratch HERMES_HOME, set BEFORE any hermes import -------------------
_HOME = Path(tempfile.mkdtemp(prefix="kanban-harness-test-")).resolve()
_REAL = Path(os.path.expanduser("~/.hermes")).resolve()
if _HOME == _REAL or _REAL in _HOME.parents:
    sys.exit("refusing: scratch HERMES_HOME resolves inside the real ~/.hermes")
for k in [k for k in os.environ if k.startswith("HERMES_KANBAN")] + ["HERMES_PROFILE"]:
    os.environ.pop(k, None)
os.environ["HERMES_HOME"] = str(_HOME)
# The harness refuses to load unless the dispatcher cannot split cards (T43).
(_HOME / "config.yaml").write_text("kanban:\n  auto_decompose: false\n")
for prof in ("coding", "qa-tester"):
    (_HOME / "profiles" / prof).mkdir(parents=True)

EXAMPLE = (PLUGIN_DIR / "harness.yaml.example").read_text()
HARNESS = _HOME / "harness.yaml"
HARNESS.write_text(EXAMPLE.replace("assignee: user", "assignee: alice"))
os.environ["KANBAN_HARNESS_FILE"] = str(HARNESS)

if not (AGENT_SRC / "hermes_cli" / "kanban_db.py").is_file():
    print(f"SKIP: hermes-agent source not found at {AGENT_SRC}")
    sys.exit(77)
sys.path.insert(0, str(AGENT_SRC))
try:
    from hermes_cli import kanban_db as kb
    from hermes_cli.plugins import (PluginContext, PluginManifest, get_plugin_manager,
                                    get_pre_tool_call_block_message)
    import model_tools
except Exception as exc:  # wrong interpreter / missing deps
    print(f"SKIP: cannot import hermes-agent ({exc})")
    sys.exit(77)

_spec = importlib.util.spec_from_file_location("kanban_harness", PLUGIN_DIR / "__init__.py")
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)
harness.register(PluginContext(PluginManifest(name="kanban-harness"), get_plugin_manager()))


# Re-pinned per module: under one pytest process every test file is imported
# before any test runs, so the import-time settings above are overwritten by
# whichever file came last, and two suites would share one board.
# HOME too: the harness's default log is ~/.hermes/logs/kanban-harness.log, and a
# test run must never write refusals into the real one.
(_HOME / "home").mkdir(exist_ok=True)
_ENV = {"HERMES_HOME": str(_HOME), "KANBAN_HARNESS_FILE": str(HARNESS),
        "KANBAN_BOARD_FILE": None, "HOME": str(_HOME / "home")}
_SAVED = {}


def setUpModule():
    for key, value in _ENV.items():
        _SAVED[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def tearDownModule():
    for key, value in _SAVED.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def blocked(tool, args):
    return get_pre_tool_call_block_message(tool, args)


def call_tool(tool, args):
    return json.loads(model_tools.handle_function_call(tool, args))


# A compact matrix used by the option tests; each test appends its own rows/keys.
_BASE = """
roles:
  coding: {profiles: [coding]}
  qa: {profiles: [qa-tester]}
  user: {human: true, assignee: alice}
always_allow:
  tools: [kanban_show, kanban_list, kanban_heartbeat, kanban_comment]
  shell_verbs: [list, show]
side_doors:
  deny_patterns: [{pattern: 'kanban\\.db', label: the kanban database file}]
"""


class Config:
    """Temporarily point the harness at another config (a fresh path busts the cache)."""
    n = 0

    def __init__(self, text):
        type(self).n += 1
        self.path = _HOME / f"harness-{self.n}.yaml"
        self.path.write_text(textwrap.dedent(text))

    def __enter__(self):
        self.old = os.environ["KANBAN_HARNESS_FILE"]
        os.environ["KANBAN_HARNESS_FILE"] = str(self.path)
        return self

    def __exit__(self, *a):
        os.environ["KANBAN_HARNESS_FILE"] = self.old


class Worker:
    """Set the env a dispatcher-spawned worker for `task` runs with."""

    def __init__(self, task_id, profile):
        self.env = {"HERMES_KANBAN_TASK": task_id, "HERMES_PROFILE": profile}

    def __enter__(self):
        os.environ.update(self.env)
        return self

    def __exit__(self, *a):
        for k in self.env:
            os.environ.pop(k, None)


class HarnessCase(unittest.TestCase):
    spawned = []

    @classmethod
    def spawn(cls, task, workspace, board=None):
        cls.spawned.append((task.id, task.assignee))
        return None

    def setUp(self):
        self.conn = kb.connect()
        type(self).spawned = []

    def tearDown(self):
        self.conn.close()

    def reopen(self):  # fresh snapshot after a write from another process
        self.conn.close()
        self.conn = kb.connect()

    def tick(self):
        return kb.dispatch_once(self.conn, spawn_fn=self.spawn)

    def task(self, tid):
        return kb.get_task(self.conn, tid)

    def new_running_task(self, profile="coding", title="t"):
        tid = kb.create_task(self.conn, title=title, assignee=profile)
        self.tick()
        self.assertEqual(self.task(tid).status, "running")
        return tid

    def human_cli(self, *argv):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HERMES_KANBAN") and k != "HERMES_PROFILE"}
        out = subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", *argv],
                             cwd=str(AGENT_SRC), env=env, capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.reopen()
        return out

    def human_cli_may_fail(self, *argv):
        """The runtime CLI, without asserting success -- for moves that must fail."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HERMES_KANBAN") and k != "HERMES_PROFILE"}
        out = subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", *argv],
                             cwd=str(AGENT_SRC), env=env, capture_output=True, text=True,
                             timeout=120)
        self.reopen()
        return out

    def user_cli(self, *argv):
        """The user's own accept/rework command, run as a real process the way a
        person at a terminal runs it: no worker environment."""
        # A person: no Hermes variable at all, the board named explicitly, and a
        # real terminal on stdin -- board_cli refuses anything less.
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
        cmd = [sys.executable, str(REPO / "scripts" / "resilience" / "board_cli.py"),
               "--db", str(kb.kanban_db_path())]
        master, slave = os.openpty()
        try:
            for step in (["init-ledger"], list(argv)):
                out = subprocess.run(cmd + step, stdin=slave, env=env,
                                     capture_output=True, text=True, timeout=60)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        finally:
            os.close(slave)
            os.close(master)
        self.reopen()
        return out


class ForbiddenTransitions(HarnessCase):
    def test_coding_cannot_complete_block_or_unblock(self):
        tid = self.new_running_task("coding")
        with Worker(tid, "coding"):
            for tool in ("kanban_complete", "kanban_block", "kanban_unblock"):
                msg = blocked(tool, {"task_id": tid, "summary": "done", "reason": "x"})
                self.assertIsNotNone(msg, tool)
                self.assertIn("kanban_handoff", msg)
            # through the real dispatch path: a readable tool error, task unchanged
            res = call_tool("kanban_complete", {"task_id": tid, "summary": "all good"})
            self.assertIn("error", res)
            self.assertIn("kanban-harness", res["error"])
        self.assertEqual(self.task(tid).status, "running")

    def test_qa_cannot_complete_and_sends_back_only_through_rework(self):
        """QA may not finish a card. Sending it back is the signed-off rework
        row (in_progress -> to do, by qa), made with to_role: coding -- and it
        needs the reason in the summary."""
        tid = self.new_running_task("qa-tester")
        with Worker(tid, "qa-tester"):
            for tool in ("kanban_complete", "kanban_block", "kanban_unblock"):
                self.assertIsNotNone(blocked(tool, {"task_id": tid}), tool)
            self.assertIn("error", call_tool("kanban_complete", {"task_id": tid, "summary": "ok"}))
            self.assertIn("error", call_tool("kanban_handoff", {"summary": " ", "to_role": "coding"}))
        self.assertEqual(self.task(tid).status, "running")
        with Worker(tid, "qa-tester"):
            res = call_tool("kanban_handoff", {"summary": "row 7 wrong: expected 12",
                                               "to_role": "coding"})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual((self.task(tid).status, self.task(tid).assignee), ("ready", "coding"))

    def test_unknown_profile_cannot_mutate(self):
        with Worker("t_nothing", "orchestrator"):
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x"}))
            self.assertIsNotNone(blocked("kanban_create", {"title": "x"}))
            self.assertIsNone(blocked("kanban_show", {"task_id": "t_x"}))

    def test_handoff_only_own_running_task(self):
        tid = self.new_running_task("coding")
        other = kb.create_task(self.conn, title="other", assignee="coding")
        with Worker(tid, "coding"):
            self.assertIn("error", call_tool("kanban_handoff", {"task_id": other, "summary": "x"}))
            self.assertIn("error", call_tool("kanban_handoff", {"summary": "  "}))
        with Worker(tid, "qa-tester"):  # not the assignee
            self.assertIn("error", call_tool("kanban_handoff", {"summary": "x"}))
        self.assertEqual(self.task(tid).status, "running")

    def test_side_doors_refused(self):
        tid = self.new_running_task("coding")
        db = str(kb.kanban_db_path())
        doors = [
            ("terminal", {"command": f"hermes kanban complete {tid} --summary ok"}),
            ("terminal", {"command": f"hermes -p coding kanban --board default block {tid} x"}),
            ("terminal", {"command": f"cd /tmp && hermes kanban unblock {tid}"}),
            ("terminal", {"command": f"echo; hermes kanban promote {tid}"}),
            ("terminal", {"command": f"hermes kanban assign {tid} alice"}),
            ("terminal", {"command": f"hermes kanban reassign {tid} qa-tester"}),
            ("terminal", {"command": f"hermes kanban archive {tid}"}),
            ("terminal", {"command": "hermes kanban create 'x'"}),  # tool-only grant
            ("terminal", {"command": "hermes kanban boards switch default"}),
            ("terminal", {"command": f"sqlite3 {db} \"update tasks set status='done'\""}),
            ("terminal", {"command": "python3 -c 'from hermes_cli import kanban_db'"}),
            ("terminal", {"command": "curl -X POST localhost:9119/api/plugins/kanban/tasks/x/reassign"}),
            ("execute_code", {"code": f"import sqlite3; sqlite3.connect('{db}')"}),
            ("write_file", {"path": db, "content": ""}),
            ("patch", {"path": db + "-wal", "old_string": "a", "new_string": "b"}),
        ]
        with Worker(tid, "coding"):
            for tool, args in doors:
                self.assertIsNotNone(blocked(tool, args), f"{tool} {args}")
            for cmd in ("hermes kanban list", f"hermes kanban show {tid}",
                        "hermes kanban boards list", "pytest -q", "git status"):
                self.assertIsNone(blocked("terminal", {"command": cmd}), cmd)
        self.assertEqual(self.task(tid).status, "running")

    def test_config_unreadable_fails_closed(self):
        old = os.environ["KANBAN_HARNESS_FILE"]
        os.environ["KANBAN_HARNESS_FILE"] = str(_HOME / "missing.yaml")
        try:
            with Worker("t_x", "coding"):
                self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x"}))
                self.assertIsNone(blocked("terminal", {"command": "ls"}))
        finally:
            os.environ["KANBAN_HARNESS_FILE"] = old


class AllowedPath(HarnessCase):
    def test_allowed_path_end_to_end(self):
        tid = kb.create_task(self.conn, title="feature", assignee="coding")
        self.tick()  # dispatcher: ready -> running, spawns the coding
        self.assertIn((tid, "coding"), self.spawned)

        with Worker(tid, "coding"):
            res = call_tool("kanban_handoff", {"summary": "implemented X; tests pass"})
        self.assertTrue(res.get("ok"), res)
        t = self.task(tid)
        self.assertEqual((t.status, t.assignee), ("ready", "qa-tester"))

        type(self).spawned = []
        self.tick()  # dispatcher spawns QA
        self.assertIn((tid, "qa-tester"), self.spawned)
        self.assertEqual(self.task(tid).status, "running")

        with Worker(tid, "qa-tester"):
            res = call_tool("kanban_handoff", {"summary": "re-ran; 10/10 rows verified"})
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res["human"])
        t = self.task(tid)
        # user_review: finished work waiting for the person
        self.assertEqual((t.status, t.assignee), ("scheduled", "alice"))

        type(self).spawned = []
        self.tick()  # user_review: not promoted, never spawned
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.task(tid).status, "scheduled")

        # The runtime's own `complete` is NOT a door out of user_review...
        refused = self.human_cli_may_fail("complete", tid, "--summary", "shortcut")
        self.assertEqual(self.task(tid).status, "scheduled",
                         "the runtime's complete reached done: accept is no longer the only door\n"
                         + refused.stdout + refused.stderr)
        # ...the user's accept is.
        self.user_cli("accept", tid, "--comment", "accepted by the user")
        self.assertEqual(self.task(tid).status, "done")
        kinds = [e.kind for e in kb.list_events(self.conn, tid)]
        self.assertEqual(kinds.count("handed_off"), 2, kinds)

    def test_human_rework_path(self):
        tid = self.new_running_task("coding")
        with Worker(tid, "coding"):
            self.assertTrue(call_tool("kanban_handoff", {"summary": "v1"}).get("ok"))
        self.tick()
        with Worker(tid, "qa-tester"):
            self.assertTrue(call_tool("kanban_handoff", {"summary": "4 bad rows"}).get("ok"))
        # the user sends it back, with the reason, to whoever did the work
        self.user_cli("rework", tid, "--comment", "rows 3,5,7,9 wrong; redo")
        self.assertEqual((self.task(tid).status, self.task(tid).assignee), ("ready", "coding"))
        type(self).spawned = []
        self.tick()
        self.assertIn((tid, "coding"), self.spawned)
        self.assertEqual(self.task(tid).status, "running")

    def test_an_agent_cannot_reach_the_users_accept_command(self):
        """accept is the only door to done; an agent at it would be closing
        its own work. Refused as a side door in any agent shell."""
        with Worker("t_x", "qa-tester"):
            for cmd in ("python3 scripts/resilience/board_cli.py accept t_x",
                        "python3 -m board_cli accept t_x",
                        "cd scripts/resilience && ./board_cli.py rework t_x --comment x"):
                self.assertIsNotNone(blocked("terminal", {"command": cmd}), cmd)


class Options(HarnessCase):
    def test_per_board_matrices(self):
        """A board may override its table -- shown with a harmless action. No
        board may grant `complete` (the baseline refuses that at load), and a
        board that is not listed still gets the default table."""
        cfg = _BASE + """
        transitions:
          - {from_role: coding, action: handoff, to_role: qa, status: ready}
        boards:
          "*": {}
          scratch:
            transitions:
              - {from_role: coding, action: link}
        """
        with Config(textwrap.dedent(_BASE) + textwrap.dedent(cfg[len(_BASE):])), \
                Worker("t_x", "coding"):
            self.assertIsNotNone(blocked("kanban_link", {"task_id": "t_x", "board": "default"}))
            self.assertIsNone(blocked("kanban_link", {"task_id": "t_x", "board": "scratch"}))
        # every board is harnessed: an unlisted board gets the default table
        cfg2 = textwrap.dedent(_BASE) + "transitions: []\nboards: [only-this]\n"
        with Config(cfg2), Worker("t_x", "coding"):
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x", "board": "only-this"}))
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x", "board": "default"}))

    def test_when_non_root_gates_a_row_by_card_level(self):
        """The `when` mechanism, shown with a harmless action. (It used to be
        shown with QA completing child cards -- the "sub-task gate" -- which is
        no longer a shipped option: no agent reaches done, ever.)"""
        parent = kb.create_task(self.conn, title="batch", assignee="coding")
        child = kb.create_task(self.conn, title="row 1", assignee="qa-tester", parents=[parent])
        marked = kb.create_task(self.conn, title="row 2", assignee="qa-tester",
                                parents=[parent], body="Level: 0\ncheck it")
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: qa, action: link, when: non_root}
        """)
        with Config(cfg):
            for tid, expect_refused in ((child, False), (parent, True), (marked, True)):
                with Worker(tid, "qa-tester"):
                    msg = blocked("kanban_link", {"task_id": tid})
                    self.assertEqual(msg is not None, expect_refused, (tid, msg))
            with Worker(child, "coding"):  # the row is QA's, not coding's
                self.assertIsNotNone(blocked("kanban_link", {"task_id": child}))

    def test_the_shipped_template_grants_no_agent_complete(self):
        """No agent reaches done, ever -- not even for a child card. Pinned so
        a later edit to the template cannot quietly bring the sub-task gate
        back."""
        rows = [t for m in [harness.load_config()["default"]] for t in m["transitions"]]
        self.assertEqual([t for t in rows if t["action"] == "complete"], [])

    def test_per_role_shell_verbs(self):
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: coding, action: create, via: [tool, shell]}
          - {from_role: coding, action: link, via: shell}
        """)
        with Config(cfg):
            with Worker("t_x", "coding"):
                self.assertIsNone(blocked("terminal", {"command": "hermes kanban create 'row 7'"}))
                self.assertIsNone(blocked("terminal", {"command": "hermes kanban link t_a t_b"}))
                self.assertIsNone(blocked("terminal", {"command": "hermes kanban list"}))
                self.assertIsNotNone(blocked("terminal", {"command": "hermes kanban complete t_x"}))
                self.assertIsNotNone(blocked("kanban_link", {"parent_id": "t_a"}))  # shell-only row
            with Worker("t_x", "qa-tester"):
                self.assertIsNotNone(blocked("terminal", {"command": "hermes kanban create 'x'"}))

    def test_mode_key_fails_to_load(self):
        """warn (log-only) mode was removed: a config that still names `mode:`
        must fail to load, and kanban calls stay refused while it does (the
        existing fail-closed-on-config-error behavior), never silently allowed."""
        cfg = textwrap.dedent(_BASE) + "mode: warn\ntransitions: []\n"
        with Config(cfg), Worker("t_x", "coding"):
            with self.assertRaises(harness.HarnessConfigError):
                harness.load_config()
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x"}))

    def test_enabled_key_fails_to_load(self):
        """There is no disable switch: a config naming `enabled:` must fail to
        load, and kanban calls stay refused while it does."""
        cfg = textwrap.dedent(_BASE) + "enabled: false\ntransitions: []\n"
        with Config(cfg), Worker("t_x", "coding"):
            with self.assertRaises(harness.HarnessConfigError):
                harness.load_config()
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x"}))

    def test_default_log_path_ignores_hermes_home(self):
        """The default log lives in the shared ~/.hermes, never under
        $HERMES_HOME (a worker's HERMES_HOME is its own profile dir)."""
        fake_home = Path(tempfile.mkdtemp(dir=_HOME))
        old_home_env = os.environ.get("HOME")
        old_hermes_home = os.environ.get("HERMES_HOME")
        os.environ["HOME"] = str(fake_home)
        os.environ["HERMES_HOME"] = str(_HOME / "profiles" / "coding")
        try:
            path = harness._log_path(None)
        finally:
            if old_home_env is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home_env
            if old_hermes_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_hermes_home
        self.assertEqual(path, fake_home / ".hermes" / "logs" / "kanban-harness.log")

    def test_human_lane_status_ready(self):
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: qa, action: handoff, to_role: user, status: ready}
        """)
        tid = self.new_running_task("qa-tester")
        with Config(cfg), Worker(tid, "qa-tester"):
            self.assertTrue(call_tool("kanban_handoff", {"summary": "ok"}).get("ok"))
        self.assertEqual(self.task(tid).status, "ready")
        type(self).spawned = []
        result = self.tick()
        self.assertEqual(self.spawned, [])
        self.assertIn(tid, result.skipped_nonspawnable)

    def test_table_is_the_only_source(self):
        # nothing granted, nothing always-allowed: even reads are refused
        cfg = "roles:\n  coding: {profiles: [coding]}\ntransitions: []\n"
        with Config(cfg), Worker("t_x", "coding"):
            self.assertIsNotNone(blocked("kanban_show", {"task_id": "t_x"}))
            self.assertIsNotNone(blocked("kanban_create", {"title": "x"}))
        # a single profile named as its own role, granted one harmless move by one row
        cfg = "roles:\n  coding: {}\ntransitions:\n  - {from_role: coding, action: link}\n"
        with Config(cfg), Worker("t_x", "coding"):
            self.assertIsNone(blocked("kanban_link", {"task_id": "t_x"}))
            self.assertIsNotNone(blocked("kanban_block", {"task_id": "t_x"}))
        # and no row can grant complete: the baseline refuses the table at load
        cfg = "roles:\n  coding: {}\ntransitions:\n  - {from_role: coding, action: complete}\n"
        with Config(cfg), Worker("t_x", "coding"):
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x"}))

    def test_handoff_requires_evidence(self):
        ws = Path(tempfile.mkdtemp(dir=_HOME))
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: coding, action: handoff, to_role: qa, status: ready,
             requires: {files: "findings/**/*.md", matches: '(?m)^Verified: yes$'}}
        """)
        tid = self.new_running_task("coding")
        os.environ["HERMES_KANBAN_WORKSPACE"] = str(ws)
        try:
            with Config(cfg), Worker(tid, "coding"):
                res = call_tool("kanban_handoff", {"summary": "done"})  # no file at all
                self.assertIn("requires evidence", res.get("error", ""), res)
                (ws / "findings").mkdir()
                (ws / "findings" / "b02.md").write_text("rows compared\nVerified: no\n")
                res = call_tool("kanban_handoff", {"summary": "done"})  # file, no match
                self.assertIn("contains", res.get("error", ""), res)
                self.assertEqual(self.task(tid).status, "running")
                (ws / "findings" / "b02.md").write_text("rows compared\nVerified: yes\n")
                self.assertTrue(call_tool("kanban_handoff", {"summary": "done"}).get("ok"))
        finally:
            os.environ.pop("HERMES_KANBAN_WORKSPACE", None)
        self.assertEqual(self.task(tid).status, "ready")

    def test_ready_handoff_records_promotion(self):
        tid = self.new_running_task("coding")
        with Worker(tid, "coding"):
            self.assertTrue(call_tool("kanban_handoff", {"summary": "v1"}).get("ok"))
        kinds = [e.kind for e in kb.list_events(self.conn, tid)]
        self.assertGreater(kinds.index("promoted", kinds.index("handed_off")),
                           kinds.index("handed_off"), kinds)

    def test_bad_config_rejected(self):
        cfg = textwrap.dedent(_BASE) + "transitions:\n  - {from_role: qa, action: handoff, to_role: nobody, status: ready}\n"
        with Config(cfg), Worker("t_x", "qa-tester"):
            self.assertIn("config unreadable", blocked("kanban_complete", {"task_id": "t_x"}))


BOARD_YAML = REPO / "config" / "board.yaml"
#: A summary that meets the shipped board's hand-off form (board.yaml `handoff:`).
_FORM_OK = ("Done: the change\nMethod: the tests\nFiles: none\nResult: 10/10 rows verified\n"
            "Could not check: none\nReview first: row 7\nDoubts: none")


class BoardFile:
    """Deploy a board specification for the duration of a block."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.old = os.environ.get("KANBAN_BOARD_FILE")
        os.environ["KANBAN_BOARD_FILE"] = str(self.path)
        return self

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop("KANBAN_BOARD_FILE", None)
        else:
            os.environ["KANBAN_BOARD_FILE"] = self.old


class _NoBoard:
    """No board specification deployed, for the duration of a block."""

    def __enter__(self):
        self.old = os.environ.pop("KANBAN_BOARD_FILE", None)
        return self

    def __exit__(self, *a):
        if self.old is not None:
            os.environ["KANBAN_BOARD_FILE"] = self.old


class BoardComposition(unittest.TestCase):
    """board.yaml says what moves exist; harness.yaml says who may make them.
    Composed at load, and the two must agree."""

    def load(self, harness_text=None):
        if harness_text is None:
            return harness.load_config()
        with Config(harness_text):
            return harness.load_config()

    def test_no_board_deployed_means_no_composition(self):
        """Opt-in: a board file in a checkout must never change the rules."""
        os.environ.pop("KANBAN_BOARD_FILE", None)
        self.assertIsNone(self.load()["board"])

    def test_the_shipped_harness_agrees_with_the_real_board(self):
        with BoardFile(BOARD_YAML):
            cfg = self.load()
        self.assertIsNotNone(cfg["board"])
        self.assertEqual(cfg["board"].status_of("to_do"), "ready")
        self.assertEqual(cfg["board"].status_of("user_review"), "scheduled")

    def test_the_sub_task_gate_cannot_be_configured(self):
        """The old sub-task gate (qa completes child cards) is refused at load,
        with or without a board -- by the baseline itself now, before the board
        composition even gets to disagree."""
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: qa, action: complete, when: non_root}
        """)
        for board in (BOARD_YAML, None):
            with self.subTest(board=board):
                ctx_board = BoardFile(BOARD_YAML) if board else _NoBoard()
                with ctx_board, self.assertRaises(harness.HarnessConfigError) as ctx:
                    self.load(cfg)
                self.assertIn("action 'complete' lands a card in done", str(ctx.exception))

    def test_coding_may_not_complete_its_own_card(self):
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: coding, action: complete}
        """)
        with BoardFile(BOARD_YAML), self.assertRaises(harness.HarnessConfigError) as ctx:
            self.load(cfg)
        self.assertIn("action 'complete' lands a card in done", str(ctx.exception))

    def test_a_hand_off_into_a_status_no_column_uses(self):
        """On a board with no column on `blocked`, a hand-off that parks a
        card there makes it vanish from every column a person looks at."""
        no_question = _HOME / "board-without-question.yaml"
        no_question.write_text(textwrap.dedent("""
            board:
              columns:
                todo: {exit_to: [in_progress]}
                in_progress: {exit_to: [review]}
                review: {exit_to: [done]}
                done: {}
        """) + _ROLES)
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: qa, action: handoff, to_role: user, status: blocked}
        """)
        with BoardFile(no_question), self.assertRaises(harness.HarnessConfigError) as ctx:
            self.load(cfg)
        self.assertIn("'blocked'", str(ctx.exception))
        self.assertIn("no column", str(ctx.exception))

    def test_a_hand_off_never_finishes_a_card(self):
        """The guard, widened for D9 to `scheduled` (user_review) and no
        further: never done, never archived, never the runtime's agent `review`
        lane."""
        for status in ("done", "archived", "review", "running"):
            cfg = textwrap.dedent(_BASE) + textwrap.dedent(f"""
            transitions:
              - {{from_role: qa, action: handoff, to_role: user, status: {status}}}
            """)
            with self.subTest(status=status), self.assertRaises(harness.HarnessConfigError):
                self.load(cfg)

    def test_user_review_is_for_a_person_not_an_agent_role(self):
        """Parking a card in `scheduled` for an agent role leaves it where
        nothing is ever spawned for it."""
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: coding, action: handoff, to_role: qa, status: scheduled}
        """)
        with self.assertRaises(harness.HarnessConfigError) as ctx:
            self.load(cfg)
        self.assertIn("PERSON", str(ctx.exception))

    def test_a_human_lane_must_be_human_in_both_files(self):
        """A human lane with a profile behind it gets a worker spawned for it."""
        cfg = textwrap.dedent("""
        roles:
          coding: {profiles: [coding]}
          user: {profiles: [planner]}
        transitions: []
        """)
        with BoardFile(BOARD_YAML), self.assertRaises(harness.HarnessConfigError) as ctx:
            self.load(cfg)
        self.assertIn("'user' is human=True", str(ctx.exception))

    def test_disagreeing_files_fail_closed_on_a_kanban_move(self):
        """Not just a load error: the running harness refuses kanban mutations
        while the two files disagree, same as any other broken config."""
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: qa, action: complete, when: non_root}
        """)
        with BoardFile(BOARD_YAML), Config(cfg), Worker("t_x", "qa-tester"):
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x"}))

    def test_a_structurally_broken_board_refuses_to_load(self):
        broken = _HOME / "broken-board.yaml"
        broken.write_text("board:\n  columns:\n    todo: {exit_to: [nowhere]}\n")
        with BoardFile(broken), self.assertRaises(harness.HarnessConfigError) as ctx:
            self.load()
        self.assertIn("unknown-edge", str(ctx.exception))

    def test_spec_warnings_do_not_take_the_harness_down(self):
        """A contradiction in the spec is the spec owner's to decide. Until
        they do, the board keeps working -- a spec question never stops a
        running board. (The shipped spec has none left; this one is made up.)"""
        warned = _HOME / "board-with-a-warning.yaml"
        warned.write_text(textwrap.dedent("""
            board:
              columns:
                in_progress: {exit_to: [todo, question, user_review]}
                todo: {status: ready, exit_to: [in_progress]}
                waiting: {status: todo}
                question: {}
                user_review: {status: scheduled}
                question: {status: blocked}
        """) + _ROLES)
        with BoardFile(warned):
            cfg = self.load()
        self.assertEqual([i.code for i in cfg["board"].warnings], ["D3"])
        self.assertEqual(cfg["board"].errors, [])


# ---------------------------------------------------------------------------
# config/board.yaml at transition time, on real hand-offs
# ---------------------------------------------------------------------------

#: The roles a scratch board declares: the harness refuses to grant a move to
#: a role its board does not declare, or a hand-off its board does not (T44).
_ROLES = """
roles:
  coding: {hands_off_to: [qa, user]}
  qa: {hands_off_to: [user, coding]}
  planner: {hands_off_to: [coding, qa, user]}
  user: {human: true}
"""


def _board(tmpname, text):
    path = _HOME / tmpname
    text = textwrap.dedent(text)
    path.write_text(text if "\nroles:" in text else text + _ROLES)
    return path


class BoardAtTransitionTime(HarnessCase):
    """A move is legal only when BOTH files agree: the harness grants the role
    the action, and the board has the edge and its conditions hold."""

    def test_the_shipped_flow_works_with_the_real_board_deployed(self):
        tid = self.new_running_task("coding")
        with BoardFile(BOARD_YAML):
            with Worker(tid, "coding"):
                self.assertTrue(call_tool("kanban_handoff", {"summary": _FORM_OK}).get("ok"))
            self.tick()
            with Worker(tid, "qa-tester"):
                res = call_tool("kanban_handoff", {"summary": _FORM_OK})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual((self.task(tid).status, self.task(tid).assignee), ("scheduled", "alice"))

    def test_a_move_that_is_not_an_edge_is_refused(self):
        no_edge = _board("board-no-review-edge.yaml", """
            board:
              columns:
                to_do:       {status: ready, exit_to: [in_progress]}
                in_progress: {status: running, exit_to: [to_do]}
                user_review: {status: scheduled, exit_to: [done]}
                question: {status: blocked}
                done: {}
        """)
        tid = self.new_running_task("qa-tester")
        with BoardFile(no_edge), Worker(tid, "qa-tester"):
            res = call_tool("kanban_handoff", {"summary": "verified"})
        self.assertIn("error", res)
        self.assertIn("'in_progress' -> 'user_review' is not a move on this board", res["error"])
        self.assertEqual(self.task(tid).status, "running", "a refused move must change nothing")

    def test_entering_user_review_requires_the_user_as_assignee(self):
        """requires (to enter): assignee: user. A second human lane that is not
        the user may not receive finished work in user_review."""
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        roles:
          coding: {profiles: [coding]}
          qa: {profiles: [qa-tester]}
          user: {human: true, assignee: alice}
          bystander: {human: true, assignee: someone-else}
        transitions:
          - {from_role: qa, action: handoff, to_role: bystander, status: scheduled}
        """)
        # The board declares the bystander lane, so the load passes (T44) and
        # the refusal comes from the column's own condition.
        import yaml
        doc = yaml.safe_load(BOARD_YAML.read_text())
        doc["roles"]["bystander"] = {"human": True, "receives": "user_review"}
        board = _HOME / "board-with-bystander.yaml"
        board.write_text(yaml.safe_dump(doc, sort_keys=False))
        tid = self.new_running_task("qa-tester")
        with BoardFile(board), Config(cfg), Worker(tid, "qa-tester"):
            res = call_tool("kanban_handoff", {"summary": "verified"})
        self.assertIn("error", res)
        self.assertIn("to enter 'user_review'", res["error"])
        self.assertIn("assigned to 'user'", res["error"])
        self.assertEqual(self.task(tid).status, "running")

    def test_leaving_a_column_can_require_acceptance_criteria(self):
        """requires_to_leave: the direction D1 settled."""
        spec = _board("board-ac-to-leave.yaml", """
            board:
              columns:
                to_do:       {status: ready, exit_to: [in_progress]}
                in_progress:
                  status: running
                  exit_to: [to_do, user_review]
                  requires_to_leave: {acceptance_criteria: true}
                user_review: {status: scheduled, exit_to: [done]}
                question: {status: blocked}
                done: {}
        """)
        without = self.new_running_task("coding")
        with BoardFile(spec), Worker(without, "coding"):
            res = call_tool("kanban_handoff", {"summary": "done"})
        self.assertIn("error", res)
        self.assertIn("to leave 'in_progress'", res["error"])
        self.assertIn("route the card to the user as a question", res["error"])

        with_ac = self.new_running_task("coding")
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET body = ? WHERE id = ?",
                ("## Acceptance criteria\n- the page renders", with_ac))
        with BoardFile(spec), Worker(with_ac, "coding"):
            self.assertTrue(call_tool("kanban_handoff", {"summary": "done"}).get("ok"))

    def test_entering_a_column_can_require_a_referenced_card(self):
        spec = _board("board-ref-to-enter.yaml", """
            board:
              columns:
                to_do:
                  status: ready
                  exit_to: [in_progress]
                  requires: {references_card: true}
                in_progress: {status: running, exit_to: [to_do, user_review]}
                user_review: {status: scheduled}
                question: {status: blocked}
        """)
        tid = self.new_running_task("coding")
        with BoardFile(spec), Worker(tid, "coding"):
            res = call_tool("kanban_handoff", {"summary": "done"})
        self.assertIn("error", res)
        self.assertIn("link it to the card it waits for", res["error"])

    def test_an_unknown_condition_fails_closed_and_says_so(self):
        spec = _board("board-unknown-cond.yaml", """
            board:
              columns:
                to_do: {status: ready, exit_to: [in_progress], requires: {moon_phase: full}}
                in_progress: {status: running, exit_to: [to_do, user_review]}
                user_review: {status: scheduled}
                question: {status: blocked}
        """)
        tid = self.new_running_task("coding")
        with BoardFile(spec), Worker(tid, "coding"):
            res = call_tool("kanban_handoff", {"summary": "done"})
        self.assertIn("error", res)
        self.assertIn("unknown condition 'moon_phase'", res["error"])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
