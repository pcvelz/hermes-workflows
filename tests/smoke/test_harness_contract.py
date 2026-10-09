# @user-gated
"""The harness contract, one rule per test, each rule the user's.

Only the user changes this file, in their own session. No skip, no xfail, no
"temporary" disable: a test that is inconvenient is the test doing its job.

Runs against the REAL hermes-agent kanban_db and the real pre_tool_call
pipeline, in a scratch HOME and HERMES_HOME (it refuses the real ~/.hermes).

T1  a worker cannot complete its own card -- the incident of 2026-09-21
    17:24 on a live board, where the refusal was logged as WOULD-BLOCK and
    the move went through anyway.
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "kanban-harness"
AGENT_SRC = Path(os.path.expanduser(
    os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent"))).resolve()

# --- isolation: a scratch HOME, so "~/.hermes/logs" is scratch too ----------
_SCRATCH = Path(tempfile.mkdtemp(prefix="harness-contract-")).resolve()
_REAL = Path(os.path.expanduser("~/.hermes")).resolve()
_HOME = _SCRATCH / "home"
_HERMES = _HOME / ".hermes"
if _REAL in _HERMES.parents or _HERMES == _REAL:
    sys.exit("refusing: scratch home resolves inside the real ~/.hermes")
_HERMES.mkdir(parents=True)
# The dispatcher must not split cards outside a tool call (T43).
(_HERMES / "config.yaml").write_text("kanban:\n  auto_decompose: false\n")
for prof in ("coding", "coding-worker", "qa-tester"):
    (_HERMES / "profiles" / prof).mkdir(parents=True)
_LOG = _HERMES / "logs" / "kanban-harness.log"

_ENV = {
    "HOME": str(_HOME),
    "HERMES_HOME": str(_HERMES),
    "KANBAN_HARNESS_FILE": str(PLUGIN_DIR / "harness.yaml.example"),
    "KANBAN_BOARD_FILE": None,
}
_SAVED = {}


def _apply_env():
    for k in [k for k in os.environ if k.startswith("HERMES_KANBAN")] + ["HERMES_PROFILE"]:
        os.environ.pop(k, None)
    for key, value in _ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# At import only HERMES_HOME is pinned (the agent import needs it). HOME is
# pinned in setUpModule and restored after: under one pytest process every
# test file is imported before any runs, and a HOME changed at import time
# would send the NEXT file looking for the agent under this scratch home.
for key in _ENV:
    _SAVED[key] = os.environ.get(key)
os.environ["HERMES_HOME"] = str(_HERMES)

if not (AGENT_SRC / "hermes_cli" / "kanban_db.py").is_file():
    print(f"SKIP: hermes-agent source not found at {AGENT_SRC}")
    sys.exit(77)
sys.path.insert(0, str(AGENT_SRC))
try:
    from hermes_cli import kanban_db as kb
    from hermes_cli.plugins import (PluginContext, PluginManifest, get_plugin_manager,
                                    get_pre_tool_call_block_message)
    import model_tools
except Exception as exc:
    print(f"SKIP: cannot import hermes-agent ({exc})")
    sys.exit(77)

_spec = importlib.util.spec_from_file_location("kanban_harness_contract",
                                               PLUGIN_DIR / "__init__.py")
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)
harness.register(PluginContext(PluginManifest(name="kanban-harness"), get_plugin_manager()))


def setUpModule():
    _apply_env()


def tearDownModule():
    for key, value in _SAVED.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class Worker:
    """The environment a dispatcher-spawned worker for `task_id` runs with."""

    def __init__(self, task_id, profile):
        self.env = {"HERMES_KANBAN_TASK": task_id, "HERMES_PROFILE": profile}

    def __enter__(self):
        os.environ.update(self.env)
        return self

    def __exit__(self, *a):
        for k in self.env:
            os.environ.pop(k, None)


class Contract(unittest.TestCase):

    def setUp(self):
        self.conn = kb.connect()

    def tearDown(self):
        self.conn.close()

    def running_card(self, profile):
        tid = kb.create_task(self.conn, title="contract card", assignee=profile)
        kb.dispatch_once(self.conn, spawn_fn=lambda task, ws, board=None: None)
        self.assertEqual(kb.get_task(self.conn, tid).status, "running")
        return tid

    def status(self, tid):
        self.conn.close()
        self.conn = kb.connect()
        return kb.get_task(self.conn, tid).status

    def test_T1_a_worker_cannot_complete_its_own_card(self):
        tid = self.running_card("coding")
        before = _LOG.read_text() if _LOG.exists() else ""

        with Worker(tid, "coding"):
            message = get_pre_tool_call_block_message(
                "kanban_complete", {"task_id": tid, "summary": "done"})
            result = json.loads(model_tools.handle_function_call(
                "kanban_complete", {"task_id": tid, "summary": "done"}))

        # 1. refused, naming the role and the missing grant
        self.assertIsNotNone(message, "kanban_complete was not refused")
        self.assertIn("'coding'", message)
        self.assertIn("kanban_complete", message)
        self.assertIn("error", result, f"the call went through: {result}")
        # 2. the board never saw the move
        self.assertEqual(self.status(tid), "running")
        # 3. the refusal is on disk at the one fixed path, as a refusal
        written = (_LOG.read_text() if _LOG.exists() else "")[len(before):]
        self.assertIn(f"tool=kanban_complete", written,
                      f"no refusal in {_LOG}")
        self.assertIn("[BLOCKED]", written)
        self.assertNotIn("WOULD-BLOCK", written)

    def test_T1_replayed_with_the_live_config_of_17_24(self):
        """The incident's own config: the live harness as it was when
        a coding card completed itself -- `mode: warn`. Whatever that key does
        today (it no longer exists), the move must be refused and logged."""
        incident = _SCRATCH / "harness-as-of-17-24.yaml"
        incident.write_text(
            "enabled: true\nmode: warn\n"
            "roles:\n  coding: {profiles: [coding]}\n  qa: {profiles: [qa-tester]}\n"
            "  user: {human: true, assignee: user}\n"
            "transitions:\n"
            "  - {from_role: coding, action: handoff, to_role: qa, status: ready}\n"
            "  - {from_role: qa, action: handoff, to_role: user, status: blocked}\n"
            "always_allow:\n  tools: [kanban_show, kanban_list, kanban_heartbeat, kanban_comment]\n"
            "boards:\n  \"*\": {}\n"
        )
        tid = self.running_card("coding")
        before = _LOG.read_text() if _LOG.exists() else ""
        old = os.environ["KANBAN_HARNESS_FILE"]
        os.environ["KANBAN_HARNESS_FILE"] = str(incident)
        try:
            with Worker(tid, "coding"):
                message = get_pre_tool_call_block_message(
                    "kanban_complete", {"task_id": tid, "summary": "done"})
                result = json.loads(model_tools.handle_function_call(
                    "kanban_complete", {"task_id": tid, "summary": "done"}))
        finally:
            os.environ["KANBAN_HARNESS_FILE"] = old

        self.assertIsNotNone(message, "the 17:24 config let kanban_complete through")
        self.assertIn("error", result, f"the call went through: {result}")
        self.assertEqual(self.status(tid), "running")
        written = (_LOG.read_text() if _LOG.exists() else "")[len(before):]
        self.assertIn("tool=kanban_complete", written, f"no refusal in {_LOG}")
        self.assertIn("[BLOCKED]", written)
        self.assertNotIn("WOULD-BLOCK", written)


# ---------------------------------------------------------------------------
# The user's hand: accept, the accept trace, the ledger, reopen
# ---------------------------------------------------------------------------

sys.path.insert(0, str(REPO / "scripts" / "resilience"))
BOARD_CLI = REPO / "scripts" / "resilience" / "board_cli.py"
BOARD_YAML = REPO / "config" / "board.yaml"


def _user_env(**extra):
    """A person at a terminal: no Hermes identity at all."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env["HOME"] = str(_HOME)
    env.update(extra)
    return env


def run_board_cli(*argv, tty=True, **extra_env):
    """board_cli.py as a real process. tty=True gives it a real pty on stdin,
    the way a person runs it; tty=False gives it /dev/null, the way any tool
    call or pipe runs it."""
    import subprocess
    if tty:
        master, slave = os.openpty()
        try:
            out = subprocess.run([sys.executable, str(BOARD_CLI), *argv], stdin=slave,
                                 capture_output=True, text=True, timeout=60,
                                 env=_user_env(**extra_env))
        finally:
            os.close(slave)
            os.close(master)
    else:
        out = subprocess.run([sys.executable, str(BOARD_CLI), *argv],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=60, env=_user_env(**extra_env))
    return out


class UsersHand(unittest.TestCase):

    def setUp(self):
        import board_cli
        self.bc = board_cli
        # A fresh board per test, so no test inherits another's ledger or cards
        # (T30 deletes the ledger on purpose). Initialise BEFORE connecting: the
        # first connect could otherwise hold a file that initialisation then
        # replaces, and every write would land where no other process can see it.
        db = Path(str(kb.kanban_db_path()))
        for suffix in ("", "-wal", "-shm", ".accepts.jsonl"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        kb.init_db()
        self.conn = kb.connect()
        self.db = str(kb.kanban_db_path())
        self.ledger = Path(self.db + ".accepts.jsonl")

    def tearDown(self):
        self.conn.close()

    def fresh(self):
        self.conn.close()
        self.conn = kb.connect()

    def card(self, status, assignee="user"):
        tid = kb.create_task(self.conn, title=f"card in {status}", assignee=assignee)
        with self.conn:
            self.conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        return tid

    def done_without_accept(self, status="done"):
        """How today's escape happened: done through the runtime's own complete."""
        tid = self.card("blocked")
        self.assertTrue(kb.complete_task(self.conn, tid, summary="closed without accept"))
        if status == "archived":
            self.assertTrue(kb.archive_task(self.conn, tid))
        # complete_task can leave this connection's transaction open; without the
        # commit a separate board_cli process does not see the card, and a test
        # passes only when an earlier test happened to commit first.
        self.conn.commit()
        return tid

    def cli(self, *argv, **kw):
        return run_board_cli("--db", self.db, "--board-file", str(BOARD_YAML), *argv, **kw)

    def init_ledger(self):
        out = self.cli("init-ledger")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def row(self, tid):
        self.fresh()
        return self.conn.execute("SELECT status, assignee FROM tasks WHERE id = ?",
                                 (tid,)).fetchone()

    def comments(self, tid):
        self.fresh()
        return [r[0] for r in self.conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,))]

    # T13 -------------------------------------------------------------------
    def test_T13_the_accept_trace_survives_the_runtimes_event_gc(self):
        self.init_ledger()
        tid = self.card("scheduled")
        out = self.cli("accept", tid, "--comment", "verified by hand")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.fresh()
        kb.gc_events(self.conn, older_than_seconds=-60)   # every event of the card gone
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ?", (tid,)).fetchone())
        self.assertIsNotNone(self.bc.accept_trace(self.conn, tid),
                             "the accept left no trace once GC ran")
        refused = self.cli("reopen", tid, "--reason", "try it")
        self.assertNotEqual(refused.returncode, 0, "an accepted card was reopened after GC")
        self.assertEqual(self.row(tid)[0], "done")

    # T14 -------------------------------------------------------------------
    def test_T14_reopen_brings_an_escaped_done_card_back_to_user_review(self):
        self.init_ledger()
        tid = self.done_without_accept()
        kb.add_comment(self.conn, tid, "coding", "evidence at findings/x.md")
        before = self.comments(tid)
        out = self.cli("reopen", tid, "--reason", "completed by a worker; never accepted")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(tuple(self.row(tid)), ("scheduled", "user"))
        after = self.comments(tid)
        self.assertEqual(after[:len(before)], before, "earlier comments were lost")
        self.assertEqual(len(after), len(before) + 1)
        self.assertIn("completed by a worker; never accepted", after[-1])

    # T15 -------------------------------------------------------------------
    def test_T15_reopen_brings_an_escaped_archived_card_back(self):
        self.init_ledger()
        tid = self.done_without_accept("archived")
        out = self.cli("reopen", tid, "--reason", "archived out of sight")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(tuple(self.row(tid)), ("scheduled", "user"))

    # T16 -------------------------------------------------------------------
    def test_T16_reopen_refuses_an_accepted_card_and_says_when(self):
        self.init_ledger()
        tid = self.card("scheduled")
        self.assertEqual(self.cli("accept", tid).returncode, 0)
        out = self.cli("reopen", tid, "--reason", "changed my mind")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("accepted", out.stderr)
        self.assertRegex(out.stderr, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertEqual(self.row(tid)[0], "done")

    # T17 -------------------------------------------------------------------
    def test_T17_only_the_users_hand_can_reopen(self):
        self.init_ledger()
        tid = self.done_without_accept()
        attempts = {
            "no tty (a pipe or a tool call)": dict(tty=False),
            "under a profile": dict(HERMES_PROFILE="coding"),
            "a profile's HERMES_HOME": dict(HERMES_HOME=str(_HERMES / "profiles" / "coding")),
            "inside a worker": dict(HERMES_KANBAN_TASK=tid),
        }
        for label, kw in attempts.items():
            with self.subTest(label):
                before = _LOG.read_text() if _LOG.exists() else ""
                out = self.cli("reopen", tid, "--reason", "x", **kw)
                # 1. the caller received the refusal, final, with its one legal move
                self.assertNotEqual(out.returncode, 0, f"{label}: reopen ran")
                self.assertIn("This refusal is final", out.stderr)
                self.assertIn("kanban_handoff", out.stderr)
                # 2. the board did not move
                self.assertEqual(self.row(tid)[0], "done")
                # 3. the refusal is on disk
                written = (_LOG.read_text() if _LOG.exists() else "")[len(before):]
                self.assertIn("[BLOCKED]", written, f"{label}: no refusal on disk")
                self.assertIn("tool=board_cli reopen", written)
        with Worker(tid, "coding"):
            _apply_env()
            self.assertIsNotNone(get_pre_tool_call_block_message(
                "terminal", {"command": f"python3 {BOARD_CLI} reopen {tid} --reason x"}))
        self.assertEqual(self.row(tid)[0], "done")

    # T36 -------------------------------------------------------------------
    def test_T36_a_hermes_process_among_the_ancestors_is_refused_even_with_a_tty(self):
        """A terminal does not change who spawned you. A caller whose parent is a
        Hermes worker, dispatcher or gateway is an agent, with a TTY or not."""
        import subprocess
        self.init_ledger()
        tid = self.done_without_accept()
        # A parent that looks exactly like a Hermes worker's process: an
        # interpreter at .../hermes-agent/venv/bin/hermes. It launches board_cli
        # as its child, with a real terminal on stdin.
        bindir = _SCRATCH / "hermes-agent" / "venv" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        worker = bindir / "hermes"
        if not worker.exists():
            worker.symlink_to(sys.executable)
        child = [sys.executable, str(BOARD_CLI), "--db", self.db,
                 "--board-file", str(BOARD_YAML), "reopen", tid, "--reason", "x"]
        launcher = ("import subprocess, sys; "
                    f"sys.exit(subprocess.call({child!r}))")

        def launch(parent):
            master, slave = os.openpty()
            try:
                return subprocess.run([parent, "-c", launcher], stdin=slave,
                                      capture_output=True, text=True, timeout=60,
                                      env=_user_env())
            finally:
                os.close(slave)
                os.close(master)

        before = _LOG.read_text() if _LOG.exists() else ""
        seen = subprocess.run(
            [sys.executable, "-c",
             f"import sqlite3; print(sqlite3.connect({self.db!r}).execute("
             f"'select id, status from tasks').fetchall())"],
            capture_output=True, text=True, timeout=30)
        opened = [r[2] for r in self.conn.execute("PRAGMA database_list")]
        self.assertIn(tid, seen.stdout, f"another process cannot see the card in {self.db}; "
                                        f"the test connection has open: {opened}; "
                                        f"in_transaction={self.conn.in_transaction} "
                                        f"isolation_level={self.conn.isolation_level!r}")
        out = launch(str(worker))
        self.assertNotEqual(out.returncode, 0, "reopen ran under a Hermes parent")
        self.assertIn("spawned by a Hermes process", out.stderr, out.stdout + out.stderr)
        self.assertEqual(self.row(tid)[0], "done")
        written = (_LOG.read_text() if _LOG.exists() else "")[len(before):]
        self.assertIn("[BLOCKED]", written)
        # Control: the same launch from a parent that is not Hermes succeeds, so
        # the refusal above was the ancestry and nothing else.
        ok = launch(sys.executable)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(self.row(tid)[0], "scheduled")

    # T18 -------------------------------------------------------------------
    def test_T18_reopen_twice_is_not_an_error_and_writes_once(self):
        self.init_ledger()
        tid = self.done_without_accept()
        self.assertEqual(self.cli("reopen", tid, "--reason", "once").returncode, 0)
        n = len(self.comments(tid))
        second = self.cli("reopen", tid, "--reason", "once")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(len(self.comments(tid)), n, "the second reopen commented again")
        self.fresh()
        reopens = self.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'reopened'",
            (tid,)).fetchone()[0]
        self.assertEqual(reopens, 1)

    # T30 -------------------------------------------------------------------
    def test_T30_a_missing_ledger_is_a_fault_not_an_empty_one(self):
        self.init_ledger()
        accepted = self.card("scheduled")
        self.assertEqual(self.cli("accept", accepted).returncode, 0)
        escaped = self.done_without_accept()
        self.ledger.unlink()
        for tid in (accepted, escaped):
            with self.subTest(tid):
                out = self.cli("reopen", tid, "--reason", "x")
                self.assertNotEqual(out.returncode, 0, "reopen ran with the ledger gone")
                self.assertIn("ledger", out.stderr)
                self.assertEqual(self.row(tid)[0], "done")
        import board as board_mod
        import escalator
        sent = []

        class Rec:
            name = "rec"

            def send(self, m):
                sent.append(m)

        cur = Path(self.db).parent / ".cur-t30"
        cur.write_text("0")
        escalator.run_once(self.db, board="default", config={}, apply=False, notify=True,
                           cursor_path=str(cur), sender=Rec(),
                           state_path=str(Path(self.db).parent / ".pages-t30.json"),
                           board_spec=board_mod.load_board(BOARD_YAML),
                           hermes_home=str(_HERMES))
        self.assertTrue([m for m in sent if "ledger_missing" in m.title],
                        "the escalator did not page the missing ledger")


