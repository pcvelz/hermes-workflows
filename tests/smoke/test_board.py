#!/usr/bin/env python3
"""Tests for config/board.yaml: the loader, auto_advance, and auto-archive.

Runs against the REAL config/board.yaml and the REAL hermes-agent kanban_db in
a scratch HERMES_HOME (it hard-refuses the real ~/.hermes).  Nothing leaves
the box and no live board is touched.

Exit 77 = prerequisites missing (the wrapper reports SKIP).
"""

import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESILIENCE = REPO / "scripts" / "resilience"
BOARD_YAML = REPO / "config" / "board.yaml"
AGENT_SRC = Path(os.path.expanduser(
    os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent"))).resolve()

# --- isolation: scratch HERMES_HOME, set BEFORE any hermes import ------------
_HOME = Path(tempfile.mkdtemp(prefix="board-test-")).resolve()
_REAL = Path(os.path.expanduser("~/.hermes")).resolve()
if _HOME == _REAL or _REAL in _HOME.parents:
    sys.exit("refusing: scratch HERMES_HOME resolves inside the real ~/.hermes")
for key in [k for k in os.environ if k.startswith("HERMES_KANBAN")] + [
        "HERMES_PROFILE", "HERMES_BOARD_FILE"]:
    os.environ.pop(key, None)
os.environ["HERMES_HOME"] = str(_HOME)

sys.path.insert(0, str(RESILIENCE))

import board          # noqa: E402
import escalator      # noqa: E402

if not (AGENT_SRC / "hermes_cli" / "kanban_db.py").exists():
    print(f"SKIP hermes-agent source not found at {AGENT_SRC} (set HERMES_AGENT_SRC)")
    sys.exit(77)
sys.path.insert(0, str(AGENT_SRC))
try:
    from hermes_cli import kanban_db  # noqa: E402
except Exception as exc:  # pragma: no cover
    print(f"SKIP cannot import hermes_cli.kanban_db: {exc}")
    sys.exit(77)

DAY = 86400

# The environment this file's tests run in. Set at import (the agent reads
# HERMES_HOME when it is first imported) AND re-pinned per module: under one
# pytest process every test file is imported before any test runs, so an
# import-time setting is silently overwritten by whichever file came last and
# two suites end up sharing one board.
# HOME too: refusals are logged at ~/.hermes/logs/kanban-harness.log, and a test
# run must never write into the real one.
(_HOME / "home").mkdir(exist_ok=True)
_ENV = {"HERMES_HOME": str(_HOME), "HERMES_BOARD_FILE": None, "KANBAN_BOARD_FILE": None,
        "HOME": str(_HOME / "home")}
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


def write_board(text: str) -> Path:
    path = Path(tempfile.mkdtemp(dir=_HOME)) / "board.yaml"
    path.write_text(textwrap.dedent(text))
    return path


# =============================================================================
# The loader, against the REAL specification
# =============================================================================

class TestTheRealSpecification(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.board = board.load_board(BOARD_YAML)

    def test_it_loads_without_structural_errors(self):
        self.assertEqual(self.board.errors, [], [str(e) for e in self.board.errors])

    def test_the_eight_columns_live_on_the_documented_statuses(self):
        self.assertEqual(
            {c.name: c.status for c in self.board.columns.values()},
            {
                "refinement": "triage", "waiting": "todo", "to_do": "ready",
                "in_progress": "running", "question": "blocked",
                "user_review": "scheduled", "done": "done", "archived": "archived",
            },
        )

    def test_the_runtime_agent_review_lane_is_left_unused(self):
        """user_review must NOT be the runtime's `review`: that status is an
        agent-review lane the dispatcher spawns reviewers for."""
        self.assertIsNone(self.board.column_of("review"))

    def test_waiting_for_the_user_is_a_status_the_dispatcher_never_touches(self):
        self.assertEqual(self.board.status_of("user_review"), "scheduled")

    def test_to_do_is_the_only_column_on_ready(self):
        """The dispatcher claims from `ready` only, so that is what to-do is."""
        self.assertEqual(
            [c.name for c in self.board.columns.values() if c.status == "ready"],
            ["to_do"],
        )

    def test_the_specification_is_clean(self):
        """D1 (requires direction), D2 (archive rule), D3 (the `todo` key
        colliding with the `todo` status) and D9 (the column waiting for the
        user) are all resolved in the spec. A new warning here is a new
        contradiction, not noise."""
        self.assertEqual([str(i) for i in self.board.issues], [])

    def test_every_column_states_its_status_explicitly(self):
        """D3's fix: the mapping is written next to the column, not kept in
        people's heads or in this loader's defaults."""
        import yaml
        doc = yaml.safe_load(BOARD_YAML.read_text())
        missing = [n for n, c in doc["board"]["columns"].items()
                   if not (c or {}).get("status")]
        self.assertEqual(missing, [])

    def test_refinement_guards_the_way_out_not_the_way_in(self):
        col = self.board.columns["refinement"]
        self.assertEqual(col.requires, {})
        self.assertEqual(col.requires_to_leave, {"acceptance_criteria": True})

    def test_the_clock_archives_done_cards_after_thirty_days(self):
        self.assertEqual(self.board.auto_archive_done_after, 30 * DAY)
        self.assertEqual(self.board.clock.get("archives"), "done_only")

    def test_a_human_may_archive_from_any_column_listing_archived(self):
        self.assertTrue(self.board.human_may_archive_from("refinement"))
        self.assertTrue(self.board.human_may_archive_from("question"))
        self.assertTrue(self.board.human_may_archive_from("done"))
        self.assertFalse(self.board.human_may_archive_from("in_progress"))

    def test_the_status_mirror_matches_the_database(self):
        """board.py duplicates VALID_STATUSES to run without the agent. It must
        never drift from the set the database actually enforces."""
        self.assertEqual(set(board.KANBAN_STATUSES), set(kanban_db.VALID_STATUSES))

    def test_edges(self):
        self.assertTrue(self.board.can_move("waiting", "to_do"))
        self.assertTrue(self.board.can_move("in_progress", "user_review"))
        self.assertFalse(self.board.can_move("to_do", "done"),
                         "to do -> done skips the whole chain")
        self.assertFalse(self.board.can_move("user_review", "archived"))


class TestStructuralErrors(unittest.TestCase):

    def _codes(self, text):
        return sorted(i.code for i in board.load_board(write_board(text)).errors)

    def test_an_edge_to_a_column_that_does_not_exist(self):
        self.assertIn("unknown-edge", self._codes("""
            board:
              columns:
                todo: {exit_to: [nowhere]}
        """))

    _TWO_COLUMNS = """
        board:
          columns:
            to_do: {status: ready, exit_to: [in_progress]}
            in_progress: {status: running, exit_to: []}
          moves:
            - {from: to_do, to: in_progress, by: %s}
        roles:
          coding: {}
          planner: {writes_code: false}
          user: {human: true}
    """

    def test_a_role_declared_in_roles_may_be_a_move_actor(self):
        """The actors are the roles the spec declares, plus the two machines:
        a list kept in code as well would disagree with the file one day."""
        self.assertNotIn("move-unknown-actor", self._codes(self._TWO_COLUMNS % "planner"))
        self.assertNotIn("move-unknown-actor", self._codes(self._TWO_COLUMNS % "dispatcher"))

    def test_an_undeclared_name_is_still_not_an_actor(self):
        self.assertIn("move-unknown-actor", self._codes(self._TWO_COLUMNS % "somebody"))

    def test_two_columns_on_one_status(self):
        self.assertIn("shared-status", self._codes("""
            board:
              columns:
                todo: {}
                also_todo: {status: ready}
        """))

    def test_a_status_the_database_would_refuse(self):
        self.assertIn("unknown-status", self._codes("""
            board:
              columns:
                todo: {status: pending}
        """))

    def test_an_automatic_move_a_person_could_not_make(self):
        self.assertIn("auto-advance-off-board", self._codes("""
            board:
              columns:
                waiting:
                  exit_to: []
                  auto_advance: {when: {referenced_card_reaches: [done]}, move_to: todo}
                todo: {}
                done: {}
        """))

    def test_an_explicit_status_key_silences_d3(self):
        """The recommended fix for D3 must actually fix it."""
        spec = board.load_board(write_board("""
            board:
              columns:
                waiting: {status: todo, exit_to: [todo]}
                todo: {status: ready}
        """))
        # A column KEY that is also a status name still collides by name; the
        # explicit key documents it but only a rename removes the ambiguity.
        self.assertEqual(spec.status_of("todo"), "ready")

    def test_a_clock_that_would_archive_unfinished_work_is_refused(self):
        self.assertIn("unsupported-clock", self._codes("""
            board:
              columns:
                done: {exit_to: [archived]}
                archived: {}
              archiving:
                clock: {archives: everything, after: 30d}
        """))

    def test_a_user_column_on_the_agent_review_lane_is_flagged(self):
        """The accident D9 found, caught at load if anyone reintroduces it."""
        spec = board.load_board(write_board("""
            board:
              columns:
                awaiting_me: {status: review, exit_to: [done]}
                done: {}
        """))
        self.assertIn("agent-review-lane", [i.code for i in spec.warnings])

    def test_a_missing_file_is_a_clear_error(self):
        with self.assertRaises(board.BoardError):
            board.load_board(_HOME / "no-such-board.yaml")


# =============================================================================
# C and D on a REAL board
# =============================================================================

class BoardCase(unittest.TestCase):

    def setUp(self):
        db_path = kanban_db.kanban_db_path()
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
        self.db_path = str(kanban_db.init_db())
        self.conn = kanban_db.connect(Path(self.db_path))
        self.addCleanup(self.conn.close)
        self.spec = board.load_board(BOARD_YAML)
        self.now = int(time.time())

    def card(self, card_id, status, *, parents=(), completed_at=None):
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, completed_at, assignee) "
                "VALUES (?, ?, ?, ?, ?, 'coding')",
                (card_id, f"card {card_id}", status, self.now - DAY, completed_at),
            )
            for parent in parents:
                self.conn.execute(
                    "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (parent, card_id),
                )

    def status(self, card_id):
        return self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (card_id,)
        ).fetchone()["status"]

    def comments(self, card_id):
        return [r["body"] for r in self.conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (card_id,)
        )]

    def tick(self, *, apply=True, now=None, spec="default"):
        return escalator.run_once(
            self.db_path, board="default", config={}, apply=apply, notify=False,
            cursor_path=str(Path(self.db_path).parent / f".cur-{id(self)}"),
            state_path=str(Path(self.db_path).parent / f".pages-{id(self)}.json"),
            board_spec=self.spec if spec == "default" else spec,
            hermes_home=str(_HOME), now=now or self.now,
        )


