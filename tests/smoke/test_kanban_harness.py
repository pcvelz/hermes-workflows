#!/usr/bin/env python3
"""End-to-end tests for plugins/kanban-harness against the REAL hermes-agent kanban_db.

Runs in a scratch HERMES_HOME (hard-refuses the real ~/.hermes). Registers the plugin
through the real PluginContext, then:

  * attempts every forbidden transition / side door and expects a refusal that comes
    back through the real pre_tool_call pipeline, with the task unchanged;
  * drives the allowed path end to end with the shipped example matrix:
      dispatcher spawns coder -> coder hands off -> dispatcher spawns QA ->
      QA hands off -> task parked in the human lane (nothing spawned) ->
      the human completes it via the real CLI; plus the human's rework path;
  * for each configuration option (per-board matrices, sub-task gate, shell verbs,
    warn mode, human-lane status, the table being the only source of permissions)
    checks one refused and one allowed case.

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
for prof in ("coder", "qa-tester"):
    (_HOME / "profiles" / prof).mkdir(parents=True)

EXAMPLE = (PLUGIN_DIR / "harness.yaml.example").read_text()
HARNESS = _HOME / "harness.yaml"
HARNESS.write_text(EXAMPLE.replace("assignee: architect", "assignee: peter"))
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


def blocked(tool, args):
    return get_pre_tool_call_block_message(tool, args)


def call_tool(tool, args):
    return json.loads(model_tools.handle_function_call(tool, args))


# A compact matrix used by the option tests; each test appends its own rows/keys.
_BASE = """
roles:
  coding: {profiles: [coder]}
  qa: {profiles: [qa-tester]}
  architect: {human: true, assignee: peter}
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

    def new_running_task(self, profile="coder", title="t"):
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