# ---------------------------------------------------------------------------
# The table lives in board.yaml; a bad board file refuses
# ---------------------------------------------------------------------------

CONTRACT_DOC = REPO / "docs" / "harness-contract.md"
_COL = {"refinement": "refinement", "waiting": "waiting", "to do": "to_do",
        "in progress": "in_progress", "question": "question",
        "user review": "user_review", "done": "done", "archived": "archived"}


def _doc_table():
    """(from, to, actor) triples from the contract's readable table."""
    rows, inside = set(), False
    for line in CONTRACT_DOC.read_text().splitlines():
        if line.startswith("| from | to | actor |"):
            inside = True
            continue
        if inside:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells[0].startswith("---"):
                continue
            rows.add((_COL[cells[0]], _COL[cells[1]], cells[2]))
    return rows


class TheTable(unittest.TestCase):

    def test_T31_board_yaml_moves_and_the_contract_table_agree(self):
        import board as board_mod
        spec = board_mod.load_board(BOARD_YAML)
        self.assertEqual(spec.errors, [])
        from_yaml = {(m["from"], m["to"], m["by"]) for m in spec.moves}
        from_doc = _doc_table()
        # 22 rows signed off 2026-09-21; +4 planner rows 2026-09-22 (the split
        # card reaches the planner, its children go to the lanes, its own card
        # goes to the user, and it may ask when it cannot cut). This number is
        # the record of what the user approved: changing it is a deliberate act.
        self.assertEqual(len(from_doc), 27, "the signed-off table has 27 rows")
        self.assertEqual(from_yaml, from_doc,
                         f"only in board.yaml: {sorted(from_yaml - from_doc)}; "
                         f"only in the contract: {sorted(from_doc - from_yaml)}")

    def test_T32_a_missing_or_broken_board_file_refuses_every_move(self):
        tid = None
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="board file", assignee="coding")
            kb.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: None)
        finally:
            conn.close()
        broken = {
            "missing": _SCRATCH / "no-such-board.yaml",
            "unreadable": _SCRATCH / "unreadable-board.yaml",
            "invalid YAML": _SCRATCH / "invalid-board.yaml",
        }
        broken["unreadable"].write_text("board: {}\n")
        broken["unreadable"].chmod(0)
        broken["invalid YAML"].write_text("board: [unclosed\n")
        old = os.environ.get("KANBAN_BOARD_FILE")
        try:
            for label, path in broken.items():
                with self.subTest(label):
                    os.environ["KANBAN_BOARD_FILE"] = str(path)
                    before = _LOG.read_text() if _LOG.exists() else ""
                    with Worker(tid, "coding"):
                        msg = get_pre_tool_call_block_message(
                            "kanban_handoff", {"summary": "done"})
                        res = json.loads(model_tools.handle_function_call(
                            "kanban_handoff", {"summary": "done"}))
                    self.assertIsNotNone(msg, f"{label}: the hand-off was not refused")
                    self.assertIn("error", res, f"{label}: the hand-off went through")
                    c = kb.connect()
                    try:
                        self.assertEqual(kb.get_task(c, tid).status, "running")
                    finally:
                        c.close()
                    written = (_LOG.read_text() if _LOG.exists() else "")[len(before):]
                    self.assertIn("[BLOCKED]", written, f"{label}: nothing on disk")
        finally:
            broken["unreadable"].chmod(0o600)
            if old is None:
                os.environ.pop("KANBAN_BOARD_FILE", None)
            else:
                os.environ["KANBAN_BOARD_FILE"] = old


class LedgerNeverShrinks(unittest.TestCase):

    def test_T34_a_ledger_that_shrinks_is_a_fault_even_after_gc(self):
        """The one window left: GC erased every accept event, then the ledger is
        deleted and re-created empty by the user's own hand. The escalator
        remembers the most accepts it has ever seen, outside the ledger."""
        import board as board_mod
        import board_cli
        import escalator
        conn = kb.connect()
        try:
            db = str(kb.kanban_db_path())
            ledger = board_cli.ledger_path(conn)
        finally:
            conn.close()
        state = Path(db).parent / ".pages-t34.json"
        cur = Path(db).parent / ".cur-t34"
        cur.write_text("0")
        sent = []

        class Rec:
            name = "rec"

            def send(self, m):
                sent.append(m)

        def tick():
            escalator.run_once(db, board="default", config={}, apply=False, notify=True,
                               cursor_path=str(cur), sender=Rec(), state_path=str(state),
                               board_spec=board_mod.load_board(BOARD_YAML),
                               hermes_home=str(_HERMES))

        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a") as fh:
            for i in range(3):
                fh.write(json.dumps({"card": f"t_seen{i}", "board": "default",
                                     "at": 1790000000 + i, "by": "user"}) + "\n")
        tick()
        self.assertEqual([m for m in sent if "ledger" in m.title], [])
        ledger.write_text("")          # re-created empty, as if by hand
        tick()
        self.assertTrue([m for m in sent if "ledger_shrank" in m.title],
                        "a ledger that went from 3 lines to 0 did not page")


# ---------------------------------------------------------------------------
# The baseline lives in code: configuration can only make it stricter
# ---------------------------------------------------------------------------

_PERMISSIVE = """
enabled: true
mode: warn
roles:
  coding: {profiles: [coding]}
  qa: {profiles: [qa-tester]}
  user: {human: true, assignee: user}
transitions:
  - {from_role: coding, action: complete}
  - {from_role: coding, action: archive, via: [tool, shell]}
  - {from_role: coding, action: unblock, via: [tool, shell]}
always_allow:
  tools: [kanban_show, kanban_complete]
  shell_verbs: [list, complete, archive]
unknown_profile: allow
side_doors:
  enabled: false
  deny_patterns: []
  deny_path_patterns: []
boards:
  "*": {}
"""


class Gate(unittest.TestCase):
    """Every refusal asserted three ways (D11): the agent got it, the board did
    not move, it is on disk."""

    def setUp(self):
        self.conn = kb.connect()
        self._restore = {k: os.environ.get(k) for k in ("KANBAN_HARNESS_FILE", "KANBAN_BOARD_FILE",
                                                         "KANBAN_HARNESS_MODE")}

    def tearDown(self):
        self.conn.close()
        for k, v in self._restore.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def config(self, text, name):
        path = _SCRATCH / name
        path.write_text(text)
        os.environ["KANBAN_HARNESS_FILE"] = str(path)
        return path

    def running_card(self, profile="coding"):
        tid = kb.create_task(self.conn, title="gate card", assignee=profile)
        kb.dispatch_once(self.conn, spawn_fn=lambda task, ws, board=None: None)
        return tid

    def status(self, tid):
        self.conn.close()
        self.conn = kb.connect()
        return kb.get_task(self.conn, tid).status

    def assert_refused(self, tid, profile, tool, args, label=""):
        before_status = self.status(tid)
        before_log = _LOG.read_text() if _LOG.exists() else ""
        with Worker(tid, profile):
            msg = get_pre_tool_call_block_message(tool, args)
            result = None
            if tool.startswith("kanban_"):
                result = json.loads(model_tools.handle_function_call(tool, args))
        # 1. the agent received the refusal
        self.assertIsNotNone(msg, f"{label}: {tool} {args} was not refused")
        if result is not None:
            self.assertIn("error", result, f"{label}: {tool} went through: {result}")
        # 2. the board did not move
        self.assertEqual(self.status(tid), before_status, f"{label}: the board moved")
        # 3. the refusal is on disk
        written = (_LOG.read_text() if _LOG.exists() else "")[len(before_log):]
        self.assertIn("[BLOCKED]", written, f"{label}: nothing on disk")
        self.assertIn(f"tool={tool}", written, f"{label}: wrong tool on disk")
        return msg

    # T3 --------------------------------------------------------------------
    def test_T3_a_permissive_table_cannot_open_the_gate(self):
        self.config(_PERMISSIVE, "permissive.yaml")
        tid = self.running_card()
        for tool, args in (
            ("kanban_complete", {"task_id": tid, "summary": "done"}),
            ("terminal", {"command": f"hermes kanban complete {tid}"}),
            ("terminal", {"command": "sqlite3 ~/.hermes/kanban.db \"update tasks set status='done'\""}),
        ):
            with self.subTest(tool=tool, args=args):
                self.assert_refused(tid, "coding", tool, args, "permissive")

    # T4 --------------------------------------------------------------------
    def test_T4_no_key_or_variable_relaxes_a_refusal(self):
        base = ("roles:\n  coding: {profiles: [coding]}\n  user: {human: true, assignee: user}\n"
                "transitions: []\n")
        relaxers = {
            "mode": "mode: warn\n",
            "enabled": "enabled: false\n",
            "side_doors.enabled": "side_doors:\n  enabled: false\n",
            "unknown_profile": "unknown_profile: allow\n",
        }
        for key, extra in relaxers.items():
            with self.subTest(key=key):
                self.config(base + extra, f"relax-{key}.yaml")
                with self.assertRaises(harness.HarnessConfigError) as ctx:
                    harness.load_config()
                self.assertIn(key.split(".")[-1], str(ctx.exception))
                tid = self.running_card()
                self.assert_refused(tid, "coding", "kanban_complete",
                                    {"task_id": tid, "summary": "x"}, key)
        self.config(base, "plain.yaml")
        os.environ["KANBAN_HARNESS_MODE"] = "warn"
        tid = self.running_card()
        self.assert_refused(tid, "coding", "kanban_complete",
                            {"task_id": tid, "summary": "x"}, "env KANBAN_HARNESS_MODE")

    # T5 --------------------------------------------------------------------
    def test_T5_every_board_is_harnessed(self):
        self.config("roles:\n  coding: {profiles: [coding]}\n  user: {human: true, assignee: user}\n"
                    "transitions: []\nboards:\n  only-this-one: {}\n", "one-board.yaml")
        tid = self.running_card()
        self.assert_refused(tid, "coding", "kanban_complete",
                            {"task_id": tid, "summary": "x", "board": "unlisted-board"},
                            "unlisted board")

    # T6 --------------------------------------------------------------------
    def test_T6_both_env_files_at_permissive_tables_still_refuse(self):
        self.config(_PERMISSIVE, "permissive-t6.yaml")
        board = _SCRATCH / "permissive-board.yaml"
        board.write_text(BOARD_YAML.read_text().replace(
            "- {from: in_progress, to: user_review, by: qa,",
            "- {from: in_progress, to: done,        by: coding}\n"
            "    - {from: in_progress, to: user_review, by: qa,"))
        os.environ["KANBAN_BOARD_FILE"] = str(board)
        tid = self.running_card()
        self.assert_refused(tid, "coding", "kanban_complete",
                            {"task_id": tid, "summary": "x"}, "both permissive")

    # T7 --------------------------------------------------------------------
    def test_T7_a_second_config_in_the_profile_home_is_not_read(self):
        self.config(harness_example(), "strict-t7.yaml")
        (_HERMES / "harness.yaml").write_text(_PERMISSIVE)
        (_HERMES / "profiles" / "coding" / "harness.yaml").write_text(_PERMISSIVE)
        try:
            tid = self.running_card()
            self.assert_refused(tid, "coding", "kanban_complete",
                                {"task_id": tid, "summary": "x"}, "shadow config")
        finally:
            (_HERMES / "harness.yaml").unlink()
            (_HERMES / "profiles" / "coding" / "harness.yaml").unlink()

    # T8 --------------------------------------------------------------------
    def test_T8_always_allow_holds_read_only_moves_only(self):
        base = ("roles:\n  coding: {profiles: [coding]}\n  user: {human: true, assignee: user}\n"
                "transitions: []\n")
        for label, extra in {
            "shell verb complete": "always_allow:\n  shell_verbs: [list, complete]\n",
            "tool kanban_complete": "always_allow:\n  tools: [kanban_show, kanban_complete]\n",
            "shell verb archive": "always_allow:\n  shell_verbs: [archive]\n",
        }.items():
            with self.subTest(label):
                self.config(base + extra, "aa.yaml")
                with self.assertRaises(harness.HarnessConfigError):
                    harness.load_config()

    # T9 --------------------------------------------------------------------
    def test_T9_the_side_door_baseline_cannot_be_removed(self):
        self.config("roles:\n  coding: {profiles: [coding]}\n  user: {human: true, assignee: user}\n"
                    "transitions: []\nside_doors:\n  deny_patterns: []\n  deny_path_patterns: []\n",
                    "no-doors.yaml")
        tid = self.running_card()
        for command in (
            "sqlite3 ~/.hermes/kanban.db \"update tasks set status='done'\"",
            "python3 -c 'from hermes_cli import kanban_db'",
            "curl -X POST http://127.0.0.1:9119/api/plugins/kanban/tasks/x/complete",
            f"python3 scripts/resilience/board_cli.py accept {tid}",
        ):
            with self.subTest(command):
                self.assert_refused(tid, "coding", "terminal", {"command": command}, command)

    # T24 -------------------------------------------------------------------
    def test_T24_no_status_verb_from_a_worker_shell(self):
        self.config(harness_example(), "t24.yaml")
        tid = self.running_card()
        for verb in (f"schedule {tid}", f"block {tid} x", f"unblock {tid}", f"promote {tid}",
                     f"assign {tid} qa-tester", f"reassign {tid} qa-tester",
                     f"unlink {tid} t_other", f"archive {tid}", f"complete {tid}",
                     f"edit {tid} --title x"):
            with self.subTest(verb):
                self.assert_refused(tid, "coding", "terminal",
                                    {"command": f"hermes kanban {verb}"}, verb)

    # T25 -------------------------------------------------------------------
    def test_T25_a_batch_with_one_forbidden_card_is_refused_whole(self):
        self.config(harness_example(), "t25.yaml")
        mine = self.running_card()
        other = kb.create_task(self.conn, title="other", assignee="coding")
        self.assert_refused(mine, "coding", "terminal",
                            {"command": f"hermes kanban complete {mine} {other}"}, "batch")
        self.assertNotEqual(self.status(other), "done")