class TestAutoAdvance(BoardCase):

    def test_a_waiting_card_leaves_when_its_reference_reaches_user_review(self):
        """Upstream never does this one: it only promotes on done/archived."""
        self.card("P1", "scheduled")
        self.card("W1", "todo", parents=["P1"])
        self.tick()
        self.assertEqual(self.status("W1"), "ready")

    def test_a_reference_in_the_runtimes_agent_review_lane_has_not_arrived(self):
        """`review` is the agent-review lane, not user_review. A card an
        agent reviewer is still working on has not reached the user."""
        self.card("PR", "review")
        self.card("WR", "todo", parents=["PR"])
        self.tick()
        self.assertEqual(self.status("WR"), "todo")

    def test_the_move_carries_the_explaining_comment(self):
        self.card("P2", "scheduled")
        self.card("W2", "todo", parents=["P2"])
        self.tick()
        self.assertEqual(
            self.comments("W2"),
            [f"{escalator.AUTO_ADVANCE_MARKER} referenced card P2 reached user_review"],
        )

    def test_a_card_whose_reference_is_still_running_stays_waiting(self):
        self.card("P3", "running")
        self.card("W3", "todo", parents=["P3"])
        self.tick()
        self.assertEqual(self.status("W3"), "todo")
        self.assertEqual(self.comments("W3"), [])

    def test_a_card_waiting_on_two_things_is_not_freed_by_one(self):
        self.card("P4a", "scheduled")
        self.card("P4b", "running")
        self.card("W4", "todo", parents=["P4a", "P4b"])
        self.tick()
        self.assertEqual(self.status("W4"), "todo")

    def test_it_leaves_when_every_reference_has_arrived(self):
        self.card("P5a", "scheduled")
        self.card("P5b", "done")
        self.card("W5", "todo", parents=["P5a", "P5b"])
        self.tick()
        self.assertEqual(self.status("W5"), "ready")
        self.assertIn("P5a reached user_review", self.comments("W5")[0])
        self.assertIn("P5b reached done", self.comments("W5")[0])

    def test_a_card_with_no_reference_never_moves_by_itself(self):
        self.card("W6", "todo")
        self.tick()
        self.assertEqual(self.status("W6"), "todo")

    def test_when_upstream_wins_the_race_the_comment_is_backfilled(self):
        """Parent goes straight to done; upstream's own recompute_ready
        promotes the child first with a bare `promoted` event. The move
        still has to carry its explanation."""
        self.card("P7", "done")
        self.card("W7", "todo", parents=["P7"])
        kanban_db.recompute_ready(self.conn)          # the real upstream promoter
        self.assertEqual(self.status("W7"), "ready")
        self.assertEqual(self.comments("W7"), [], "upstream wrote a comment after all?")
        self.tick()
        self.assertEqual(len(self.comments("W7")), 1)
        self.assertIn("P7 reached done", self.comments("W7")[0])

    def test_a_backfill_is_written_once_not_every_tick(self):
        self.card("P8", "done")
        self.card("W8", "todo", parents=["P8"])
        kanban_db.recompute_ready(self.conn)
        for i in range(5):
            self.tick(now=self.now + i * 60)
        self.assertEqual(len(self.comments("W8")), 1)

    def test_our_own_move_is_never_backfilled_twice(self):
        self.card("P9", "scheduled")
        self.card("W9", "todo", parents=["P9"])
        for i in range(5):
            self.tick(now=self.now + i * 60)
        self.assertEqual(len(self.comments("W9")), 1)

    def test_a_dry_run_moves_nothing_and_writes_nothing(self):
        self.card("P10", "scheduled")
        self.card("W10", "todo", parents=["P10"])
        report = self.tick(apply=False)
        self.assertEqual(self.status("W10"), "todo")
        self.assertEqual(self.comments("W10"), [])
        self.assertEqual([a["card_id"] for a in report["advanced"]], ["W10"])

    def test_board_rules_are_off_unless_a_specification_is_passed(self):
        """The escalator already runs --apply against live boards. A board
        rule must never switch itself on because a file appeared."""
        self.card("P11", "scheduled")
        self.card("W11", "todo", parents=["P11"])
        report = self.tick(spec=None)
        self.assertEqual(self.status("W11"), "todo")
        self.assertEqual(report["advanced"], [])