class ForbiddenTransitions(HarnessCase):
    def test_coder_cannot_complete_block_or_unblock(self):
        tid = self.new_running_task("coder")
        with Worker(tid, "coder"):
            for tool in ("kanban_complete", "kanban_block", "kanban_unblock"):
                msg = blocked(tool, {"task_id": tid, "summary": "done", "reason": "x"})
                self.assertIsNotNone(msg, tool)
                self.assertIn("kanban_handoff", msg)
            # through the real dispatch path: a readable tool error, task unchanged
            res = call_tool("kanban_complete", {"task_id": tid, "summary": "all good"})
            self.assertIn("error", res)
            self.assertIn("kanban-harness", res["error"])
        self.assertEqual(self.task(tid).status, "running")

    def test_qa_cannot_complete_or_send_back(self):
        tid = self.new_running_task("qa-tester")
        with Worker(tid, "qa-tester"):
            for tool in ("kanban_complete", "kanban_block", "kanban_unblock"):
                self.assertIsNotNone(blocked(tool, {"task_id": tid}), tool)
            self.assertIn("error", call_tool("kanban_complete", {"task_id": tid, "summary": "ok"}))
            # QA's only hand-off target is the architect, not back to coding
            res = call_tool("kanban_handoff", {"summary": "x", "to_role": "coding"})
            self.assertIn("error", res)
        self.assertEqual(self.task(tid).status, "running")

    def test_unknown_profile_cannot_mutate(self):
        with Worker("t_nothing", "orchestrator"):
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x"}))
            self.assertIsNotNone(blocked("kanban_create", {"title": "x"}))
            self.assertIsNone(blocked("kanban_show", {"task_id": "t_x"}))

    def test_handoff_only_own_running_task(self):
        tid = self.new_running_task("coder")
        other = kb.create_task(self.conn, title="other", assignee="coder")
        with Worker(tid, "coder"):
            self.assertIn("error", call_tool("kanban_handoff", {"task_id": other, "summary": "x"}))
            self.assertIn("error", call_tool("kanban_handoff", {"summary": "  "}))
        with Worker(tid, "qa-tester"):  # not the assignee
            self.assertIn("error", call_tool("kanban_handoff", {"summary": "x"}))
        self.assertEqual(self.task(tid).status, "running")

    def test_side_doors_refused(self):
        tid = self.new_running_task("coder")
        db = str(kb.kanban_db_path())
        doors = [
            ("terminal", {"command": f"hermes kanban complete {tid} --summary ok"}),
            ("terminal", {"command": f"hermes -p coder kanban --board default block {tid} x"}),
            ("terminal", {"command": f"cd /tmp && hermes kanban unblock {tid}"}),
            ("terminal", {"command": f"echo; hermes kanban promote {tid}"}),
            ("terminal", {"command": f"hermes kanban assign {tid} peter"}),
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
        with Worker(tid, "coder"):
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
            with Worker("t_x", "coder"):
                self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x"}))
                self.assertIsNone(blocked("terminal", {"command": "ls"}))
        finally:
            os.environ["KANBAN_HARNESS_FILE"] = old


class AllowedPath(HarnessCase):
    def test_allowed_path_end_to_end(self):
        tid = kb.create_task(self.conn, title="feature", assignee="coder")
        self.tick()  # dispatcher: ready -> running, spawns the coder
        self.assertIn((tid, "coder"), self.spawned)

        with Worker(tid, "coder"):
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
        self.assertEqual((t.status, t.assignee), ("blocked", "peter"))

        type(self).spawned = []
        self.tick()  # parked in the human lane: not promoted, never spawned
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.task(tid).status, "blocked")

        self.human_cli("complete", tid, "--summary", "accepted by architect")
        self.assertEqual(self.task(tid).status, "done")
        kinds = [e.kind for e in kb.list_events(self.conn, tid)]
        self.assertEqual(kinds.count("handed_off"), 2, kinds)

    def test_human_rework_path(self):
        tid = self.new_running_task("coder")
        with Worker(tid, "coder"):
            self.assertTrue(call_tool("kanban_handoff", {"summary": "v1"}).get("ok"))
        self.tick()
        with Worker(tid, "qa-tester"):
            self.assertTrue(call_tool("kanban_handoff", {"summary": "4 bad rows"}).get("ok"))
        # the human sends it back to coding with feedback
        self.human_cli("comment", tid, "rows 3,5,7,9 wrong; redo")
        self.human_cli("reassign", tid, "coder")
        self.human_cli("unblock", tid)
        type(self).spawned = []
        self.tick()
        self.assertIn((tid, "coder"), self.spawned)
        self.assertEqual(self.task(tid).status, "running")