def _agent_routes(tid):
    """Every way an agent can try to change a card's status: (tool, args, lands_in).
    `kanban_handoff` lands wherever its harness row says, so it is enumerated
    per target role separately."""
    return [
        ("kanban_complete", {"task_id": tid, "summary": "x"}, "done"),
        ("kanban_block", {"task_id": tid, "reason": "x"}, "blocked"),
        ("kanban_unblock", {"task_id": tid}, "ready"),
        ("terminal", {"command": f"hermes kanban complete {tid}"}, "done"),
        ("terminal", {"command": f"hermes kanban archive {tid}"}, "archived"),
        ("terminal", {"command": f"hermes kanban block {tid} x"}, "blocked"),
        ("terminal", {"command": f"hermes kanban unblock {tid}"}, "ready"),
        ("terminal", {"command": f"hermes kanban promote {tid}"}, "ready"),
        ("terminal", {"command": f"hermes kanban schedule {tid}"}, "scheduled"),
    ]


class Generated(Gate):

    def test_T2_every_move_not_in_the_table_is_refused(self):
        """Generated: every status x every agent role x every agent route. The
        permitted set is the table's agent rows; everything else in the product
        is refused, with all three assertions. A new status, role or route
        enters the product on its own."""
        import board as board_mod
        spec = board_mod.load_board(BOARD_YAML)
        os.environ["KANBAN_BOARD_FILE"] = str(BOARD_YAML)
        self.config(harness_example(), "t2.yaml")
        roles = {"coding": "coding", "qa-tester": "qa", "stranger": None}
        refused = 0
        for from_status in sorted(kb.VALID_STATUSES):
            for profile, role in roles.items():
                tid = kb.create_task(self.conn, title="t2", assignee=profile)
                with self.conn:
                    self.conn.execute("UPDATE tasks SET status = ? WHERE id = ?",
                                      (from_status, tid))
                for tool, args, lands in _agent_routes(tid):
                    if lands == from_status:
                        continue
                    frm, to = spec.column_of(from_status), spec.column_of(lands)
                    permitted = (role is not None and frm and to
                                 and spec.allows(frm, to, role) and tool == "kanban_handoff")
                    self.assertFalse(permitted)   # no non-handoff route is ever permitted
                    with self.subTest(frm=from_status, to=lands, role=role, tool=tool,
                                      args=args.get("command", "")):
                        self.assert_refused(tid, profile, tool, args,
                                            f"{from_status}->{lands} by {role} via {tool}")
                        refused += 1
        self.assertGreater(refused, 200, "the generator enumerated almost nothing")

    def test_T2_a_hand_off_the_table_does_not_allow_is_refused_and_logged(self):
        """kanban_handoff is the one agent route that can be permitted. From
        any status but running, or by a card's non-assignee, it is refused --
        and the refusal must reach the agent AND the disk like any other."""
        os.environ["KANBAN_BOARD_FILE"] = str(BOARD_YAML)
        self.config(harness_example(), "t2h.yaml")
        for from_status in ("ready", "blocked", "scheduled", "done", "triage"):
            with self.subTest(from_status):
                tid = kb.create_task(self.conn, title="t2h", assignee="coding")
                with self.conn:
                    self.conn.execute("UPDATE tasks SET status = ? WHERE id = ?",
                                      (from_status, tid))
                before_log = _LOG.read_text() if _LOG.exists() else ""
                with Worker(tid, "coding"):
                    result = json.loads(model_tools.handle_function_call(
                        "kanban_handoff", {"summary": "handing off"}))
                self.assertIn("error", result, f"hand-off from {from_status} went through")
                self.assertEqual(self.status(tid), from_status)
                written = (_LOG.read_text() if _LOG.exists() else "")[len(before_log):]
                self.assertIn("[BLOCKED]", written, f"{from_status}: refused hand-off not on disk")
                self.assertIn("tool=kanban_handoff", written)

    def test_T2_every_agent_row_of_the_table_has_a_working_route(self):
        """The other half of the product: a permitted row must be makeable, or
        the worker is left to guess -- and a guessing worker tries the next
        status. Each agent row, driven through kanban_handoff."""
        os.environ["KANBAN_BOARD_FILE"] = str(BOARD_YAML)
        self.config(harness_example(), "t2p.yaml")
        rows = {  # (role, profile, to_role, question) -> expected (status, assignee)
            ("coding", "coding", "qa", False): ("ready", "qa-tester"),        # hand-off
            ("coding", "coding", "user", True): ("blocked", "user"),          # question
            ("qa", "qa-tester", "coding", False): ("ready", "coding"),        # rework
            ("qa", "qa-tester", "user", False): ("scheduled", "user"),       # user review
            ("qa", "qa-tester", "user", True): ("blocked", "user"),          # question
            ("coding", "coding", None, False): ("ready", "qa-tester"),        # default target
            ("qa", "qa-tester", None, False): ("scheduled", "user"),         # default target
        }
        for (role, profile, to_role, question), (status, assignee) in rows.items():
            with self.subTest(role=role, to=to_role, question=question):
                tid = self.running_card(profile)
                call = {"summary": form_summary(), "question": question}
                if to_role:
                    call["to_role"] = to_role
                with Worker(tid, profile):
                    res = json.loads(model_tools.handle_function_call("kanban_handoff", call))
                self.assertTrue(res.get("ok"), f"{role} -> {to_role}: {res}")
                self.conn.close()
                self.conn = kb.connect()
                t = kb.get_task(self.conn, tid)
                self.assertEqual(t.assignee, assignee)
                if status:
                    self.assertEqual(t.status, status)


_RETRY_WORDS = ("try again", "retry later", "temporar", "later", "timeout", "timed out",
                "overloaded", "busy", "rate limit", "unavailable", "transient")


class Routes(Gate):

    # T10 -------------------------------------------------------------------
    def test_T10_every_route_to_done_is_refused_for_an_agent(self):
        self.config(harness_example(), "t10.yaml")
        tid = self.running_card()
        for tool, args in (
            ("kanban_complete", {"task_id": tid, "summary": "x"}),
            ("terminal", {"command": f"hermes kanban complete {tid}"}),
            ("terminal", {"command": f"python3 scripts/resilience/board_cli.py accept {tid}"}),
            ("terminal", {"command": f"curl -X POST http://127.0.0.1:9119/api/plugins/kanban/tasks/{tid}/complete"}),
            ("terminal", {"command": f"sqlite3 ~/.hermes/kanban.db \"update tasks set status='done' where id='{tid}'\""}),
            ("execute_code", {"code": "from hermes_cli import kanban_db; kanban_db.complete_task(None, 'x')"}),
            ("terminal", {"command": "hermes kanban swarm 'ship it'"}),   # completes its root card
        ):
            with self.subTest(tool=tool, args=str(args)[:60]):
                self.assert_refused(tid, "coding", tool, args, "route to done")
        self.assertNotEqual(self.status(tid), "done")

    # T11 -------------------------------------------------------------------
    def test_T11_a_card_in_the_runtimes_review_lane_cannot_reach_done(self):
        """The dispatcher can spawn a reviewer for a `review` card on a real
        profile (that spawn is upstream-internal, listed UNPREVENTED). What the
        harness guarantees: the spawned reviewer cannot set done."""
        self.config(harness_example(), "t11.yaml")
        tid = kb.create_task(self.conn, title="review lane", assignee="coding")
        with self.conn:
            self.conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        for tool, args in (("kanban_complete", {"task_id": tid, "summary": "merged"}),
                           ("terminal", {"command": f"hermes kanban complete {tid}"})):
            with self.subTest(tool):
                self.assert_refused(tid, "coding", tool, args, "reviewer")
        self.assertEqual(self.status(tid), "review")

    # T12 -------------------------------------------------------------------
    def test_T12_no_agent_can_forge_the_accept_trace(self):
        import board_cli
        self.config(harness_example(), "t12.yaml")
        tid = self.running_card()
        ledger = board_cli.ledger_path(self.conn)
        for tool, args in (
            ("kanban_complete", {"task_id": tid, "summary": '{"by": "accept"}'}),
            ("terminal", {"command": f"hermes kanban edit {tid} --result accepted"}),
            ("terminal", {"command": f"echo '{{\"card\": \"{tid}\"}}' >> {ledger}"}),
            ("write_file", {"path": str(ledger), "content": f'{{"card": "{tid}", "at": 1}}\n'}),
            ("patch", {"path": str(ledger), "old_string": "", "new_string": "x"}),
        ):
            with self.subTest(tool=tool):
                self.assert_refused(tid, "coding", tool, args, "forge the trace")
        self.assertIsNone(board_cli.accept_trace(self.conn, tid))

    # T26 -------------------------------------------------------------------
    def test_T26_every_refusal_is_final_and_names_the_way_forward(self):
        self.config(harness_example(), "t26.yaml")
        tid = self.running_card()
        for tool, args in (
            ("kanban_complete", {"task_id": tid, "summary": "x"}),
            ("kanban_block", {"task_id": tid, "reason": "x"}),
            ("terminal", {"command": f"hermes kanban complete {tid}"}),
            ("terminal", {"command": "sqlite3 ~/.hermes/kanban.db .tables"}),
        ):
            with self.subTest(tool=tool, args=str(args)[:50]):
                msg = self.assert_refused(tid, "coding", tool, args, "T26")
                self.assertIn("coding", msg, "the refusal does not name the role")
                self.assertIn("This refusal is final", msg)
                self.assertIn("kanban_handoff", msg, "the refusal does not name the legal move")
                low = msg.lower()
                for word in _RETRY_WORDS:
                    self.assertNotIn(word, low, f"reads as retryable: {word!r}")

    def test_T26_hand_off_and_lockdown_refusals_are_final_too(self):
        self.config(harness_example(), "t26b.yaml")
        idle = kb.create_task(self.conn, title="not running", assignee="coding")
        with Worker(idle, "coding"):
            res = json.loads(model_tools.handle_function_call(
                "kanban_handoff", {"summary": "x"}))
        self.assertIn("This refusal is final", res.get("error", ""), res)
        self.config("mode: warn\nroles: {}\n", "t26-broken.yaml")
        tid = self.running_card()
        for tool, args in (("kanban_complete", {"task_id": tid, "summary": "x"}),
                           ("terminal", {"command": f"hermes kanban complete {tid}"})):
            with self.subTest(tool):
                msg = self.assert_refused(tid, "coding", tool, args, "lockdown")
                self.assertIn("This refusal is final", msg)

    # T29 -------------------------------------------------------------------
    def test_T29_an_idempotency_key_never_hands_back_a_finished_card(self):
        self.config(harness_example(), "t29.yaml")
        done_card = kb.create_task(self.conn, title="finished", assignee="coding",
                                   idempotency_key="batch-7")
        with self.conn:
            self.conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (done_card,))
        tid = self.running_card()
        before = self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        msg = self.assert_refused(tid, "coding", "kanban_create",
                                  {"title": "again", "assignee": "coding",
                                   "idempotency_key": "batch-7"}, "reused key")
        self.assertIn(done_card, msg, "the refusal does not name the finished card")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before)
        self.assertEqual(self.status(done_card), "done")