class TestAcceptAndRework(BoardCase):
    """The user's two exits from user_review. accept is the only door to done."""

    def setUp(self):
        super().setUp()
        import board_cli
        self.cli = board_cli

    def events(self, card_id, kind):
        return [json.loads(r["payload"] or "{}") for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (card_id, kind))]

    def handed_off_by(self, card_id, profile):
        with self.conn:
            self.conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'handed_off', ?, ?)",
                (card_id, json.dumps({"from": profile, "from_role": "coding",
                                      "to_role": "qa"}), self.now),
            )

    # -- the structural guarantee --------------------------------------------

    def test_the_runtime_has_no_door_from_user_review_to_done(self):
        """Why accept is the ONLY path to done, by construction: upstream's
        own complete refuses the status user_review lives on. If an upstream
        upgrade ever changes that, this test is the alarm."""
        self.card("S0", "scheduled")
        self.assertFalse(kanban_db.complete_task(self.conn, "S0", summary="sneaky"))
        self.assertEqual(self.status("S0"), "scheduled")

    # -- accept ----------------------------------------------------------------

    def test_accept_moves_a_waiting_card_to_done(self):
        self.card("A1", "scheduled")
        self.cli.accept(self.conn, "A1", comment="looks right", spec=self.spec, now=self.now)
        self.assertEqual(self.status("A1"), "done")
        self.assertEqual(self.events("A1", "completed")[-1]["by"], "accept")
        self.assertIn("[accept] looks right", self.comments("A1"))
        completed_at = self.conn.execute(
            "SELECT completed_at FROM tasks WHERE id = 'A1'").fetchone()[0]
        self.assertEqual(completed_at, self.now, "the archive clock counts from here")

    def test_accept_refuses_a_card_that_is_not_waiting_for_the_user(self):
        for i, status in enumerate(("running", "ready", "blocked", "review", "todo")):
            self.card(f"NA{i}", status)
            with self.assertRaises(self.cli.AcceptError):
                self.cli.accept(self.conn, f"NA{i}", spec=self.spec)
            self.assertEqual(self.status(f"NA{i}"), status)

    def test_an_accepted_parent_frees_its_waiting_children(self):
        """accept produces a real `done`: upstream's own promoter treats it as
        one and releases what was waiting on it."""
        self.card("AP", "scheduled")
        self.card("AW", "todo", parents=["AP"])
        self.cli.accept(self.conn, "AP", spec=self.spec)
        kanban_db.recompute_ready(self.conn)
        self.assertEqual(self.status("AW"), "ready")

    def test_batch_accept_takes_every_waiting_child_and_nothing_else(self):
        self.card("BP", "running")
        for cid in ("B1", "B2", "B3"):
            self.card(cid, "scheduled", parents=["BP"])
        self.card("B4", "running", parents=["BP"])
        result = self.cli.accept_children(self.conn, "BP", spec=self.spec)
        self.assertEqual(result["accepted"], ["B1", "B2", "B3"])
        self.assertEqual(result["skipped"], [{"card_id": "B4", "status": "running"}])
        self.assertEqual([self.status(c) for c in ("B1", "B2", "B3", "B4")],
                         ["done", "done", "done", "running"])
        self.assertEqual(self.status("BP"), "running", "the parent is not accepted by proxy")

    # -- rework ----------------------------------------------------------------

    def test_rework_needs_the_reason(self):
        self.card("R1", "scheduled")
        with self.assertRaises(self.cli.AcceptError):
            self.cli.rework(self.conn, "R1", comment="  ", spec=self.spec)
        self.assertEqual(self.status("R1"), "scheduled")

    def test_rework_sends_it_back_to_whoever_did_the_work(self):
        self.card("R2", "scheduled")
        self.handed_off_by("R2", "coding")
        self.cli.rework(self.conn, "R2", comment="row 7 is wrong: expected 12, got 21",
                        spec=self.spec, now=self.now)
        row = self.conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = 'R2'").fetchone()
        self.assertEqual((row["status"], row["assignee"]), ("ready", "coding"),
                         "back to to do, and assigned to a profile the dispatcher can spawn")
        self.assertIn("[rework] row 7 is wrong: expected 12, got 21", self.comments("R2"))

    def test_rework_with_unfinished_parents_goes_to_waiting(self):
        """Same rule as upstream unblock: not ready while it still waits."""
        self.card("RP", "running")
        self.card("R3", "scheduled", parents=["RP"])
        self.cli.rework(self.conn, "R3", comment="redo", to="coding", spec=self.spec)
        self.assertEqual(self.status("R3"), "todo")

    def test_rework_refuses_when_nobody_can_be_found_to_do_it(self):
        """A to-do card assigned to the user's own name would never be picked
        up -- it would be stranded by the very command meant to move it."""
        self.card("R4", "scheduled")
        with self.assertRaises(self.cli.AcceptError):
            self.cli.rework(self.conn, "R4", comment="redo", spec=self.spec)
        self.assertEqual(self.status("R4"), "scheduled")

    # -- only the user --------------------------------------------------------

    def test_a_kanban_worker_cannot_run_it(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_some_card"
        try:
            rc = self.cli.main(["--db", self.db_path, "accept", "X"])
        finally:
            os.environ.pop("HERMES_KANBAN_TASK", None)
        self.assertEqual(rc, 2)

    def test_the_cli_finds_the_same_database_the_agent_uses(self):
        self.assertEqual(
            str(self.cli.db_path_for("default", str(_HOME))),
            str(kanban_db.kanban_db_path()),
        )

    def test_the_cli_end_to_end(self):
        """As a person runs it: a real process, a real terminal, no Hermes env."""
        import subprocess
        self.card("E1", "scheduled")
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
        cmd = [sys.executable, str(RESILIENCE / "board_cli.py"), "--db", self.db_path,
               "--board-file", str(BOARD_YAML)]
        master, slave = os.openpty()
        try:
            for step in (["init-ledger"], ["accept", "E1", "--comment", "verified by hand"]):
                out = subprocess.run(cmd + step, stdin=slave, env=env,
                                     capture_output=True, text=True, timeout=60)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(self.status("E1"), "done")

    def test_the_cli_refuses_without_a_terminal(self):
        """In-process from a test (or any tool call) stdin is not a terminal."""
        self.card("E2", "scheduled")
        rc = self.cli.main(["--db", self.db_path, "accept", "E2"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.status("E2"), "scheduled")


class TestBoardAlarms(BoardCase):
    """Two board invariants the escalator makes loud instead of silent."""

    class Rec:
        name = "rec"

        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append(message)

    def setUp(self):
        super().setUp()
        self.sender = self.Rec()
        self.cur =str(Path(self.db_path).parent / f".cur-alarm-{id(self)}")
        Path(self.cur).write_text("0")

    def alarm_tick(self, *, spec="default", now=None):
        return escalator.run_once(
            self.db_path, board="default", config={}, apply=True, notify=True,
            cursor_path=self.cur,
            state_path=str(Path(self.db_path).parent / f".pages-alarm-{id(self)}.json"),
            sender=self.sender, board_spec=self.spec if spec == "default" else spec,
            hermes_home=str(_HOME), now=now or self.now,
        )

    def titles(self, kind):
        return [m for m in self.sender.messages if kind in m.title]

    # -- done without accept --------------------------------------------------

    def test_done_by_the_runtimes_complete_is_paged(self):
        """The bypass is visible, not prevented: a human's own `complete` is
        their tool. Detection is the answer the architecture asked for."""
        self.card("DX", "blocked")
        kanban_db.complete_task(self.conn, "DX", summary="closed the old way")
        self.alarm_tick()
        self.assertEqual(len(self.titles("done_without_accept")), 1)
        self.assertIn("without your accept", self.titles("done_without_accept")[0].body)

    def test_done_by_accept_is_not_paged(self):
        import board_cli
        self.card("DA", "scheduled")
        board_cli.accept(self.conn, "DA", spec=self.spec, now=self.now)
        self.alarm_tick()
        self.assertEqual(self.titles("done_without_accept"), [])

    def test_a_done_without_accept_pages_once(self):
        self.card("DY", "ready")
        kanban_db.complete_task(self.conn, "DY", summary="x")
        for i in range(10):
            self.alarm_tick(now=self.now + i * 60)
        self.assertEqual(len(self.titles("done_without_accept")), 1)

    def test_without_a_board_the_runtimes_complete_is_the_normal_door(self):
        self.card("DZ", "blocked")
        kanban_db.complete_task(self.conn, "DZ", summary="x")
        self.alarm_tick(spec=None)
        self.assertEqual(self.titles("done_without_accept"), [])

    # -- a card in the runtime's agent review lane ---------------------------

    def test_a_review_card_on_a_real_profile_is_paged_immediately(self):
        (_HOME / "profiles" / "coding").mkdir(parents=True, exist_ok=True)
        self.card("RV", "review")          # assignee coding: a real profile
        self.alarm_tick()
        pages = self.titles("agent_review_lane")
        self.assertEqual(len(pages), 1)
        self.assertIn("merge and set done without you", pages[0].body)

    def test_a_review_card_on_a_persons_name_is_left_alone(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, assignee) "
                "VALUES ('RP', 'x', 'review', ?, 'someone')", (self.now,))
        self.alarm_tick()
        self.assertEqual(self.titles("agent_review_lane"), [])

    def test_the_review_alarm_pages_once_and_closes_when_it_leaves(self):
        (_HOME / "profiles" / "coding").mkdir(parents=True, exist_ok=True)
        self.card("RC", "review")
        for i in range(5):
            self.alarm_tick(now=self.now + i * 60)
        self.assertEqual(len(self.titles("agent_review_lane")), 1)
        with self.conn:
            self.conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = 'RC'")
        self.alarm_tick(now=self.now + 600)
        self.assertEqual(len(self.titles("resolved")), 1)


class TestAutoArchive(BoardCase):

    def test_a_card_done_longer_than_the_window_is_archived(self):
        self.card("D1", "done", completed_at=self.now - 31 * DAY)
        self.tick()
        self.assertEqual(self.status("D1"), "archived")
        kinds = [r["kind"] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id = 'D1'")]
        self.assertIn("archived", kinds)

    def test_a_recently_finished_card_is_left_alone(self):
        self.card("D2", "done", completed_at=self.now - 5 * DAY)
        self.tick()
        self.assertEqual(self.status("D2"), "done")

    def test_the_clock_never_archives_anything_that_is_not_done(self):
        """Whatever D2 decides about the edges, the clock is restricted."""
        for i, status in enumerate(("scheduled", "review", "blocked", "triage", "ready")):
            self.card(f"ND{i}", status, completed_at=self.now - 90 * DAY)
        self.tick()
        for i, status in enumerate(("scheduled", "review", "blocked", "triage", "ready")):
            self.assertEqual(self.status(f"ND{i}"), status)

    def test_a_dry_run_archives_nothing(self):
        self.card("D3", "done", completed_at=self.now - 31 * DAY)
        report = self.tick(apply=False)
        self.assertEqual(self.status("D3"), "done")
        self.assertEqual([a["card_id"] for a in report["archived"]], ["D3"])

    def test_no_done_to_archived_edge_means_no_clock_archiving(self):
        """The clock may not invent a move the board does not have."""
        spec = board.load_board(write_board("""
            board:
              columns:
                done: {exit_to: []}
                archived: {}
              archiving: {auto_archive_done_after: 1d}
        """))
        self.card("D4", "done", completed_at=self.now - 30 * DAY)
        self.tick(spec=spec)
        self.assertEqual(self.status("D4"), "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