class Options(HarnessCase):
    def test_per_board_matrices(self):
        cfg = _BASE + """
        transitions:
          - {from_role: coding, action: handoff, to_role: qa, status: ready}
        boards:
          "*": {}
          scratch:
            transitions:
              - {from_role: coding, action: complete}
        """
        with Config(textwrap.dedent(_BASE) + textwrap.dedent(cfg[len(_BASE):])), \
                Worker("t_x", "coder"):
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x", "board": "default"}))
            self.assertIsNone(blocked("kanban_complete", {"task_id": "t_x", "board": "scratch"}))
        # without "*", boards not listed are not harnessed
        cfg2 = textwrap.dedent(_BASE) + "transitions: []\nboards: [only-this]\n"
        with Config(cfg2), Worker("t_x", "coder"):
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x", "board": "only-this"}))
            self.assertIsNone(blocked("kanban_complete", {"task_id": "t_x", "board": "default"}))

    def test_sub_task_gate(self):
        parent = kb.create_task(self.conn, title="batch", assignee="coder")
        child = kb.create_task(self.conn, title="row 1", assignee="qa-tester", parents=[parent])
        marked = kb.create_task(self.conn, title="row 2", assignee="qa-tester",
                                parents=[parent], body="Level: 0\ncheck it")
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: qa, action: complete, when: non_root}
        """)
        with Config(cfg):
            for tid, expect_refused in ((child, False), (parent, True), (marked, True)):
                with Worker(tid, "qa-tester"):
                    msg = blocked("kanban_complete", {"task_id": tid})
                    self.assertEqual(msg is not None, expect_refused, (tid, msg))
            with Worker(child, "coder"):  # the gate is QA's, not coding's
                self.assertIsNotNone(blocked("kanban_complete", {"task_id": child}))

    def test_per_role_shell_verbs(self):
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: coding, action: create, via: [tool, shell]}
          - {from_role: coding, action: link, via: shell}
        """)
        with Config(cfg):
            with Worker("t_x", "coder"):
                self.assertIsNone(blocked("terminal", {"command": "hermes kanban create 'row 7'"}))
                self.assertIsNone(blocked("terminal", {"command": "hermes kanban link t_a t_b"}))
                self.assertIsNone(blocked("terminal", {"command": "hermes kanban list"}))
                self.assertIsNotNone(blocked("terminal", {"command": "hermes kanban complete t_x"}))
                self.assertIsNotNone(blocked("kanban_link", {"parent_id": "t_a"}))  # shell-only row
            with Worker("t_x", "qa-tester"):
                self.assertIsNotNone(blocked("terminal", {"command": "hermes kanban create 'x'"}))

    def test_warn_mode(self):
        log = _HOME / "warn.log"
        cfg = textwrap.dedent(_BASE) + f"mode: warn\nlog_file: {log}\ntransitions: []\n"
        with Config(cfg), Worker("t_x", "coder"):
            self.assertIsNone(blocked("kanban_complete", {"task_id": "t_x"}))
            self.assertIsNone(blocked("terminal", {"command": "hermes kanban complete t_x"}))
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all("[warn] [WOULD-BLOCK]" in ln for ln in lines), lines)
        self.assertIn("tool=kanban_complete", lines[0])
        # enforce mode with the same config refuses (and logs BLOCKED)
        with Config(cfg.replace("mode: warn", "mode: enforce")), Worker("t_x", "coder"):
            self.assertIsNotNone(blocked("kanban_complete", {"task_id": "t_x"}))
        self.assertIn("[enforce] [BLOCKED]", log.read_text().splitlines()[-1])

    def test_human_lane_status_ready(self):
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: qa, action: handoff, to_role: architect, status: ready}
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
        cfg = "roles:\n  coding: {profiles: [coder]}\ntransitions: []\n"
        with Config(cfg), Worker("t_x", "coder"):
            self.assertIsNotNone(blocked("kanban_show", {"task_id": "t_x"}))
            self.assertIsNotNone(blocked("kanban_create", {"title": "x"}))
        # a single profile named as its own role, granted complete by one row
        cfg = "roles:\n  coder: {}\ntransitions:\n  - {from_role: coder, action: complete}\n"
        with Config(cfg), Worker("t_x", "coder"):
            self.assertIsNone(blocked("kanban_complete", {"task_id": "t_x"}))
            self.assertIsNotNone(blocked("kanban_block", {"task_id": "t_x"}))

    def test_handoff_requires_evidence(self):
        ws = Path(tempfile.mkdtemp(dir=_HOME))
        cfg = textwrap.dedent(_BASE) + textwrap.dedent("""
        transitions:
          - {from_role: coding, action: handoff, to_role: qa, status: ready,
             requires: {files: "findings/**/*.md", matches: '(?m)^Verified: yes$'}}
        """)
        tid = self.new_running_task("coder")
        os.environ["HERMES_KANBAN_WORKSPACE"] = str(ws)
        try:
            with Config(cfg), Worker(tid, "coder"):
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
        tid = self.new_running_task("coder")
        with Worker(tid, "coder"):
            self.assertTrue(call_tool("kanban_handoff", {"summary": "v1"}).get("ok"))
        kinds = [e.kind for e in kb.list_events(self.conn, tid)]
        self.assertGreater(kinds.index("promoted", kinds.index("handed_off")),
                           kinds.index("handed_off"), kinds)

    def test_bad_config_rejected(self):
        cfg = textwrap.dedent(_BASE) + "transitions:\n  - {from_role: qa, action: handoff, to_role: nobody, status: ready}\n"
        with Config(cfg), Worker("t_x", "qa-tester"):
            self.assertIn("config unreadable", blocked("kanban_complete", {"task_id": "t_x"}))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