class RouteInventory(unittest.TestCase):

    @staticmethod
    def scan(root):
        """module:function for every function in the agent that writes a
        terminal status in SQL or calls complete_task / archive_task."""
        import ast
        import re
        sql = re.compile(r"status\s*=\s*'(done|archived)'", re.I)
        found = set()
        for sub in ("hermes_cli", "tools", "gateway", "cron", "plugins"):
            base = root / sub
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.py")):
                try:
                    tree = ast.parse(path.read_text(), filename=str(path))
                except (SyntaxError, UnicodeDecodeError):
                    continue
                mod = str(path.relative_to(root))[:-3].replace("/", ".")
                for fn in ast.walk(tree):
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for node in ast.walk(fn):
                        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                                and "update tasks" in node.value.lower()
                                and sql.search(node.value)):
                            found.add(f"{mod}:{fn.name}")
                        if isinstance(node, ast.Call):
                            name = (getattr(node.func, "attr", None)
                                    or getattr(node.func, "id", None))
                            if name in ("complete_task", "archive_task") and fn.name != name:
                                found.add(f"{mod}:{fn.name}")
        return found

    def test_T20_every_route_to_a_terminal_state_is_known_and_reasoned(self):
        import yaml
        known = yaml.safe_load((REPO / "tests" / "fixtures" / "terminal-routes.yaml")
                               .read_text())["routes"]
        found = self.scan(AGENT_SRC)
        new, gone = sorted(found - set(known)), sorted(set(known) - found)
        self.assertEqual(
            (new, gone), ([], []),
            f"NEW routes into done/archived (add each with a reason, and a test "
            f"that refuses it to agents): {new}; VANISHED routes (moved or removed? "
            f"decide, then update the list): {gone}")
        for route, why in known.items():
            self.assertTrue(str(why).strip(), f"{route} has no reason")


class Audit(unittest.TestCase):
    """Moves the harness cannot prevent (the dispatcher, the user's own CLI)
    are audited: a status change whose (from, to, actor) is not in the table
    pages once -- never per tick, never for the reconciler's legal moves."""

    def setUp(self):
        self.conn = kb.connect()
        self.db = str(kb.kanban_db_path())
        self.tag = self.id().split(".")[-1]
        self.sent = []
        (Path(self.db).parent / f".cur-{self.tag}").write_text("0")

    def tearDown(self):
        self.conn.close()

    def tick(self, now):
        import board as board_mod
        import escalator
        sent = self.sent

        class Rec:
            name = "rec"

            def send(self, m):
                sent.append(m)

        escalator.run_once(self.db, board="default", config={}, apply=True, notify=True,
                           cursor_path=str(Path(self.db).parent / f".cur-{self.tag}"),
                           state_path=str(Path(self.db).parent / f".pages-{self.tag}.json"),
                           sender=Rec(), board_spec=board_mod.load_board(BOARD_YAML),
                           hermes_home=str(_HERMES), now=now)

    def move(self, tid, status, kind, payload=None, at=None):
        with self.conn:
            self.conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            self.conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, ?, ?, ?)",
                (tid, kind, json.dumps(payload) if payload else None, at or int(time.time())))

    def audits(self):
        return [m for m in self.sent if "illegal_move" in m.title]

    def test_T21_a_status_change_not_in_the_table_pages(self):
        now = int(time.time())
        tid = kb.create_task(self.conn, title="audited", assignee="coding")
        self.move(tid, "running", "claimed", at=now)
        self.tick(now)
        # the user's own terminal: running -> ready (unblock/promote) is no row
        self.move(tid, "ready", "promoted", {"by": "cli"}, at=now + 30)
        self.tick(now + 60)
        self.assertEqual(len(self.audits()), 1, [m.title for m in self.sent])
        self.assertIn("in_progress", self.audits()[0].body)
        self.assertIn("to_do", self.audits()[0].body)

    def test_T28_one_page_per_illegal_move_and_none_for_legal_ones(self):
        now = int(time.time())
        legal = kb.create_task(self.conn, title="legal", assignee="coding")
        parent = kb.create_task(self.conn, title="parent", assignee="coding")
        with self.conn:
            self.conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                              (parent, legal))
        self.move(legal, "todo", "created", at=now)
        self.move(parent, "done", "completed", {"by": "accept"}, at=now)
        illegal = kb.create_task(self.conn, title="illegal", assignee="coding")
        self.move(illegal, "running", "claimed", at=now)
        self.tick(now)
        self.move(illegal, "blocked", "blocked", {"by": "cli"}, at=now + 10)
        for i in range(1, 61):                   # 60 ticks; the reconciler moves `legal`
            self.tick(now + 60 * i)
        titles = [m.title for m in self.audits()]
        self.assertEqual(len(titles), 1, titles)
        self.assertIn("illegal", titles[0])
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                           (legal,)).fetchone()[0], "ready",
                         "the reconciler's auto_advance should have moved it")


class InstallCheck(unittest.TestCase):
    """A profile that can reach kanban tools must have THE gated harness loaded.
    Listed-but-not-linked is exactly how the harness was absent for a whole run."""

    def make_profile(self, home, name, *, kanban=True, listed=True, link=None):
        import yaml
        p = home / "profiles" / name
        (p / "plugins").mkdir(parents=True, exist_ok=True)
        cfg = {"toolsets": ["hermes-cli"],
               "platform_toolsets": {"cli": ["file"] + (["kanban"] if kanban else [])},
               "plugins": {"enabled": (["kanban-harness"] if listed else []) + ["other"]}}
        (p / "config.yaml").write_text(yaml.safe_dump(cfg))
        if link is not None:
            (p / "plugins" / "kanban-harness").symlink_to(link)

    def test_T19_T27_every_kanban_profile_has_the_gated_harness_loaded(self):
        import shutil
        import escalator
        home = Path(tempfile.mkdtemp(prefix="install-", dir=_SCRATCH))
        canonical = PLUGIN_DIR.resolve()
        copy = home / "a-copy-of-the-harness"
        shutil.copytree(canonical, copy)
        self.make_profile(home, "good", link=canonical)
        self.make_profile(home, "listed-not-linked")
        self.make_profile(home, "not-listed", listed=False, link=canonical)
        self.make_profile(home, "kanban-no-harness", listed=False)
        self.make_profile(home, "linked-to-a-copy", link=copy)
        self.make_profile(home, "reads-only", kanban=False, listed=False)
        faults = {f["profile"]: f["why"] for f in escalator.harness_install_faults(str(home))}
        self.assertEqual(sorted(faults), ["kanban-no-harness", "linked-to-a-copy",
                                          "listed-not-linked", "not-listed"], faults)
        self.assertIn("not linked", faults["listed-not-linked"])
        self.assertIn("not the gated harness", faults["linked-to-a-copy"])

    def test_T19_the_escalator_pages_a_profile_without_the_harness_as_a_fault(self):
        import escalator
        home = Path(tempfile.mkdtemp(prefix="install-page-", dir=_SCRATCH))
        self.make_profile(home, "private", listed=False)
        conn = kb.connect()
        try:
            db = str(kb.kanban_db_path())
        finally:
            conn.close()
        cur = Path(db).parent / ".cur-t19"
        cur.write_text("0")
        sent = []

        class Rec:
            name = "rec"

            def send(self, m):
                sent.append(m)

        escalator.run_once(db, board="default", config={}, apply=False, notify=True,
                           cursor_path=str(cur), sender=Rec(),
                           state_path=str(Path(db).parent / ".pages-t19.json"),
                           hermes_home=str(home))
        self.assertTrue([m for m in sent if "harness_not_loaded" in m.title],
                        "a profile with kanban tools and no harness was not paged")

    def test_T19_a_fault_that_has_cleared_is_closed_on_the_next_tick(self):
        """A page whose condition has cleared must close, visibly, on the next
        tick -- or it repeats every six hours for a fixed profile and the
        alarm becomes noise that gets muted."""
        import yaml
        import escalator
        home = Path(tempfile.mkdtemp(prefix="install-close-", dir=_SCRATCH))
        self.make_profile(home, "private", listed=False)
        conn = kb.connect()
        try:
            db = str(kb.kanban_db_path())
        finally:
            conn.close()
        cur = Path(db).parent / ".cur-t19c"
        cur.write_text("0")
        state = str(Path(db).parent / ".pages-t19c.json")
        sent = []

        class Rec:
            name = "rec"

            def send(self, m):
                sent.append(m)

        def tick(at):
            before = len(sent)
            escalator.run_once(db, board="default", config={}, apply=False, notify=True,
                               cursor_path=str(cur), sender=Rec(), state_path=state,
                               hermes_home=str(home), now=at)
            return sent[before:]

        t0 = int(time.time())
        first = tick(t0)
        self.assertEqual(len([m for m in first if "harness_not_loaded" in m.title]), 1)
        # fix the profile: listed and linked to the gated harness
        cfg_path = home / "profiles" / "private" / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text())
        cfg["plugins"]["enabled"].append("kanban-harness")
        cfg_path.write_text(yaml.safe_dump(cfg))
        (home / "profiles" / "private" / "plugins" / "kanban-harness").symlink_to(
            PLUGIN_DIR.resolve())
        self.assertEqual(escalator.harness_install_faults(str(home)), [])
        second = tick(t0 + 60)
        closed = [m for m in second if "resolved" in m.title and "profile:private" in
                  (m.title + m.body)]
        self.assertEqual(len(closed), 1, f"the cleared fault was not closed: "
                                         f"{[m.title for m in second]}")
        third = tick(t0 + 7 * 3600)
        self.assertEqual([m.title for m in third if "private" in m.title + m.body], [],
                         "a closed fault paged again")

    def test_T27_the_launchd_copy_of_the_escalator_finds_the_gated_harness(self):
        """launchd runs a COPY of the escalator outside the checkout. Located
        from its own file, the copy looked for the harness in the wrong place and
        called every correctly linked profile faulted."""
        import importlib.util
        import shutil
        bin_dir = Path(tempfile.mkdtemp(prefix="launchd-bin-", dir=_SCRATCH))
        for name in ("escalator.py", "escalation.py", "board.py", "board_cli.py",
                     "wait_budget.py", "error_policy.py", "failure_policy.py"):
            src = REPO / "scripts" / "resilience" / name
            if src.exists():
                shutil.copy(src, bin_dir / name)
        home = Path(tempfile.mkdtemp(prefix="install-copy-", dir=_SCRATCH))
        self.make_profile(home, "good", link=PLUGIN_DIR.resolve())
        saved = os.environ.get("HERMES_WORKFLOWS_REPO")
        sys.path.insert(0, str(bin_dir))
        try:
            os.environ["HERMES_WORKFLOWS_REPO"] = str(REPO)
            spec = importlib.util.spec_from_file_location("escalator_launchd_copy",
                                                          bin_dir / "escalator.py")
            copy = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(copy)
            self.assertEqual(copy.harness_install_faults(str(home)), [])
            os.environ.pop("HERMES_WORKFLOWS_REPO")
            faults = copy.harness_install_faults(str(home))
            self.assertTrue(faults and "HERMES_WORKFLOWS_REPO" in faults[0]["why"],
                            f"a copy that cannot locate the harness must say so: {faults}")
        finally:
            sys.path.remove(str(bin_dir))
            if saved is None:
                os.environ.pop("HERMES_WORKFLOWS_REPO", None)
            else:
                os.environ["HERMES_WORKFLOWS_REPO"] = saved


