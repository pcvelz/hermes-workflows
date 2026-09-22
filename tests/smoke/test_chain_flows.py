#!/usr/bin/env python3
"""The chain flows: a card the planner cuts is claimed with no command typed.

The motor is upstream's: the gateway hosts one dispatcher
(``kanban.dispatch_in_gateway``, singleton ``kanban/.dispatcher.lock``) that
calls ``kanban_db.dispatch_once`` every ``dispatch_interval_seconds``.  These
tests drive that exact call against the REAL kanban_db in a scratch
HERMES_HOME, with the caps the live config sets, and a stub spawn so no model
runs.  A second, separate dispatcher is deliberately NOT added: upstream warns
that two dispatchers on one kanban.db race for claims and can corrupt the WAL.

Stdlib unittest; exits 77 (SKIP) when the hermes-agent source is absent.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENT_SRC = Path(os.path.expanduser(
    os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent"))).resolve()

_HOME = Path(tempfile.mkdtemp(prefix="chain-flows-test-")).resolve()
_REAL = Path(os.path.expanduser("~/.hermes")).resolve()
if _HOME == _REAL or _REAL in _HOME.parents:
    sys.exit("refusing: scratch HERMES_HOME resolves inside the real ~/.hermes")
for key in [k for k in os.environ if k.startswith("HERMES_KANBAN")] + ["HERMES_PROFILE"]:
    os.environ.pop(key, None)
os.environ["HERMES_HOME"] = str(_HOME)

_SAVED = {}


def setUpModule():
    _SAVED["HERMES_HOME"] = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(_HOME)


def tearDownModule():
    if _SAVED.get("HERMES_HOME") is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = _SAVED["HERMES_HOME"]


if not (AGENT_SRC / "hermes_cli" / "kanban_db.py").exists():
    print(f"SKIP hermes-agent source not found at {AGENT_SRC} (set HERMES_AGENT_SRC)")
    sys.exit(77)
sys.path.insert(0, str(AGENT_SRC))
try:
    from hermes_cli import kanban_db  # noqa: E402
except Exception as exc:  # pragma: no cover
    print(f"SKIP cannot import hermes_cli.kanban_db: {exc}")
    sys.exit(77)

#: The caps the live config sets: per-profile on, global off (setting both
#: double-counts running cards upstream).
LIVE_CAPS = {"max_in_progress": None, "max_in_progress_per_profile": 1}


def fresh_board():
    db_path = kanban_db.kanban_db_path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    for name in ("planner", "coding"):
        (_HOME / "profiles" / name).mkdir(parents=True, exist_ok=True)
        (_HOME / "profiles" / name / "config.yaml").touch()
    return kanban_db.connect(kanban_db.init_db())


def status_of(conn, card_id):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (card_id,)).fetchone()["status"]


class StubSpawn:
    """Records spawns; returns a PID the test controls."""

    def __init__(self, pid=None):
        self.calls = []
        self.pid = pid if pid is not None else os.getpid()

    def __call__(self, task, workspace, *args, **kwargs):
        self.calls.append(task.id if hasattr(task, "id") else task["id"])
        return self.pid


def tick(conn, spawn):
    # One gateway tick, exactly as gateway/kanban_watchers.py makes it.
    return kanban_db.dispatch_once(conn, spawn_fn=spawn, stale_timeout_seconds=0, **LIVE_CAPS)


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class ChainFlows(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_board()

    def tearDown(self):
        self.conn.close()

    def test_planner_card_is_claimed_and_started_without_a_command(self):
        parent = kanban_db.create_task(self.conn, title="cut", assignee="planner",
                                       created_by="planner")
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
        self.conn.commit()
        child = kanban_db.create_task(self.conn, title="child", assignee="coding",
                                      created_by="planner", parents=[parent])
        self.assertEqual(status_of(self.conn, child), "todo")
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        self.conn.commit()
        spawn = StubSpawn()
        tick(self.conn, spawn)
        self.assertEqual(spawn.calls, [child])
        self.assertEqual(status_of(self.conn, child), "running")

    def test_per_profile_cap_holds(self):
        a = kanban_db.create_task(self.conn, title="a", assignee="coding")
        b = kanban_db.create_task(self.conn, title="b", assignee="coding")
        spawn = StubSpawn()
        tick(self.conn, spawn)
        self.assertEqual(len(spawn.calls), 1)
        self.assertEqual(sorted([status_of(self.conn, a), status_of(self.conn, b)]),
                         ["ready", "running"])

    def test_gone_pid_returns_card_to_ready(self):
        card = kanban_db.create_task(self.conn, title="c", assignee="coding")
        tick(self.conn, StubSpawn(pid=dead_pid()))
        self.assertEqual(status_of(self.conn, card), "running")
        # Past the launch grace window, the PID is checked and found gone.
        self.conn.execute("UPDATE tasks SET started_at = started_at - 3600 WHERE id=?", (card,))
        self.conn.commit()
        res = kanban_db.dispatch_once(self.conn, spawn_fn=StubSpawn(pid=dead_pid()),
                                      stale_timeout_seconds=0, max_spawn=0, **LIVE_CAPS)
        self.assertIn(card, res.crashed)
        self.assertNotEqual(status_of(self.conn, card), "running")

    def test_live_pid_with_silent_heartbeat_stays_running(self):
        # A heartbeat is not proof of death; a live PID keeps its claim.
        card = kanban_db.create_task(self.conn, title="d", assignee="coding")
        tick(self.conn, StubSpawn(pid=os.getpid()))
        self.conn.execute("UPDATE tasks SET started_at = started_at - 3600, "
                          "last_heartbeat_at = NULL WHERE id=?", (card,))
        self.conn.commit()
        res = tick(self.conn, StubSpawn())
        self.assertNotIn(card, res.crashed)
        self.assertEqual(status_of(self.conn, card), "running")


if __name__ == "__main__":
    unittest.main()