class TheGateRunsEveryLayer(unittest.TestCase):

    def test_T35_a_layer_that_exits_early_fails_the_gate(self):
        """A sourced layer that calls `exit 0` ended `test.sh smoke` green after
        one check. Build the real runner with fake layers, one of which exits
        early; the gate must fail, and say why."""
        import shutil
        import subprocess
        tree = Path(tempfile.mkdtemp(prefix="gate-", dir=_SCRATCH))
        (tree / "scripts").mkdir()
        (tree / "tests" / "lib").mkdir(parents=True)
        (tree / "tests" / "smoke").mkdir()
        (tree / "tests" / "static").mkdir()
        (tree / "tests" / "e2e").mkdir()
        shutil.copy(REPO / "scripts" / "test.sh", tree / "scripts" / "test.sh")
        shutil.copy(REPO / "tests" / "lib" / "common.sh", tree / "tests" / "lib" / "common.sh")
        ok = 'pass "fake layer ran"\n'
        for name in ("bridge", "dispatcher", "kanban-harness", "resilience",
                     "board", "harness-contract", "claude-code-bridge"):
            (tree / "tests" / "smoke" / f"{name}.sh").write_text(ok)
        # the LLM layer runs in its own process, so it brings its own helpers
        (tree / "tests" / "smoke" / "llm.sh").write_text(
            '. "$(dirname "$0")/../lib/common.sh"\npass "llm"\n')
        (tree / "tests" / "smoke" / "bridge.sh").write_text('pass "bridge"\nexit 0\n')
        out = subprocess.run(["bash", str(tree / "scripts" / "test.sh"), "smoke"],
                             capture_output=True, text=True, timeout=60)
        self.assertNotEqual(out.returncode, 0,
                            "a layer that exited early still let the gate pass:\n" + out.stdout)
        self.assertIn("did not finish", out.stdout + out.stderr)
        # and with every layer finishing normally, the same runner passes
        (tree / "tests" / "smoke" / "bridge.sh").write_text(ok)
        out = subprocess.run(["bash", str(tree / "scripts" / "test.sh"), "smoke"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


class ProfileIsNotARole(Gate):
    """T37: a role token given a profile name. The refusal, the load failure and
    the validation error stand exactly as they are; when the value is a known
    profile the message names it, says it is not a role, and gives the literal
    correction. The profile is never resolved to its role, and the hint never
    changes the outcome, only the message."""

    HINT = "is a profile, not a role"

    def worker_harness(self):
        """The shipped harness with its worker profile renamed to coding-worker.
        The profile must differ from the role name coding, or the profile and the
        role are the same token and the refusal under test cannot be told apart."""
        text = harness_example()
        old = "  coding:\n    profiles: [coding]\n"
        self.assertIn(old, text)
        return text.replace(old, "  coding:\n    profiles: [coding-worker]\n", 1)

    def handoff_to(self, to_role):
        os.environ["KANBAN_BOARD_FILE"] = str(BOARD_YAML)
        self.config(self.worker_harness(), "t37.yaml")
        tid = self.running_card("coding-worker")
        before_log = _LOG.read_text() if _LOG.exists() else ""
        with Worker(tid, "coding-worker"):
            res = json.loads(model_tools.handle_function_call(
                "kanban_handoff", {"summary": form_summary(), "to_role": to_role}))
        written = (_LOG.read_text() if _LOG.exists() else "")[len(before_log):]
        return tid, res, written

    def assert_handoff_refused(self, tid, res, written):
        self.assertIn("error", res, f"the hand-off went through: {res}")
        self.conn.close()
        self.conn = kb.connect()
        card = kb.get_task(self.conn, tid)
        self.assertEqual(card.status, "running", "the board moved")
        self.assertEqual(card.assignee, "coding-worker", "the assignee changed")
        self.assertIn("[BLOCKED]", written, "nothing on disk")
        self.assertIn("tool=kanban_handoff", written)
        return res["error"]

    # R1 --------------------------------------------------------------------
    def test_T37_R1_a_profile_as_to_role_is_refused_and_named(self):
        msg = self.assert_handoff_refused(*self.handoff_to("qa-tester"))
        self.assertIn("'qa-tester' " + self.HINT, msg)
        self.assertIn("Pass to_role='qa'", msg)

    # R2 --------------------------------------------------------------------
    def test_T37_R2_a_value_that_is_neither_gets_no_hint(self):
        msg = self.assert_handoff_refused(*self.handoff_to("nonsense"))
        self.assertNotIn(self.HINT, msg)
        self.assertNotIn("Pass to_role=", msg)

    # R3 --------------------------------------------------------------------
    def test_T37_R3_the_role_name_itself_hands_off(self):
        tid, res, _ = self.handoff_to("qa")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(self.status(tid), "ready")

    # R4 --------------------------------------------------------------------
    def test_T37_R4_the_hinted_refusal_is_final(self):
        msg = self.assert_handoff_refused(*self.handoff_to("qa-tester"))
        self.assertIn("This refusal is final", msg)
        low = msg.lower()
        for word in _RETRY_WORDS:
            self.assertNotIn(word, low, f"reads as retryable: {word!r}")

    # R5 / R7 (harness.yaml) --------------------------------------------------
    def load_error(self, text, name):
        self.config(text, name)
        os.environ.pop("KANBAN_BOARD_FILE", None)
        with self.assertRaises(harness.HarnessConfigError) as caught:
            harness.load_config()
        return str(caught.exception)

    def test_T37_R5_a_profile_in_harness_yaml_fails_the_load_with_the_fix(self):
        for key, old, new, role in (
            ("to_role", "action: handoff, to_role: qa,", "action: handoff, to_role: qa-tester,", "qa"),
            ("from_role", "{from_role: coding, action: handoff",
             "{from_role: coding-worker, action: handoff", "coding"),
        ):
            with self.subTest(key):
                text = self.worker_harness()
                self.assertIn(old, text)
                bad = text.replace(old, new, 1)
                err = self.load_error(bad, f"t37-{key}.yaml")
                self.assertIn(self.HINT, err)
                self.assertIn(f"In harness.yaml, write {key}: {role}", err)
                tid = self.running_card()
                self.assert_refused(tid, "coding-worker", "kanban_complete",
                                    {"task_id": tid, "summary": "x"}, f"R5 {key}")

    def test_T39_a_harness_role_the_board_does_not_declare_fails_the_load(self):
        """Two files describe the roles. With a board deployed, a role the
        harness grants moves to must be declared in the board's roles, or the
        load fails naming it: nobody can satisfy one file and be refused by
        the other."""
        os.environ["KANBAN_BOARD_FILE"] = str(BOARD_YAML)
        text = harness_example().replace(
            "roles:\n", "roles:\n  somebody:\n    profiles: [somebody-profile]\n", 1)
        text = text.replace("transitions:\n",
                            "transitions:\n  - {from_role: somebody, action: link}\n", 1)
        self.config(text, "t39.yaml")
        with self.assertRaises(harness.HarnessConfigError) as caught:
            harness.load_config()
        self.assertIn("'somebody'", str(caught.exception))
        self.assertIn("roles", str(caught.exception))

    def test_T37_R7_harness_yaml_value_that_is_neither_gets_no_hint(self):
        bad = harness_example().replace("action: handoff, to_role: qa,",
                                        "action: handoff, to_role: nonsense,", 1)
        err = self.load_error(bad, "t37-neither.yaml")
        self.assertNotIn(self.HINT, err)
        self.assertNotIn("In harness.yaml, write", err)

    # R6 / R7 / R8 (board.yaml) ------------------------------------------------
    def bad_board(self, by):
        import board as board_mod
        text = BOARD_YAML.read_text()
        old = "by: coding,     when: hand-off to qa"
        self.assertIn(old, text)
        path = _SCRATCH / f"t37-board-{by}.yaml"
        path.write_text(text.replace(old, f"by: {by},     when: hand-off to qa", 1))
        return board_mod, board_mod.load_board(path)

    def profile_role(self):
        return {"coding-worker": "coding", "qa-tester": "qa"}

    def test_T37_R6_a_profile_in_board_yaml_by_names_its_role(self):
        board_mod, spec = self.bad_board("coding-worker")
        issues = [i for i in board_mod.validate(spec, profile_role=self.profile_role())
                  if i.code == "move-unknown-actor"]
        self.assertTrue(issues, "a profile in by: was accepted")
        self.assertIn("'coding-worker' " + self.HINT, issues[0].message)
        self.assertIn("In board.yaml, write by: coding", issues[0].message)

    def test_T37_R7_board_yaml_value_that_is_neither_gets_no_hint(self):
        board_mod, spec = self.bad_board("nonsense")
        msgs = [i.message for i in board_mod.validate(spec, profile_role=self.profile_role())
                if i.code == "move-unknown-actor"]
        self.assertTrue(msgs)
        self.assertFalse(any(self.HINT in m for m in msgs), msgs)

    def test_T37_R8_the_hint_never_changes_the_verdict(self):
        board_mod, spec = self.bad_board("coding-worker")
        without = board_mod.validate(spec)
        with_map = board_mod.validate(spec, profile_role=self.profile_role())
        self.assertEqual(sorted((i.severity, i.code) for i in without),
                         sorted((i.severity, i.code) for i in with_map))
        self.assertNotEqual([i.message for i in without], [i.message for i in with_map],
                            "the matrix made no difference to the words")


#: The generator of the agent patches. The tests build them from the
#: installed checkout's own upstream (`git show HEAD:<path>`), so the suite
#: tests this repo's claim, not a file someone copied. HERMES_PATCH_DIR
#: overrides that with an already built set (the install step).
PATCH_BUILD = REPO / "scripts" / "harness-patches" / "build.py"


def _load_patch_build():
    spec = importlib.util.spec_from_file_location("harness_patch_build", PATCH_BUILD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _upstream_files(dest):
    import subprocess
    for rel in _load_patch_build().PINNED:
        out = subprocess.run(["git", "-C", str(AGENT_SRC), "show", f"HEAD:{rel}"],
                             capture_output=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(f"cannot read upstream {rel} from {AGENT_SRC}: {out.stderr}")
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(out.stdout)
    return dest


def _built_patches():
    env = os.environ.get("HERMES_PATCH_DIR", "").strip()
    if env:
        return Path(os.path.expanduser(env)).resolve()
    out = _SCRATCH / "built-patches"
    if not out.exists():
        _load_patch_build().build(_upstream_files(_SCRATCH / "upstream"), out)
    return out

#: Each contract row that an agent patch closes, the file that closes it and
#: the marker the patch carries.
_PATCHES = {
    "Upstream internals and crons": ("hermes_cli/kanban_db.py",
                                     "kanban-harness-patch: terminal-writes"),
    "A profile where the harness isn't loaded": ("tools/kanban_tools.py",
                                                 "kanban-harness-patch: fail-closed"),
}
_READ_ONLY_TOOLS = {"kanban_show", "kanban_list"}


def _upstream_sha(text):
    import re
    m = re.search(r"^# upstream-sha256: ([0-9a-f]{64})$", text, re.M)
    return m.group(1) if m else None


def _installed_upstream(rel):
    import hashlib
    import subprocess
    out = subprocess.run(["git", "-C", str(AGENT_SRC), "show", f"HEAD:{rel}"],
                         capture_output=True, timeout=30)
    return hashlib.sha256(out.stdout).hexdigest() if out.returncode == 0 else None


def contract_and_install_disagree(contract_text, agent_src=None, patch_dir=None):
    """Every way the contract's route table and the installed agent disagree.

    A row that says UNPREVENTED must not be patched; a row that claims anything
    else must be installed as a link to a patch whose bytes are exactly what the
    generator builds from the installed upstream. A row that cannot be found is
    itself a disagreement."""
    agent_src = agent_src or AGENT_SRC
    patch_dir = patch_dir or _built_patches()
    problems = []
    rows = [l for l in contract_text.splitlines() if l.startswith("| ")]
    for row_start, (rel, marker) in _PATCHES.items():
        row = next((l for l in rows if l[2:].startswith(row_start)), None)
        if row is None:
            problems.append(f"the contract has no row {row_start!r}; a renamed row "
                            "must not turn this check off")
            continue
        patch = patch_dir / rel
        if not patch.is_file():
            problems.append(f"{row_start}: the patch {patch} does not exist")
            continue
        ptext = patch.read_text()
        if marker not in ptext or not _upstream_sha(ptext):
            problems.append(f"{row_start}: {patch} lacks its marker or upstream-sha256")
        installed = agent_src / rel
        linked = installed.is_symlink() and installed.is_file() \
            and installed.read_bytes() == patch.read_bytes()
        carries = installed.is_file() and marker in installed.read_text()
        status = row.split("|")[2]
        if "UNPREVENTED" in status:
            if linked or carries:
                problems.append(f"the contract says {row_start!r} is UNPREVENTED, but "
                                f"{installed} is patched: correct the row")
        else:
            if not linked:
                problems.append(f"the contract claims {row_start!r} is prevented, but "
                                f"{installed} is not a link to the generated patch "
                                "(scripts/harness-patches/build.py)")
            if not carries:
                problems.append(f"{row_start}: the installed {rel} carries no {marker!r}")
            if _upstream_sha(ptext) != _installed_upstream(rel):
                problems.append(f"{row_start}: the patch was written against a different "
                                f"upstream than the installed {rel}: re-derive it before "
                                "claiming prevention")
    return problems


def _patched_tree():
    """The installed agent with both patch files swapped in, as links."""
    tree = _SCRATCH / "patched-agent"
    if tree.exists():
        return tree
    tree.mkdir()
    swapped = {rel for rel, _ in _PATCHES.values()}
    for entry in AGENT_SRC.iterdir():
        if entry.name in ("hermes_cli", "tools"):
            (tree / entry.name).mkdir()
            for f in entry.iterdir():
                rel = f"{entry.name}/{f.name}"
                (tree / rel).symlink_to(_built_patches() / rel if rel in swapped else f)
        else:
            (tree / entry.name).symlink_to(entry)
    return tree


def _run_patched(script, **env):
    import subprocess
    home = _SCRATCH / f"t33-{time.time_ns()}"
    (home / ".hermes" / "logs").mkdir(parents=True)
    full = {k: v for k, v in os.environ.items()
            if not k.startswith(("HERMES_", "KANBAN_"))}
    full.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"),
                PYTHONPATH=os.pathsep.join([str(_patched_tree()),
                                            str(REPO / "scripts" / "resilience")]))
    full.update({k: str(v) for k, v in env.items()})
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         timeout=120, env=full, cwd=str(home))
    log = home / ".hermes" / "logs" / "kanban-harness.log"
    return out, (log.read_text() if log.exists() else ""), home


_T33A = r'''
import json, os, sys
from hermes_cli import kanban_db as kb
conn = kb.connect()
res = {"module": kb.__file__}
def card(status):
    tid = kb.create_task(conn, title="t33a", assignee="coding")
    with conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    return tid
def now(tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
for fn, statuses in (("complete_task", sys.argv[1].split(",")),
                     ("archive_task", sys.argv[2].split(","))):
    for s in statuses:
        tid = card(s)
        try:
            ok = getattr(kb, fn)(conn, tid)
            err = None
        except Exception as exc:
            ok, err = None, f"{type(exc).__name__}: {exc}"
        res[f"{fn}:{s}"] = {"ok": ok, "error": err, "status": now(tid)}
print(json.dumps(res))
'''

_T33A_DASHBOARD = r'''
import json, os, sys, types
sys.path.insert(0, os.environ["DASHBOARD_DIR"])
# The dashboard module registers an /attachments route needing python-multipart
# (an unrelated upload endpoint we never call here). A local shim satisfies
# FastAPI's import-time check without pulling a real dependency into this test.
try:
    import python_multipart  # noqa: F401
except ImportError:
    shim = types.ModuleType("python_multipart")
    shim.__version__ = "999"
    sys.modules["python_multipart"] = shim
from hermes_cli import kanban_db as kb
import plugin_api
conn = kb.connect()
def card(status):
    tid = kb.create_task(conn, title="dash", assignee="coding")
    with conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    return tid
def now(tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
res = {"module": plugin_api.__file__}
running, ready = card("running"), card("ready")
for key, tid, body in (
    ("done", running, plugin_api.UpdateTaskBody(status="done")),
    ("archived", ready, plugin_api.UpdateTaskBody(status="archived")),
):
    try:
        plugin_api.update_task(tid, body, board=None)
        res[key] = {"ok": True, "error": None, "status": now(tid)}
    except Exception as exc:
        res[key] = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "status": now(tid)}
print(json.dumps(res))
'''


class AgentPatches(unittest.TestCase):
    """T33: the two agent patches for the rows the contract marks UNPREVENTED,
    tested against the installed agent with the patch files swapped in. They
    are not linked into the install until /hermes-e2e passes; T33c keeps the
    contract's words and the install in step."""

    def complete_and_archive(self, spec, complete, archive, **extra_env):
        env = {} if spec is None else {"KANBAN_BOARD_FILE": spec}
        env.update(extra_env)
        script = _T33A.replace("sys.argv[1]", repr(",".join(complete))) \
                      .replace("sys.argv[2]", repr(",".join(archive)))
        out, log, _ = _run_patched(script, **env)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertTrue(str(res.pop("module")).startswith(str(_SCRATCH)),
                        "the scratch agent tree was not the one imported")
        return res, log

    #: an agent caller for the gate: an inside-the-runtime identity, not a
    #: person at a terminal or the dashboard (neither sets these).
    AGENT_ENV = {"HERMES_KANBAN_TASK": "t_x", "HERMES_PROFILE": "coding"}

    # T33a ------------------------------------------------------------------
    def test_T33a_with_a_spec_deployed_no_terminal_write_leaves_the_table(self):
        res, log = self.complete_and_archive(
            str(BOARD_YAML), ["running", "ready", "blocked"],
            ["running", "ready", "scheduled", "triage", "blocked", "done"],
            **self.AGENT_ENV)
        for s in ("running", "ready", "blocked"):
            r = res[f"complete_task:{s}"]
            self.assertIsNotNone(r["error"], f"complete_task from {s} went through: {r}")
            self.assertIn("board_cli", r["error"], "the refusal does not name accept")
            self.assertEqual(r["status"], s, f"complete_task from {s} moved the card")
        for s in ("running", "ready", "scheduled"):
            r = res[f"archive_task:{s}"]
            self.assertIsNotNone(r["error"], f"archive_task from {s} went through: {r}")
            self.assertEqual(r["status"], s)
        for s in ("triage", "blocked", "done"):
            r = res[f"archive_task:{s}"]
            self.assertTrue(r["ok"], f"archive_task from {s}, a table row, refused: {r}")
            self.assertEqual(r["status"], "archived")
        self.assertEqual(log.count("[BLOCKED]"), 6, log)
        self.assertIn("tool=complete_task", log)
        self.assertIn("tool=archive_task", log)

    def test_T33a_a_broken_spec_refuses_both(self):
        for name, text in (("missing", None), ("invalid", "board: [unclosed\n"),
                           ("empty", "")):
            with self.subTest(name):
                path = _SCRATCH / f"t33a-{name}.yaml"
                if text is not None:
                    path.write_text(text)
                res, log = self.complete_and_archive(str(path), ["running"], ["done"],
                                                     **self.AGENT_ENV)
                for key in ("complete_task:running", "archive_task:done"):
                    self.assertIsNotNone(res[key]["error"], f"{name}: {key} went through")
                self.assertEqual(res["complete_task:running"]["status"], "running")
                self.assertEqual(res["archive_task:done"]["status"], "done")
                self.assertEqual(log.count("[BLOCKED]"), 2, log)

    def test_T33a_the_protocol_message_names_the_exit_the_board_allows(self):
        """On a board with a spec, kanban_complete is refused to every agent, so
        a protocol message telling a worker to call it points at a refusal. It
        names kanban_handoff there; without a spec, upstream's words stand."""
        script = ("from hermes_cli import kanban_db as kb\n"
                  "print(kb._protocol_violation_text())\n")
        out, _, _ = _run_patched(script, KANBAN_BOARD_FILE=BOARD_YAML)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn("kanban_handoff", out.stdout)
        self.assertNotIn("kanban_complete", out.stdout)
        out, _, _ = _run_patched(script)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn("kanban_complete or kanban_block", out.stdout)

    def test_T33a_with_no_spec_deployed_upstream_is_unchanged(self):
        res, log = self.complete_and_archive(None, ["running"], ["running"])
        self.assertTrue(res["complete_task:running"]["ok"], res)
        self.assertEqual(res["complete_task:running"]["status"], "done")
        self.assertTrue(res["archive_task:running"]["ok"], res)
        self.assertEqual(log, "")

    def test_T33a_the_two_writers_we_own_still_work_under_the_patch(self):
        script = r'''
import json, time
from hermes_cli import kanban_db as kb
import board_cli, escalator
conn = kb.connect()
a = kb.create_task(conn, title="accept me", assignee="user")
d = kb.create_task(conn, title="old done", assignee="coding")
with conn:
    conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (a,))
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (d,))
board_cli.ledger_path(conn).touch()
board_cli.accept(conn, a)
escalator._archive(conn, d, 0, int(time.time()))
print(json.dumps({r[0]: r[1] for r in conn.execute("SELECT id, status FROM tasks")}))
'''
        out, _, _ = _run_patched(script, KANBAN_BOARD_FILE=BOARD_YAML)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(sorted(json.loads(out.stdout.strip().splitlines()[-1]).values()),
                         ["archived", "done"])

    def test_T33a_the_user_moves_cards_through_the_web_dashboard(self):
        """Drives the dashboard's real write path: the installed
        plugins/kanban/dashboard/plugin_api.py:update_task, the FastAPI route
        handler for `PATCH /tasks/{id}`, called directly with the same
        UpdateTaskBody a browser PATCH builds. update_task calls
        kanban_db.complete_task for status='done' and kanban_db.archive_task
        for status='archived' -- the same two primitives _terminal_write_gate
        wraps -- so calling it here is the dashboard's own code path, not a
        stand-in for it.

        A plain user (no HERMES_KANBAN_TASK/HERMES_PROFILE, the dashboard
        server's own identity) must go through on both moves, with nothing
        logged as blocked. The same calls under an agent identity are
        refused and leave [BLOCKED] on disk."""
        dashboard_dir = str(_patched_tree() / "plugins" / "kanban" / "dashboard")
        out, log, _ = _run_patched(_T33A_DASHBOARD, DASHBOARD_DIR=dashboard_dir,
                                   KANBAN_BOARD_FILE=str(BOARD_YAML))
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertTrue(res["done"]["ok"], f"the user's move to done was refused: {res['done']}")
        self.assertEqual(res["done"]["status"], "done")
        self.assertTrue(res["archived"]["ok"],
                        f"the user's move to archived was refused: {res['archived']}")
        self.assertEqual(res["archived"]["status"], "archived")
        self.assertNotIn("[BLOCKED]", log, f"the user's own dashboard move was logged blocked: {log}")

        out2, log2, _ = _run_patched(_T33A_DASHBOARD, DASHBOARD_DIR=dashboard_dir,
                                     KANBAN_BOARD_FILE=str(BOARD_YAML), **self.AGENT_ENV)
        self.assertEqual(out2.returncode, 0, out2.stderr[-2000:])
        res2 = json.loads(out2.stdout.strip().splitlines()[-1])
        self.assertFalse(res2["done"]["ok"], res2["done"])
        self.assertIn("board_cli", res2["done"]["error"], "the refusal does not name accept")
        self.assertEqual(res2["done"]["status"], "running", "the card moved")
        self.assertFalse(res2["archived"]["ok"], res2["archived"])
        self.assertEqual(res2["archived"]["status"], "ready", "the card moved")
        self.assertIn("[BLOCKED]", log2)

    def test_T33a_every_inventoried_route_passes_through_a_refused_function(self):
        """Addition 1: the inventory (T20) and the patch must cover the same
        ground. A route that neither is a patched primitive nor calls one is
        a hole between two green tests."""
        import ast
        import yaml
        known = yaml.safe_load((REPO / "tests" / "fixtures" / "terminal-routes.yaml")
                               .read_text())["routes"]
        primitives = {"complete_task", "archive_task"}
        for route in known:
            mod, fn = route.split(":")
            with self.subTest(route):
                if mod == "hermes_cli.kanban_db" and fn in primitives:
                    continue
                path = AGENT_SRC / (mod.replace(".", "/") + ".py")
                tree = ast.parse(path.read_text())
                func = next((n for n in ast.walk(tree)
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and n.name == fn), None)
                self.assertIsNotNone(func, f"{route} not found in the installed agent")
                calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                         for n in ast.walk(func) if isinstance(n, ast.Call)}
                self.assertTrue(calls & primitives,
                                f"{route} reaches a terminal state without complete_task or "
                                "archive_task, so the terminal-write patch does not cover it")

    # T33b ------------------------------------------------------------------
    @staticmethod
    def kanban_tools_in_the_agent():
        """Addition 2: every kanban tool the installed agent registers, found
        by scanning, not by trusting a list."""
        import ast
        names = set()
        for path in sorted((AGENT_SRC / "tools").rglob("*.py")):
            try:
                tree = ast.parse(path.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "register":
                    kw = {k.arg: k.value for k in n.keywords}
                    name, ts = kw.get("name"), kw.get("toolset")
                    if (isinstance(name, ast.Constant) and isinstance(ts, ast.Constant)
                            and ts.value == "kanban"):
                        names.add(name.value)
        return names

    def test_T33b_without_the_harness_every_kanban_write_is_refused(self):
        tools = self.kanban_tools_in_the_agent()
        self.assertTrue({"kanban_complete", "kanban_show"} <= tools, tools)
        script = r'''
import json
from hermes_cli import kanban_db as kb
conn = kb.connect()
tid = kb.create_task(conn, title="t33b", assignee="coding")
kb.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: None)
import os
os.environ["HERMES_KANBAN_TASK"] = tid
import model_tools
res = {"card": tid, "before": kb.get_task(conn, tid).status}
args = {"task_id": tid, "summary": "x", "reason": "x", "title": "x", "assignee": "coding",
        "body": "x", "parent_id": tid, "child_id": tid, "note": "x"}
for name in TOOLS:
    res[name] = json.loads(model_tools.handle_function_call(name, dict(args)))
conn.close()
conn = kb.connect()
res["after"] = kb.get_task(conn, tid).status
res["cards"] = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
res["comments"] = conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
print(json.dumps(res))
'''.replace("TOOLS", repr(sorted(tools)))
        out, log, _ = _run_patched(script, HERMES_PROFILE="coding")
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(res["after"], res["before"], "the board moved")
        self.assertEqual(res["cards"], 1, "a card was created")
        self.assertEqual(res["comments"], 0, "a comment was written")
        for name in sorted(tools - _READ_ONLY_TOOLS):
            with self.subTest(name):
                self.assertIn("error", res[name], f"{name} ran without the harness: {res[name]}")
                self.assertIn("kanban-harness is not loaded", res[name]["error"])
                self.assertIn(f"tool={name}", log, f"{name}: refusal not on disk")
        for name in sorted(_READ_ONLY_TOOLS & tools):
            with self.subTest(name):
                self.assertNotIn("kanban-harness is not loaded", json.dumps(res[name]))
        self.assertIn("kanban-harness is not loaded", log)

    # the generator -----------------------------------------------------------
    def test_T33_the_generator_refuses_an_unpinned_upstream(self):
        mod = _load_patch_build()
        up = _upstream_files(_SCRATCH / "upstream-drifted")
        path = up / "tools" / "kanban_tools.py"
        path.write_bytes(path.read_bytes() + b"\n# a newer upstream\n")
        with self.assertRaises(mod.UpstreamMismatch) as caught:
            mod.build(up, _SCRATCH / "never-built")
        msg = str(caught.exception)
        self.assertIn(mod.PINNED["tools/kanban_tools.py"], msg)
        self.assertIn(__import__("hashlib").sha256(path.read_bytes()).hexdigest(), msg)
        self.assertFalse((_SCRATCH / "never-built" / "tools" / "kanban_tools.py").exists())

    def test_T33_the_generator_is_deterministic(self):
        mod = _load_patch_build()
        up = _upstream_files(_SCRATCH / "upstream-twice")
        a, b = mod.build(up, _SCRATCH / "build-a"), mod.build(up, _SCRATCH / "build-b")
        for rel in mod.PINNED:
            with self.subTest(rel):
                self.assertEqual((a / rel).read_bytes(), (b / rel).read_bytes())

    # T33c ------------------------------------------------------------------
    def test_T33c_the_contract_and_the_install_agree(self):
        self.assertEqual(contract_and_install_disagree(CONTRACT_DOC.read_text()), [])

    def test_T33c_a_row_claiming_prevention_without_the_patch_fails(self):
        text = CONTRACT_DOC.read_text()
        claimed = "\n".join(
            l.replace("**UNPREVENTED**", "**PREVENTED**")
            if l.startswith("| Upstream internals and crons") else l
            for l in text.splitlines())
        problems = contract_and_install_disagree(claimed)
        self.assertTrue(any("claims 'Upstream internals and crons' is prevented" in p
                            for p in problems), problems)

    def test_T33c_a_renamed_row_fails(self):
        text = CONTRACT_DOC.read_text().replace("| A profile where the harness isn't loaded",
                                                "| Some other wording")
        problems = contract_and_install_disagree(text)
        self.assertTrue(any("has no row" in p for p in problems), problems)


_FORM_FIELDS = ["Done", "Method", "Files", "Result", "Could not check", "Review first",
                "Doubts"]


def form_summary(files="none", could="none", drop=None):
    vals = {"Done": "implemented the parser", "Method": "ran the unit tests",
            "Files": files, "Result": "12 passed", "Could not check": could,
            "Review first": "the edge case in parse()", "Doubts": "none"}
    return "\n".join(f"{k}: {v}" for k, v in vals.items() if k != drop)


# Synthetic fixtures (T47): invented ids and values, no real record.
_REAL_CODE = ('"""Parse one example row into the fields the check compares."""\n'
              "def parse(row):\n    return {k.strip(): v.strip() for k, v in row.items()}\n")

_REAL_FINDINGS = """# Findings example-0001

| field | before | after | verdict |
|---|---|---|---|
| title | Widget One | Widget One | match |
| price | 10.00 | 12.00 | differs |
"""


class HandoffFormGate(Gate):
    """The fixture of T38 and T47: a board whose spec carries `handoff:`, a
    worker with a workspace, and the D11 assertions on a refused hand-off."""

    FORM = "~/.hermes/HANDOVER.md"

    def setUp(self):
        super().setUp()
        self.soul = _HERMES / "SOUL.md"
        self._saved_ws = os.environ.pop("HERMES_KANBAN_WORKSPACE", None)
        self.config(harness_example(), "t38.yaml")
        self.board(with_form=True)
        # Every dispatcher worker has a workspace; a path on the Files line
        # must resolve inside it (T47).
        self.ws = Path(tempfile.mkdtemp(prefix="t38-plain-ws-", dir=_SCRATCH))
        os.environ["HERMES_KANBAN_WORKSPACE"] = str(self.ws)

    def tearDown(self):
        if self.soul.exists():
            self.soul.unlink()
        os.environ.pop("HERMES_KANBAN_WORKSPACE", None)
        if self._saved_ws is not None:
            os.environ["HERMES_KANBAN_WORKSPACE"] = self._saved_ws
        super().tearDown()

    def board(self, with_form):
        import yaml
        doc = yaml.safe_load(BOARD_YAML.read_text())
        doc.pop("handoff", None)
        if with_form:
            doc["handoff"] = {"form": self.FORM, "fields": list(_FORM_FIELDS)}
        path = _SCRATCH / f"t38-board-{'form' if with_form else 'plain'}.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
        os.environ["KANBAN_BOARD_FILE"] = str(path)
        return path

    def tool_ran(self, tid, tool, args, result):
        """A tool call as upstream reports it to post_tool_call."""
        with Worker(tid, "coding"):
            model_tools._emit_post_tool_call_hook(function_name=tool, function_args=args,
                                                  result=result)

    def hand_off(self, tid, summary):
        before_log = _LOG.read_text() if _LOG.exists() else ""
        with Worker(tid, "coding"):
            res = json.loads(model_tools.handle_function_call(
                "kanban_handoff", {"summary": summary}))
        written = (_LOG.read_text() if _LOG.exists() else "")[len(before_log):]
        return res, written

    def refused(self, tid, summary):
        res, written = self.hand_off(tid, summary)
        # D11: the agent got it, the board did not move, it is on disk
        self.assertIn("error", res, f"the hand-off went through: {res}")
        self.conn.close()
        self.conn = kb.connect()
        card = kb.get_task(self.conn, tid)
        self.assertEqual(card.status, "running", "the board moved")
        self.assertEqual(card.assignee, "coding", "the assignee changed")
        self.assertIn("[BLOCKED]", written, "nothing on disk")
        self.assertIn("tool=kanban_handoff", written)
        msg = res["error"]
        self.assertIn("This refusal is final", msg)
        low = msg.lower()
        for word in _RETRY_WORDS:
            self.assertNotIn(word, low, f"reads as retryable: {word!r}")
        self.assertIn(self.FORM, msg, "the refusal does not point at the form")
        return msg

    def accepted(self, tid, summary):
        res, _ = self.hand_off(tid, summary)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(self.status(tid), "ready")

    def git_workspace(self):
        import subprocess
        ws = Path(tempfile.mkdtemp(prefix="t38-ws-", dir=_SCRATCH))

        def git(*a):
            subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True)
        git("init", "-q")
        (ws / "tracked.py").write_text("a = 1\n")
        (ws / "theirs.py").write_text("b = 1\n")
        git("add", ".")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        (ws / "theirs.py").write_text("b = 2  # someone else's dirty edit\n")
        (ws / "stray.txt").write_text("someone else's untracked file\n")
        os.environ["HERMES_KANBAN_WORKSPACE"] = str(ws)
        return ws


class HandoffForm(HandoffFormGate):
    """T38: on a board whose spec carries `handoff:`, a hand-off whose summary
    does not meet the form is refused -- final, logged, the board unchanged
    (D11) -- and the refusal teaches: the field or path, the form's path, and
    the line of the agent's own prompt that mentions the form."""

    # structure ----------------------------------------------------------------
    def test_T38_a_missing_field_is_refused_and_named(self):
        tid = self.running_card()
        msg = self.refused(tid, form_summary(drop="Review first"))
        self.assertIn("Review first:", msg)

    def test_T38_none_is_a_value(self):
        tid = self.running_card()
        self.accepted(tid, form_summary())

    def test_T38_fields_are_case_insensitive(self):
        tid = self.running_card()
        self.accepted(tid, form_summary().replace("Could not check:", "could NOT check:"))

    # computed Files -------------------------------------------------------------
    def test_T38_a_file_written_by_a_tool_but_not_listed_is_refused(self):
        tid = self.running_card()
        path = self.ws / f"t38-{tid}-out.py"
        path.write_text(_REAL_CODE)
        self.tool_ran(tid, "write_file", {"path": str(path), "content": _REAL_CODE},
                      json.dumps({"bytes_written": len(_REAL_CODE)}))
        msg = self.refused(tid, form_summary())
        self.assertIn(path.name, msg)
        self.accepted(tid, form_summary(files=str(path)))

    def test_T38_a_patched_file_counts_too(self):
        tid = self.running_card()
        path = _SCRATCH / f"t38-{tid}-patched.py"
        self.tool_ran(tid, "patch", {"mode": "replace", "path": str(path),
                                     "old_string": "a", "new_string": "b"},
                      json.dumps({"success": True, "diff": "-a\n+b"}))
        msg = self.refused(tid, form_summary(files="elsewhere.py"))
        self.assertIn(path.name, msg)

    def test_T38_a_file_the_run_changed_in_git_is_demanded_one_it_did_not_is_not(self):
        # A worker must never be refused for a file it did not touch.
        ws = self.git_workspace()
        tid = self.running_card()
        with Worker(tid, "coding"):      # the run starts: its first tool call
            get_pre_tool_call_block_message("read_file", {"path": str(ws / "tracked.py")})
        (ws / "tracked.py").write_text("a = 2  # changed outside any file tool\n" + _REAL_CODE)
        (ws / "new.txt").write_text("made by the run\n" + _REAL_CODE)
        msg = self.refused(tid, form_summary(files="new.txt"))
        self.assertIn("tracked.py", msg)
        self.assertNotIn("theirs.py", msg)
        self.assertNotIn("stray.txt", msg)
        msg = self.refused(tid, form_summary(files="tracked.py"))
        self.assertIn("new.txt", msg)
        self.accepted(tid, form_summary(files="tracked.py, new.txt"))

    def test_T38_extra_listed_paths_must_exist(self):
        # Inverted by T47 (2026-09-22): this test used to accept listed paths
        # that did not exist, which is how a placeholder deliverable got past
        # the form. An extra listed path is fine only when it is real work.
        tid = self.running_card()
        msg = self.refused(tid, form_summary(files="a.py, b.py, docs/c.md"))
        self.assertIn("a.py", msg)
        (self.ws / "docs").mkdir()
        for name in ("a.py", "b.py", "docs/c.md"):
            (self.ws / name).write_text(_REAL_CODE)
        self.accepted(tid, form_summary(files="a.py, b.py, docs/c.md"))

    # consistency ----------------------------------------------------------------
    def test_T38_a_failed_tool_call_and_could_not_check_none_is_refused(self):
        tid = self.running_card()
        self.tool_ran(tid, "terminal", {"command": "pytest -q tests/test_x.py"},
                      json.dumps({"error": "exit 2: collection failed"}))
        msg = self.refused(tid, form_summary())
        self.assertIn("terminal", msg)
        self.assertIn("collection failed", msg)
        self.assertIn("if you recovered from it, say so under Could not check", msg)
        self.accepted(tid, form_summary(could="the x tests: collection failed"))

    def test_T38_a_recovered_failure_does_not_force_could_not_check(self):
        tid = self.running_card()
        cmd = {"command": "pytest -q tests/test_x.py"}
        self.tool_ran(tid, "terminal", cmd, json.dumps({"error": "exit 1: 1 failed"}))
        self.tool_ran(tid, "terminal", cmd, json.dumps({"output": "1 passed", "exit_code": 0}))
        self.accepted(tid, form_summary())

    def test_T38_a_later_success_with_other_args_does_not_recover(self):
        tid = self.running_card()
        self.tool_ran(tid, "terminal", {"command": "pytest -q a"},
                      json.dumps({"error": "exit 1"}))
        self.tool_ran(tid, "terminal", {"command": "pytest -q b"},
                      json.dumps({"output": "ok"}))
        self.refused(tid, form_summary())

    # the refusal teaches --------------------------------------------------------
    def test_T38_the_refusal_quotes_the_prompt_line_that_names_the_form(self):
        self.soul.write_text("You are the coding agent.\n"
                             "Every hand-off follows HANDOVER.md, field by field.\n"
                             "Be brief.\n")
        tid = self.running_card()
        msg = self.refused(tid, form_summary(drop="Doubts"))
        self.assertIn("Every hand-off follows HANDOVER.md, field by field.", msg)

    def test_T38_the_refusal_says_when_the_prompt_does_not_mention_the_form(self):
        self.soul.write_text("You are the coding agent.\n")
        tid = self.running_card()
        msg = self.refused(tid, form_summary(drop="Doubts"))
        self.assertIn("your system prompt does not mention HANDOVER.md", msg)

    # opt-in ---------------------------------------------------------------------
    def test_T38_no_handoff_key_keeps_the_old_behaviour(self):
        self.board(with_form=False)
        tid = self.running_card()
        path = _SCRATCH / f"t38-{tid}-unlisted.py"
        self.tool_ran(tid, "write_file", {"path": str(path), "content": ""},
                      json.dumps({"bytes_written": 0}))
        self.tool_ran(tid, "terminal", {"command": "false"}, json.dumps({"error": "exit 1"}))
        self.accepted(tid, "did it; tests pass")

    def test_T38_the_spec_key_is_validated(self):
        import yaml
        import board as board_mod
        spec = board_mod.load_board(self.board(with_form=True))
        self.assertEqual(spec.handoff["fields"], _FORM_FIELDS)
        self.assertEqual(spec.handoff["form"], self.FORM)
        self.assertIsNone(board_mod.load_board(self.board(with_form=False)).handoff)
        for bad in ({"form": self.FORM, "fields": []},
                    {"form": self.FORM, "fields": ["Done", ""]},
                    {"form": self.FORM, "fields": "Done"},
                    {"form": 3, "fields": ["Done"]}):
            with self.subTest(bad=bad):
                doc = yaml.safe_load(BOARD_YAML.read_text())
                doc["handoff"] = bad
                p = _SCRATCH / "t38-bad.yaml"
                p.write_text(yaml.safe_dump(doc, sort_keys=False))
                errs = [i for i in board_mod.load_board(p).errors if i.code == "handoff-form"]
                self.assertTrue(errs, f"{bad} was accepted")

    def test_T38_the_shipped_board_carries_the_form(self):
        import board as board_mod
        spec = board_mod.load_board(BOARD_YAML)
        self.assertEqual(spec.handoff, {"form": self.FORM, "fields": _FORM_FIELDS})

    # visible when absent --------------------------------------------------------
    def test_T38_a_board_without_a_form_pages_once_and_closes_when_it_appears(self):
        import board as board_mod
        import escalator
        conn = kb.connect()
        try:
            db = str(kb.kanban_db_path())
        finally:
            conn.close()
        cur = Path(db).parent / ".cur-t38"
        cur.write_text("0")
        state = str(Path(db).parent / ".pages-t38.json")
        home = Path(tempfile.mkdtemp(prefix="t38-home-", dir=_SCRATCH))
        sent = []

        class Rec:
            name = "rec"

            def send(self, m):
                sent.append(m)

        def tick(at, spec):
            before = len(sent)
            escalator.run_once(db, board="default", config={}, apply=False, notify=True,
                               cursor_path=str(cur), sender=Rec(), state_path=state,
                               board_spec=spec, hermes_home=str(home), now=at)
            return sent[before:]

        plain = board_mod.load_board(self.board(with_form=False))
        form = board_mod.load_board(self.board(with_form=True))
        t0 = int(time.time())
        first = [m for m in tick(t0, plain) if "handoff_form_missing" in m.title]
        self.assertEqual(len(first), 1, "a board with no hand-off form was not paged")
        self.assertIn("board default has a spec but no hand-off form; hand-offs are "
                      "unchecked", first[0].title + first[0].body)
        again = [m for m in tick(t0 + 60, plain) if "handoff_form_missing" in m.title]
        self.assertEqual(again, [], "paged twice")
        closed = [m for m in tick(t0 + 120, form)
                  if "resolved" in m.title and "handoff_form" in m.title + m.body]
        self.assertEqual(len(closed), 1, "the fault did not close when the key appeared")


def harness_example():
    return (PLUGIN_DIR / "harness.yaml.example").read_text()


# ---------------------------------------------------------------------------
# T40-T44: the bypasses. A gate that can be walked round by indirection, a
# script, a code tool, a dispatcher tick or a second file is not a gate.
# ---------------------------------------------------------------------------

#: Every shell form that hides the program word behind something the gate would
#: have to evaluate to read. Each maps a program word `h` and the words after it
#: to one command line. A form added here is covered by every test below.
_HIDDEN_PROGRAM = {
    "bare variable":        lambda h, rest: f"H={h}; $H {rest}",
    "braced variable":      lambda h, rest: f"H={h}; ${{H}} {rest}",
    "eval":                 lambda h, rest: f"eval '{h} {rest}'",
    "exec variable":        lambda h, rest: f"H={h}; exec $H {rest}",
    "backticks":            lambda h, rest: f"`echo {h}` {rest}",
    "command substitution": lambda h, rest: f"$(echo {h}) {rest}",
    "sh -c":                lambda h, rest: f"sh -c 'H={h}; $H {rest}'",
    "bash -c":              lambda h, rest: f"bash -c 'H={h}; $H {rest}'",
    "env with assignment":  lambda h, rest: f"H={h}; env A=1 $H {rest}",
    "xargs variable":       lambda h, rest: f"H={h}; echo x | xargs $H {rest}",
}

#: What follows the program word: the verb spelled literally, and split so that
#: no token anywhere contains the word "kanban".
_PAYLOADS = {
    "literal": lambda tid: f"kanban complete {tid}",
    "split":   lambda tid: f"${{v}}${{w}} complete {tid}",
}
_SPLIT_PREFIX = "v=kan; w=ban; "

#: Literal wrappers and bodies the rule must NOT refuse: resolving is not refusing.
_RESOLVABLE = [
    "env A=1 pytest -q", "timeout 5 pytest -q", "nohup git status",
    "command ls", "exec ls", "echo a | xargs grep foo", "bash -c 'pytest -q'",
    "sh -c \"git status\"", "pytest $FILE", "cd $DIR && ls", "echo $(date)",
    "ls `pwd`", "hermes kanban list", "hermes kanban show $ID",
]

#: A literal `hermes` whose board or verb cannot be read: the gate cannot tell
#: which table governs or which action is taken, so it refuses to guess.
_HERMES_UNRESOLVED = [
    "hermes $k complete {tid}", "hermes kanban $v {tid}",
    "hermes --board $B kanban complete {tid}",
    "echo {tid} | xargs hermes kanban complete",
    "echo complete | xargs hermes kanban",
]


def _hermes_config(text):
    (_HERMES / "config.yaml").write_text(text)


def _unprevented_section():
    """The contract's dated section on routes the harness cannot prevent."""
    text = CONTRACT_DOC.read_text()
    m = re.search(r"^## Unprevented routes\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert m, "the contract has no '## Unprevented routes' section"
    body = m.group(1)
    assert re.search(r"\b20\d\d-\d\d-\d\d\b", body), "the section carries no date"
    return body


class Bypasses(Gate):

    def setUp(self):
        super().setUp()
        self.config(harness_example(), "bypasses.yaml")

    def tearDown(self):
        _hermes_config("kanban:\n  auto_decompose: false\n")
        super().tearDown()

    # T40 -------------------------------------------------------------------
    def test_T40_every_hidden_program_word_is_refused(self):
        """Generated: every form x every payload, refused with all three
        assertions, and the refusal says the way through: write it literally."""
        tid = self.running_card()
        n = 0
        for form, build in _HIDDEN_PROGRAM.items():
            for pay, payload in _PAYLOADS.items():
                cmd = build("hermes", payload(tid))
                if pay == "split":
                    cmd = _SPLIT_PREFIX + cmd
                with self.subTest(form=form, payload=pay, cmd=cmd):
                    msg = self.assert_refused(tid, "coding", "terminal", {"command": cmd},
                                              f"{form}/{pay}")
                    self.assertIn("write the command literally", msg)
                    n += 1
        self.assertEqual(n, len(_HIDDEN_PROGRAM) * len(_PAYLOADS))

    def test_T40_a_hidden_program_word_is_refused_whatever_it_runs(self):
        """No keyword: a hidden program word is refused even when nothing in the
        text names the board."""
        tid = self.running_card()
        for form, build in _HIDDEN_PROGRAM.items():
            with self.subTest(form=form):
                self.assert_refused(tid, "coding", "terminal",
                                    {"command": build("pytest", "-q")}, form)

    def test_T40_an_unreadable_board_or_verb_after_literal_hermes_is_refused(self):
        tid = self.running_card()
        for cmd in _HERMES_UNRESOLVED:
            cmd = cmd.format(tid=tid)
            with self.subTest(cmd=cmd):
                msg = self.assert_refused(tid, "coding", "terminal", {"command": cmd}, cmd)
                self.assertIn("write the command literally", msg)

    def test_T40_literal_wrappers_and_bodies_are_resolved_not_refused(self):
        tid = self.running_card()
        with Worker(tid, "coding"):
            for cmd in _RESOLVABLE:
                with self.subTest(cmd=cmd):
                    self.assertIsNone(get_pre_tool_call_block_message(
                        "terminal", {"command": cmd}), cmd)

    def test_T40_a_literal_wrapper_around_a_refused_verb_is_still_refused(self):
        tid = self.running_card()
        for cmd in (f"env A=1 hermes kanban complete {tid}",
                    f"timeout 5 hermes kanban complete {tid}",
                    f"bash -c 'hermes kanban complete {tid}'",
                    f"nohup hermes kanban complete {tid}"):
            with self.subTest(cmd=cmd):
                self.assert_refused(tid, "coding", "terminal", {"command": cmd}, cmd)

    # T41 -------------------------------------------------------------------
    def test_T41_a_script_is_not_prevented_and_the_contract_says_so(self):
        """Pinned truth: the gate does not read inside a script file. If this
        ever fails, the route became prevented: update the contract with it."""
        tid = self.running_card()
        script = _SCRATCH / "t41.sh"
        script.write_text(f"hermes kanban complete {tid}\n")
        with Worker(tid, "coding"):
            self.assertIsNone(get_pre_tool_call_block_message(
                "terminal", {"command": f"bash {script}"}))
        self.assertIn("script", _unprevented_section().lower())

    # T42 -------------------------------------------------------------------
    def test_T42_code_execution_is_not_prevented_and_the_contract_says_so(self):
        tid = self.running_card()
        code = ('import subprocess, os\n'
                'h = os.environ.get("HERMES", "hermes")\n'
                f'subprocess.run([h, "kanban", "complete", "{tid}"])\n')
        with Worker(tid, "coding"):
            self.assertIsNone(get_pre_tool_call_block_message("execute_code", {"code": code}))
            # The shell rule does not apply to code: `$` in a Python string is
            # not a program word.
            self.assertIsNone(get_pre_tool_call_block_message(
                "execute_code", {"code": 'print("$H `x` $(y)")'}))
        db = str(kb.kanban_db_path())
        for code in ("from hermes_cli import kanban_db",
                     f"import sqlite3; sqlite3.connect('{db}')"):
            with self.subTest(code=code):   # what is caught stays caught
                self.assert_refused(tid, "coding", "execute_code", {"code": code}, code)
        section = _unprevented_section()
        self.assertIn("code execution", section.lower())
        self.assertIn("should not hold a tool that can", section)

    # T43 -------------------------------------------------------------------
    def test_T43_auto_decompose_must_be_false_in_so_many_words(self):
        tid = self.running_card()
        for label, text in (("true", "kanban:\n  auto_decompose: true\n"),
                            ("absent", "kanban:\n  dispatch_interval: 60\n"),
                            ("no kanban section", "model: x\n")):
            with self.subTest(label):
                _hermes_config(text)
                with self.assertRaises(harness.HarnessConfigError) as caught:
                    harness.load_config()
                self.assertIn("auto_decompose", str(caught.exception))
                self.assert_refused(tid, "coding", "kanban_create", {"title": "x"}, label)
        (_HERMES / "config.yaml").unlink()
        with self.assertRaises(harness.HarnessConfigError):
            harness.load_config()
        _hermes_config("kanban:\n  auto_decompose: false\n")
        harness.load_config()

    def test_T43_every_shipped_config_example_says_false(self):
        import yaml
        files = [REPO / "config" / "config.yaml.example",
                 *sorted((REPO / "config" / "profiles").glob("*/config.yaml.example"))]
        self.assertGreater(len(files), 3)
        for f in files:
            with self.subTest(str(f.relative_to(REPO))):
                doc = yaml.safe_load(f.read_text()) or {}
                self.assertIs((doc.get("kanban") or {}).get("auto_decompose"), False)

    # T44 -------------------------------------------------------------------
    def _board_with(self, name, mutate):
        import yaml
        doc = yaml.safe_load(BOARD_YAML.read_text())
        mutate(doc["roles"])
        path = _SCRATCH / name
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
        os.environ["KANBAN_BOARD_FILE"] = str(path)

    def test_T44_board_role_keys_bind_the_grant_table_strictest_wins(self):
        """Every permission board.yaml states about a role is enforced against
        harness.yaml's grants: a board forbidding a granted move fails the load."""
        cases = {
            "may_not": lambda r: r["coding"].update(may_not=["to_do"]),
            "hands_off_to": lambda r: r["coding"].update(hands_off_to="user"),
            "rework_to": lambda r: r["qa"].update(rework_to="question"),
            "owns": lambda r: r["user"].update(owns=["question", "done", "archived"]),
            "human": lambda r: r["qa"].update(human=True),
        }
        tid = self.running_card()
        for key, mutate in cases.items():
            with self.subTest(key):
                self._board_with(f"t44-{key}.yaml", mutate)
                with self.assertRaises(harness.HarnessConfigError) as caught:
                    harness.load_config()
                self.assertIn(key, str(caught.exception))
                self.assert_refused(tid, "coding", "kanban_create", {"title": "x"}, key)
        os.environ["KANBAN_BOARD_FILE"] = str(BOARD_YAML)
        harness.load_config()   # the shipped pair agrees

    def test_T44_an_unknown_role_key_fails_the_load(self):
        for key in ("approves", "may", "can_archive", "notes"):
            with self.subTest(key):
                self._board_with(f"t44-unknown-{key}.yaml",
                                 lambda r: r["qa"].update({key: ["done"]}))
                with self.assertRaises(harness.HarnessConfigError) as caught:
                    harness.load_config()
                self.assertIn(repr(key), str(caught.exception))

    # T46 -------------------------------------------------------------------
    def test_T46_in_process_python_is_not_prevented_and_the_contract_says_so(self):
        """Pinned truth: in-process Python reaches the board and the gate cannot
        refuse it. This test is expected to fail when the hole is closed; update
        the contract with it. The sentence check pins one known-bad sentence and
        cannot police the claim in general."""
        tid = self.running_card()
        imports = ("from hermes_cli import kanban_swarm",
                   "import hermes_cli.kanban as k; k.main(['create', 'x'])",
                   "from tools.kanban_tools import _handle_create",
                   # a class, not a list: an assembled name reaches it too
                   'import importlib; importlib.import_module("hermes" + "_cli.kanban")')
        with Worker(tid, "coding"):
            for code in imports:
                with self.subTest(code=code):
                    self.assertIsNone(get_pre_tool_call_block_message(
                        "execute_code", {"code": code}))
        section = _unprevented_section()
        low = section.lower()
        self.assertIn("in-process python", low)
        for name in ("kanban_swarm", "hermes_cli.kanban", "tools.kanban_tools"):
            self.assertIn(name, section)
        self.assertNotIn("module stay refused", section)
        for sentence in re.split(r"(?<=[.:;])\s+", " ".join(section.split())):
            s = sentence.lower()
            self.assertFalse("refused" in s and "module" in s,
                             f"'refused' beside 'module' reads as a guarantee: {sentence!r}")
        self.assertIn("code_execution", section)
        self.assertIn("API or database layer", section)


class HandoffDeliverable(HandoffFormGate):
    """T47: every path on the Files line must be work. A path that is missing,
    empty, a placeholder, outside the work tree, a directory, or unreadable
    is refused -- final, logged, the board unchanged (D11) -- and the refusal
    names the path and says what would satisfy it. A byte count catches a
    placeholder, not a plausible lie: see "Unprevented routes"."""

    def listed(self, text, name="out.md"):
        path = self.ws / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)
        return path

    def refused_for(self, files, *words):
        tid = self.running_card()
        msg = self.refused(tid, form_summary(files=files))
        for w in words:
            self.assertIn(w, msg)
        return msg

    # refused ------------------------------------------------------------------
    def test_T47_a_missing_file_is_refused(self):
        self.refused_for("findings/example-0001.md", "findings/example-0001.md", "does not exist")

    def test_T47_an_empty_file_is_refused(self):
        self.listed("", "findings/example-0001.md")
        self.refused_for("findings/example-0001.md", "findings/example-0001.md", "0 bytes")

    def test_T47_the_five_byte_placeholder_is_refused(self):
        self.listed("hello", "findings/example-0001.md")
        msg = self.refused_for("findings/example-0001.md", "findings/example-0001.md", "5 bytes",
                               "cannot be the work")
        self.assertIn("docstring", msg, "the refusal does not say what satisfies it")

    def test_T47_a_markdown_file_of_only_headings_is_refused(self):
        self.listed("# Findings example-0001\n\n## Summary\n\n## Verdicts\n\n## Raw extractor output\n")
        self.refused_for("out.md", "out.md", "only headings")

    def test_T47_a_verdict_table_with_no_rows_is_refused(self):
        self.listed("# Findings example-0001\n\n| field | before | after | verdict |\n|---|---|---|---|\n")
        self.refused_for("out.md", "out.md", "no rows")

    def test_T47_an_unreadable_file_is_refused(self):
        path = self.listed(_REAL_FINDINGS)
        path.chmod(0)
        try:
            self.refused_for("out.md", "out.md", "cannot be read")
        finally:
            path.chmod(0o644)

    def test_T47_markdown_that_is_not_utf8_is_refused(self):
        self.listed(b"\xff\xfe" + _REAL_FINDINGS.encode("utf-16-le"))
        self.refused_for("out.md", "out.md", "cannot be read")

    def test_T47_a_directory_is_refused(self):
        self.listed(_REAL_FINDINGS, "findings/example-0001.md")
        self.refused_for(".", "list the files, not the directory")
        self.refused_for("findings", "list the files, not the directory")

    def test_T47_prose_on_the_files_line_is_refused(self):
        self.refused_for("updated the parser and its tests", "takes paths")

    def test_T47_a_file_outside_the_work_tree_is_refused(self):
        self.refused_for("/etc/hosts", "/etc/hosts", "must live in the work tree")

    def test_T47_a_symlink_out_of_the_work_tree_is_refused(self):
        outside = Path(tempfile.mkdtemp(prefix="t47-outside-", dir=_SCRATCH)) / "real.md"
        outside.write_text(_REAL_FINDINGS)
        (self.ws / "link.md").symlink_to(outside)
        self.refused_for("link.md", "link.md", "must live in the work tree")

    def test_T47_with_no_workspace_no_path_can_pass(self):
        path = self.listed(_REAL_FINDINGS)
        os.environ.pop("HERMES_KANBAN_WORKSPACE", None)
        self.refused_for(str(path), "no workspace")

    # accepted -----------------------------------------------------------------
    def test_T47_a_real_findings_file_passes(self):
        self.listed(_REAL_FINDINGS, "findings/example-0001.md")
        self.accepted(self.running_card(), form_summary(files="findings/example-0001.md"))

    def test_T47_a_small_binary_over_the_threshold_passes_on_size(self):
        self.listed(bytes(range(256)), "photo.jpg")
        self.accepted(self.running_card(), form_summary(files="photo.jpg"))

    def test_T47_files_none_on_a_run_that_wrote_nothing_passes(self):
        self.accepted(self.running_card(), form_summary(files="none"))

    def test_T47_a_bulleted_multi_line_list_passes(self):
        self.listed(_REAL_FINDINGS, "findings/a file.md")
        self.listed(_REAL_CODE, "src/parse.py")
        files = "\n- `findings/a file.md`\n- src/parse.py"
        self.accepted(self.running_card(), form_summary(files=files))

    def test_T47_a_file_the_run_deleted_passes(self):
        ws = self.git_workspace()
        tid = self.running_card()
        with Worker(tid, "coding"):      # the run starts: its first tool call
            get_pre_tool_call_block_message("read_file", {"path": str(ws / "tracked.py")})
        (ws / "tracked.py").unlink()
        self.accepted(tid, form_summary(files="tracked.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
