#!/usr/bin/env python3
"""Tests for scripts/resilience -- wait budget, counting policy, escalation.

Four behaviours, matching the four things that went wrong on a real board:

  1. A WAIT-CLASS error is retried past the OLD count limit and does NOT
     increment the card's consecutive_failures counter.
  2. Budget exhaustion escalates -- a human is told, with the resume command.
  3. A REAL failure still trips the breaker. The fix must not disarm it.
  4. The escalation message renders correctly, with the sender stubbed.

Runs against the REAL hermes-agent kanban_db in a scratch HERMES_HOME (it
hard-refuses the real ~/.hermes), following the same isolation contract as
test_kanban_harness.py. Nothing leaves the box: every test uses a recording
sender, never a real channel, and no test opens a socket.

Exit 77 = prerequisites missing (the wrapper reports SKIP).
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESILIENCE = REPO / "scripts" / "resilience"
AGENT_SRC = Path(os.path.expanduser(
    os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent"))).resolve()

# A stand-in local backend URL. Deliberately NOT a real backend port: these
# tests only ever parse the string, never connect, and a real port in a
# fixture invites someone to think otherwise.
LOCAL_BACKEND = "http://127.0.0.1:4242"

# --- isolation: scratch HERMES_HOME, set BEFORE any hermes import ------------
_HOME = Path(tempfile.mkdtemp(prefix="resilience-test-")).resolve()
_REAL = Path(os.path.expanduser("~/.hermes")).resolve()
if _HOME == _REAL or _REAL in _HOME.parents:
    sys.exit("refusing: scratch HERMES_HOME resolves inside the real ~/.hermes")
for key in [k for k in os.environ if k.startswith("HERMES_KANBAN")] + ["HERMES_PROFILE"]:
    os.environ.pop(key, None)
os.environ["HERMES_HOME"] = str(_HOME)

# Re-pinned per module: under one pytest process every test file is imported
# before any test runs, so the import-time setting above is overwritten by
# whichever file came last, and two suites would share one board.
_SAVED = {}


def setUpModule():
    _SAVED["HERMES_HOME"] = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(_HOME)


def tearDownModule():
    if _SAVED.get("HERMES_HOME") is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = _SAVED["HERMES_HOME"]


sys.path.insert(0, str(RESILIENCE))

import error_policy          # noqa: E402
import escalation            # noqa: E402
import escalator             # noqa: E402
import failure_policy        # noqa: E402
from wait_budget import (    # noqa: E402
    DEFAULT_WAIT_BUDGET_SECONDS,
    WaitBudget,
    WaitBudgetInvariantError,
    parse_duration,
    validate_budget,
)

# --- the real agent kanban_db ------------------------------------------------
if not (AGENT_SRC / "hermes_cli" / "kanban_db.py").exists():
    print(f"SKIP hermes-agent source not found at {AGENT_SRC} (set HERMES_AGENT_SRC)")
    sys.exit(77)
sys.path.insert(0, str(AGENT_SRC))
try:
    from hermes_cli import kanban_db  # noqa: E402
except Exception as exc:  # pragma: no cover
    print(f"SKIP cannot import hermes_cli.kanban_db: {exc}")
    sys.exit(77)


# --- helpers -----------------------------------------------------------------

class RecordingSender(escalation.Sender):
    """A stubbed channel. Nothing leaves the box; every message is kept."""

    name = "recording"

    def __init__(self, fail: bool = False):
        self.messages = []
        self.fail = fail

    def send(self, message):
        if self.fail:
            raise escalation.EscalationError("channel down (stub)")
        self.messages.append(message)


class FakeClock:
    """A monotonic clock the test drives, so a 12h budget expires instantly."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def fresh_board():
    """A real kanban.db, created by the real agent code, in the scratch home.

    Torn down and rebuilt per test so one test's cards can never make another
    test pass (or fail)."""
    db_path = kanban_db.kanban_db_path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    db_path = kanban_db.init_db()   # creates the schema, returns the path used
    conn = kanban_db.connect(db_path)
    return conn, str(db_path)


def add_card(conn, card_id: str, title: str, status: str = "ready") -> None:
    now = int(time.time())
    with conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, assignee) "
            "VALUES (?, ?, ?, ?, 'coder')",
            (card_id, title, status, now),
        )


def failures_of(conn, card_id: str) -> int:
    row = conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id = ?", (card_id,)
    ).fetchone()
    return int(row["consecutive_failures"])


def status_of(conn, card_id: str) -> str:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (card_id,)).fetchone()
    return row["status"]


def event_kinds(conn, card_id: str):
    return [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (card_id,)
        )
    ]


# =============================================================================
# A. the taxonomy
# =============================================================================

class TestErrorTaxonomy(unittest.TestCase):

    def test_local_mid_stream_disconnect_is_backend_busy_not_timeout(self):
        """The observed failure: the local proxy aborted the stream.

        Upstream calls it a timeout. It is not a stall, it is no capacity."""
        verdict = error_policy.refine(
            "timeout",
            "RemoteProtocolError: upstream disconnected mid-stream",
            base_url=LOCAL_BACKEND,
        )
        self.assertEqual(verdict.reason, error_policy.BACKEND_BUSY)
        self.assertTrue(verdict.wait_class)
        self.assertEqual(verdict.source, "refined")

    def test_request_parked_behind_a_model_swap_is_backend_busy(self):
        verdict = error_policy.refine(
            "server_error",
            "HTTP 503: no router for requested model (model swap in progress)",
            base_url="http://localhost:4242/v1",
        )
        self.assertEqual(verdict.reason, error_policy.BACKEND_BUSY)

    def test_remote_provider_disconnect_stays_timeout(self):
        """backend_busy is a statement about a LOCAL queue. A remote provider
        dropping a stream must keep its existing behaviour."""
        verdict = error_policy.refine(
            "timeout",
            "upstream disconnected mid-stream",
            base_url="https://api.example-provider.com",
        )
        self.assertEqual(verdict.reason, "timeout")
        self.assertTrue(verdict.wait_class)
        self.assertEqual(verdict.source, "upstream")

    def test_a_genuine_stall_stays_timeout(self):
        verdict = error_policy.refine(
            "timeout", "Read timed out after 900s", base_url=LOCAL_BACKEND,
        )
        self.assertEqual(verdict.reason, "timeout")

    def test_the_two_sets_are_exactly_as_specified(self):
        self.assertEqual(
            set(error_policy.WAIT_CLASS_REASONS),
            {"overloaded", "server_error", "timeout", "rate_limit", "backend_busy"},
        )
        self.assertEqual(
            set(error_policy.REAL_REASONS),
            {"auth_permanent", "billing", "context_overflow",
             "payload_too_large", "model_not_found", "provider_policy_blocked"},
        )
        self.assertFalse(error_policy.WAIT_CLASS_REASONS & error_policy.REAL_REASONS)

    def test_real_reasons_are_not_wait_class(self):
        for reason in error_policy.REAL_REASONS:
            with self.subTest(reason=reason):
                self.assertFalse(error_policy.refine(reason, "boom").wait_class)

    def test_unknown_is_treated_as_real_not_waited_on(self):
        """Fail safe: an error we do not recognise surfaces to a human rather
        than silently consuming a twelve-hour budget."""
        self.assertFalse(error_policy.refine("unknown", "???").wait_class)
        self.assertFalse(error_policy.refine("auth", "401").wait_class)

    def test_classify_uses_the_real_agent_classifier(self):
        """Not a stub: the real classifier runs and its answer is refined."""
        import httpx  # the agent's transport; its errors are what we see live

        verdict = error_policy.classify(
            httpx.RemoteProtocolError("upstream disconnected mid-stream"),
            base_url=LOCAL_BACKEND,
        )
        self.assertEqual(verdict.source, "refined")
        self.assertEqual(verdict.reason, error_policy.BACKEND_BUSY)


# =============================================================================
# B. the wait budget
# =============================================================================

class TestWaitBudget(unittest.TestCase):

    def test_shipped_default_is_thirty_minutes(self):
        self.assertEqual(DEFAULT_WAIT_BUDGET_SECONDS, 1800)
        self.assertEqual(WaitBudget().budget_seconds, 1800)

    def test_durations_parse(self):
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("12h"), 43200)
        self.assertEqual(parse_duration(1800), 1800)
        self.assertEqual(parse_duration("90"), 90)

    def test_invariant_budget_must_be_inside_the_card_runtime_cap(self):
        validate_budget(parse_duration("30m"), parse_duration("4h"))  # fine
        with self.assertRaises(WaitBudgetInvariantError):
            validate_budget(parse_duration("12h"), parse_duration("4h"))
        with self.assertRaises(WaitBudgetInvariantError):
            validate_budget(parse_duration("4h"), parse_duration("4h"))  # equal

    def test_invariant_is_enforced_at_construction(self):
        with self.assertRaises(WaitBudgetInvariantError):
            WaitBudget("12h", max_runtime_seconds="4h")
        # A 12h budget under a 24h card cap is legal.
        self.assertEqual(
            WaitBudget("12h", max_runtime_seconds="24h").budget_seconds, 43200
        )

    def test_wait_class_retries_far_past_the_old_count_limit(self):
        """THE regression. Three retries used to be the whole of patience."""
        clock = FakeClock()
        budget = WaitBudget("12h", clock=clock)
        verdict = error_policy.refine(
            "timeout", "upstream disconnected mid-stream", base_url=LOCAL_BACKEND,
        )

        attempts = 0
        decision = None
        for _ in range(5000):
            decision = budget.should_retry(verdict, retry_count=attempts, max_retries=3)
            if not decision.retry:
                break
            attempts += 1
            clock.advance(60)  # a minute of jittered backoff per attempt

        self.assertGreater(attempts, 3, "gave up at the old retry count")
        self.assertEqual(attempts, 720, "should wait the full 12h, one attempt/min")
        self.assertEqual(decision.basis, "budget_exhausted")

    def test_wait_class_decisions_never_cite_the_retry_count(self):
        budget = WaitBudget("30m", clock=FakeClock())
        decision = budget.should_retry("backend_busy", retry_count=999, max_retries=3)
        self.assertTrue(decision.retry)
        self.assertEqual(decision.basis, "wait_budget")

    def test_real_failures_keep_the_count_limit(self):
        budget = WaitBudget("12h", clock=FakeClock())
        for count in range(3):
            self.assertTrue(budget.should_retry("billing", count, 3).retry)
        decision = budget.should_retry("billing", 3, 3)
        self.assertFalse(decision.retry)
        self.assertEqual(decision.basis, "retries_exhausted")

    def test_budget_does_not_start_until_the_first_wait_class_failure(self):
        budget = WaitBudget("30m", clock=FakeClock())
        self.assertFalse(budget.started)
        budget.should_retry("auth_permanent", 0, 3)
        self.assertFalse(budget.started, "a real failure must not spend the budget")
        budget.should_retry("overloaded", 0, 3)
        self.assertTrue(budget.started)

    def test_success_resets_the_budget(self):
        clock = FakeClock()
        budget = WaitBudget("30m", clock=clock)
        budget.should_retry("rate_limit", 0, 3)
        clock.advance(1700)
        budget.reset()
        clock.advance(1000)
        self.assertTrue(budget.should_retry("rate_limit", 0, 3).retry)

    def test_sleep_is_clamped_to_the_remaining_budget(self):
        clock = FakeClock()
        budget = WaitBudget("30m", clock=clock)
        budget.should_retry("timeout", 0, 3)
        clock.advance(1790)
        self.assertAlmostEqual(budget.cap_sleep(120), 10.0, places=3)


# =============================================================================
# C. failure counting as policy
# =============================================================================

class TestFailurePolicy(unittest.TestCase):

    def test_shipped_defaults_are_conservative(self):
        """Everything counts out of the box -- identical to today's behaviour."""
        policy = failure_policy.resolve_policy({})
        self.assertTrue(all(policy[k] for k in failure_policy.KNOWN_OUTCOMES))

    def test_a_local_first_box_switches_the_machine_caused_ones_off(self):
        config = {"kanban": {"count_toward_breaker": {
            "crashed": True, "spawn_failed": False,
            "timed_out": False, "backend_busy": False,
        }}}
        self.assertTrue(failure_policy.should_count("crashed", config))
        for outcome in ("spawn_failed", "timed_out", "backend_busy"):
            with self.subTest(outcome=outcome):
                self.assertFalse(failure_policy.should_count(outcome, config))

    def test_an_outcome_nobody_configured_still_counts(self):
        self.assertTrue(failure_policy.should_count("something_new", {}))

    def test_yaml_string_booleans_are_honoured(self):
        config = {"kanban": {"count_toward_breaker": {"timed_out": "false"}}}
        self.assertFalse(failure_policy.should_count("timed_out", config))


# =============================================================================
# D. the communication agent
# =============================================================================

class TestEscalationMessage(unittest.TestCase):

    def _escalation(self, **kw):
        defaults = dict(
            card_id="CARD-42",
            title="Port the notifier to the new board layout",
            board="project-a",
            kind="gave_up",
            last_error="RemoteProtocolError: upstream disconnected mid-stream",
            worker_log="/home/u/.hermes/kanban/boards/project-a/logs/CARD-42.log",
            facts={"failures": 3, "effective_limit": 3},
        )
        defaults.update(kw)
        return escalation.Escalation(**defaults)

    def test_message_carries_all_six_required_facts(self):
        message = escalation.render(self._escalation())
        body = message.body
        self.assertIn("CARD-42", body)                            # 1. card id
        self.assertIn("Port the notifier", body)                  # 1. title
        self.assertIn("project-a", body)                          # 2. board
        self.assertIn("taken off the board", body)                # 3. plain words
        self.assertIn("upstream disconnected mid-stream", body)   # 4. last error
        self.assertIn("hermes kanban unblock CARD-42", body)      # 5. resume cmd
        self.assertIn("logs/CARD-42.log", body)                   # 6. worker log

    def test_plain_words_not_jargon(self):
        body = escalation.render(self._escalation()).body
        self.assertIn("will NOT come back on its own", body)
        for jargon in ("consecutive_failures", "recompute_ready", "FailoverReason"):
            self.assertNotIn(jargon, body)

    def test_backend_busy_says_whose_fault_it_was(self):
        body = escalation.render(self._escalation(kind="budget_exhausted")).body
        self.assertIn("machine's fault, not the card's", body)

    def test_stranded_card_gets_a_different_resume_command(self):
        esc = self._escalation(kind="stranded")
        self.assertIn("hermes kanban show", esc.resume_command)
        self.assertIn("nobody can run it", escalation.render(esc).body)

    def test_card_id_is_shell_quoted_in_the_resume_command(self):
        esc = self._escalation(card_id="weird id; rm -rf /")
        self.assertIn("'weird id; rm -rf /'", esc.resume_command)

    def test_long_errors_are_flattened_to_one_line(self):
        esc = self._escalation(last_error="line one\nline two\n" + "x" * 500)
        body = escalation.render(esc).body
        self.assertNotIn("line one\nline two", body)
        self.assertIn("...", body)

    def test_escalate_delivers_through_the_stubbed_sender(self):
        sender = RecordingSender()
        record = escalation.escalate(self._escalation(), {}, sender=sender)
        self.assertTrue(record["delivered"])
        self.assertEqual(len(sender.messages), 1)
        self.assertIn("CARD-42", sender.messages[0].title)

    def test_a_failed_send_is_reported_not_raised(self):
        record = escalation.escalate(
            self._escalation(), {}, sender=RecordingSender(fail=True),
        )
        self.assertFalse(record["delivered"])
        self.assertIn("channel down", record["error"])

    def test_channel_must_be_chosen_explicitly(self):
        with self.assertRaises(escalation.EscalationError):
            escalation.build_sender({"escalation": {}})
        with self.assertRaises(escalation.EscalationError):
            escalation.build_sender({"escalation": {"channel": "carrier-pigeon"}})

    def test_none_is_a_valid_explicit_channel(self):
        sender = escalation.build_sender({"escalation": {"channel": "none"}})
        self.assertIsInstance(sender, escalation.NullSender)

    def test_every_documented_channel_is_buildable(self):
        self.assertEqual(set(escalation.CHANNELS),
                         {"mattermost", "telegram", "ntfy", "none"})

    def test_no_token_is_hardcoded_anywhere_in_the_module(self):
        """The senders carry credential-resolution logic, never credentials."""
        source = (RESILIENCE / "escalation.py").read_text()
        for marker in ("bot6", "xox", "Bearer ey", "api.telegram.org/bot1"):
            self.assertNotIn(marker, source)

    def test_missing_credentials_name_every_source_tried(self):
        os.environ.pop("HERMES_ESCALATION_TELEGRAM_BOT_TOKEN", None)
        with self.assertRaises(escalation.EscalationError) as ctx:
            escalation.TelegramSender({})
        message = str(ctx.exception)
        self.assertIn("HERMES_ESCALATION_TELEGRAM_BOT_TOKEN", message)
        self.assertIn("keychain", message)


# =============================================================================
# E. end to end on a REAL board
# =============================================================================

class TestOnTheRealBoard(unittest.TestCase):
    """Drives the real hermes-agent kanban_db, the real circuit breaker, and
    the escalator reconciler against a scratch board."""

    def setUp(self):
        self.conn, self.db_path = fresh_board()
        self.addCleanup(self.conn.close)
        self.sender = RecordingSender()

    def _cursor(self):
        """A per-test cursor, seeded at 0.

        A real first run seeds the cursor at the current event tip so a fresh
        install does not replay history as a burst of notifications. Tests set
        up their events first, so they start from the beginning instead."""
        path = Path(self.db_path).parent / f".cursor-{self.id().split('.')[-1]}"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("0")
        return str(path)

    def _state(self):
        return str(Path(self.db_path).parent / f".pages-{self.id().split('.')[-1]}.json")

    def _run(self, *, apply=True, config=None, **kw):
        return escalator.run_once(
            self.db_path, board="default",
            config=config or {}, apply=apply,
            cursor_path=self._cursor(), state_path=self._state(),
            sender=self.sender, hermes_home=str(_HOME), **kw,
        )

    # -- 1. wait-class does not touch the card counter ----------------------

    def test_wait_class_retries_do_not_increment_the_card_counter(self):
        """A worker that waits out a busy backend must cost the card nothing.

        Drives the wait budget past the old count limit and asserts the real
        board never saw a failure."""
        add_card(self.conn, "WAIT-1", "waits for the model")
        clock = FakeClock()
        budget = WaitBudget("12h", clock=clock)
        verdict = error_policy.refine(
            "timeout", "upstream disconnected mid-stream", base_url=LOCAL_BACKEND,
        )

        attempts = 0
        while budget.should_retry(verdict, attempts, 3).retry and attempts < 50:
            attempts += 1
            clock.advance(60)
            # A wait-class attempt is NOT a task failure: nothing is recorded.

        self.assertEqual(attempts, 50, "the loop stopped waiting too early")
        self.assertEqual(failures_of(self.conn, "WAIT-1"), 0)
        self.assertEqual(status_of(self.conn, "WAIT-1"), "ready")
        self.assertNotIn("gave_up", event_kinds(self.conn, "WAIT-1"))

    # -- 2. budget exhaustion escalates -------------------------------------

    def test_budget_exhaustion_escalates_to_a_human(self):
        add_card(self.conn, "WAIT-2", "never got capacity")
        clock = FakeClock()
        budget = WaitBudget("30m", clock=clock)
        budget.should_retry("backend_busy", 0, 3)
        clock.advance(1801)
        decision = budget.should_retry("backend_busy", 1, 3)
        self.assertFalse(decision.retry)
        self.assertEqual(decision.basis, "budget_exhausted")

        # The worker records what happened, then the escalator tells a human.
        with self.conn:
            self.conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES ('WAIT-2', NULL, 'budget_exhausted', ?, ?)",
                (json.dumps({
                    "error": "upstream disconnected mid-stream",
                    "trigger_outcome": "backend_busy",
                    "budget_seconds": 1800,
                }), int(time.time())),
            )

        report = self._run()
        kinds = [e["kind"] for e in report["escalations"]]
        self.assertIn("budget_exhausted", kinds)
        body = self.sender.messages[0].body
        self.assertIn("WAIT-2", body)
        self.assertIn("machine's fault", body)
        self.assertIn("hermes kanban unblock WAIT-2", body)
        self.assertIn("WAIT-2.log", body)

    # -- 3. a real failure still trips the breaker --------------------------

    def test_a_real_failure_still_trips_the_breaker(self):
        """The fix must not disarm the circuit breaker. Uses the agent's own
        _record_task_failure, not a reimplementation."""
        add_card(self.conn, "REAL-1", "genuinely broken card", status="running")

        blocked = False
        for _ in range(2):
            blocked = kanban_db._record_task_failure(
                self.conn, "REAL-1", "AssertionError: the card's own test fails",
                outcome="crashed", failure_limit=2,
            )
        self.assertTrue(blocked, "the breaker did not trip")
        self.assertEqual(status_of(self.conn, "REAL-1"), "blocked")
        self.assertEqual(failures_of(self.conn, "REAL-1"), 2)
        self.assertIn("gave_up", event_kinds(self.conn, "REAL-1"))

        # crashed counts by default, so the escalator leaves it blocked...
        report = self._run()
        self.assertEqual(
            [r for r in report["reconciled"] if r["recovered"]], [],
            "a real failure was wrongly auto-recovered",
        )
        self.assertEqual(status_of(self.conn, "REAL-1"), "blocked")
        # ...and still tells a human, because no card sits silently.
        self.assertIn("gave_up", [e["kind"] for e in report["escalations"]])
        self.assertTrue(all(e["delivered"] for e in report["escalations"]))

    # -- 4. a machine-caused failure gives the card its life back -----------

    def test_machine_caused_failure_does_not_cost_the_card_a_life(self):
        add_card(self.conn, "MACH-1", "died on a busy backend", status="running")
        for _ in range(2):
            kanban_db._record_task_failure(
                self.conn, "MACH-1",
                "RemoteProtocolError: upstream disconnected mid-stream",
                outcome="crashed", failure_limit=2,
            )
        self.assertEqual(status_of(self.conn, "MACH-1"), "blocked")

        config = {"kanban": {"count_toward_breaker": {"backend_busy": False}}}
        report = self._run(config=config)

        recovered = [r for r in report["reconciled"] if r["recovered"]]
        self.assertEqual([r["card_id"] for r in recovered], ["MACH-1"])
        self.assertEqual(recovered[0]["trigger_outcome"], "backend_busy")
        self.assertEqual(status_of(self.conn, "MACH-1"), "ready")
        self.assertEqual(failures_of(self.conn, "MACH-1"), 0)
        self.assertIn("unblocked", event_kinds(self.conn, "MACH-1"))
        # A human is told anyway, and told it was put back.
        self.assertTrue(self.sender.messages)
        self.assertIn("auto_recovered", self.sender.messages[0].body)

    def test_recovered_card_is_promotable_again_by_the_real_recompute_ready(self):
        """The counter and the status must both be cleared: recompute_ready
        refuses to auto-recover a card still at its failure limit."""
        add_card(self.conn, "MACH-2", "should return to the board", status="running")
        for _ in range(2):
            kanban_db._record_task_failure(
                self.conn, "MACH-2", "upstream disconnected mid-stream",
                outcome="crashed", failure_limit=2,
            )
        self._run(config={"kanban": {"count_toward_breaker": {"backend_busy": False}}})
        self.assertEqual(failures_of(self.conn, "MACH-2"), 0)
        # Blocked again by something else -> the real promoter now takes it.
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = 'MACH-2'"
            )
        kanban_db.recompute_ready(self.conn, failure_limit=2)
        self.assertEqual(status_of(self.conn, "MACH-2"), "ready")

    # -- 5. stranded cards ---------------------------------------------------

    def test_a_stranded_card_is_escalated(self):
        old = int(time.time()) - 7200
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, assignee) "
                "VALUES ('STRAND-1', 'nobody can run me', 'ready', ?, 'coder')",
                (old,),
            )
        report = self._run(stranded_after=3600)
        stranded = [e for e in report["escalations"] if e["kind"] == "stranded"]
        self.assertEqual(len(stranded), 1)
        self.assertIn("nobody can run it", stranded[0]["body"])
        self.assertIn("idle_for=2h00m", stranded[0]["body"])

    def test_a_freshly_created_ready_card_is_not_stranded(self):
        add_card(self.conn, "FRESH-1", "just created")
        report = self._run(stranded_after=3600)
        self.assertEqual(
            [e for e in report["escalations"] if e["kind"] == "stranded"], []
        )

    # -- 5b. the two false pages the live dry run produced -------------------

    def _old_ready(self, card_id, title, assignee, age=7200, status="ready"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, assignee) "
                "VALUES (?, ?, ?, ?, ?)",
                (card_id, title, status, int(time.time()) - age, assignee),
            )

    def test_a_human_lane_card_is_never_stranded(self):
        """A review gate assigned to a person has no profile behind it, so no
        worker is ever meant to claim it. It is permanently ready and
        permanently fine -- paging about it is how a channel earns a mute."""
        (_HOME / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        self._old_ready("GATE-1", "review gate for the user", "user", age=90 * 86400)
        report = self._run(stranded_after=3600)
        self.assertEqual(
            [e for e in report["escalations"] if e["kind"] == "stranded"], [],
            "paged about a human-lane card",
        )
        reasons = [s["suppressed_because"] for s in report["suppressed"]]
        self.assertTrue(any("human lane" in r for r in reasons), reasons)

    def test_an_assignee_with_a_real_profile_is_still_stranded(self):
        """The human-lane rule must not swallow a genuinely stuck card."""
        (_HOME / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        self._old_ready("REALLY-1", "nobody picked me up", "coder")
        report = self._run(stranded_after=3600)
        self.assertIn("stranded", [e["kind"] for e in report["escalations"]])

    def test_a_card_queued_behind_capacity_is_not_stranded(self):
        """max_in_progress=1 with another worker running means the card is
        next in line, not stuck. 'Nobody can run it', not 'nobody has yet'."""
        (_HOME / "profiles" / "qa-tester").mkdir(parents=True, exist_ok=True)
        self._old_ready("QUEUED-1", "waiting its turn", "qa-tester")
        self._old_ready("BUSY-1", "currently running", "qa-tester", status="running")
        report = self._run(stranded_after=3600, max_in_progress=1)
        self.assertEqual(
            [e for e in report["escalations"] if e["kind"] == "stranded"], [],
            "paged about a card that was merely queued",
        )
        reasons = [s["suppressed_because"] for s in report["suppressed"]]
        self.assertTrue(any("at capacity" in r for r in reasons), reasons)

    def test_a_full_board_with_an_idle_assignee_is_still_stranded(self):
        """Both halves are required. A full board whose worker belongs to a
        DIFFERENT assignee means this card really is being passed over."""
        for name in ("coder", "qa-tester"):
            (_HOME / "profiles" / name).mkdir(parents=True, exist_ok=True)
        self._old_ready("PASSED-1", "being passed over", "qa-tester")
        self._old_ready("OTHER-1", "someone else's work", "coder", status="running")
        report = self._run(stranded_after=3600, max_in_progress=1)
        self.assertIn("stranded", [e["kind"] for e in report["escalations"]])

    def test_a_card_held_by_the_concurrency_cap_says_so_and_names_the_holder(self):
        """'No worker' with a healthy box sent a reader bisecting for an hour:
        the dispatcher was at max_in_progress and said nothing. The page names
        the cap and the card holding it."""
        for name in ("coder", "qa-tester"):
            (_HOME / "profiles" / name).mkdir(parents=True, exist_ok=True)
        self._old_ready("HELD-1", "waiting on the cap", "qa-tester")
        self._old_ready("HOLDER-1", "the one running", "coder", status="running")
        report = self._run(stranded_after=3600, max_in_progress=1)
        page = [e for e in report["escalations"] if e["kind"] == "stranded"]
        self.assertEqual(len(page), 1)
        self.assertIn("held by the concurrency cap (1 running: HOLDER-1)", page[0]["body"])

    def test_a_card_queued_behind_its_profiles_own_cap_is_not_stranded(self):
        """kanban.max_in_progress_per_profile: 1 and that profile already
        running means the card is next in line for its role."""
        (_HOME / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        self._old_ready("ROLEQ-1", "next for coder", "coder")
        self._old_ready("ROLEBUSY-1", "coder at work", "coder", status="running")
        report = self._run(stranded_after=3600, max_in_progress=5,
                           config={"kanban": {"max_in_progress_per_profile": 1}})
        self.assertEqual(
            [e for e in report["escalations"] if e["kind"] == "stranded"], [])
        reasons = [s["suppressed_because"] for s in report["suppressed"]]
        self.assertTrue(any("per-profile cap" in r for r in reasons), reasons)

    def test_a_ready_card_with_a_stale_claim_lock_is_named_repaired_and_then_claimed(self):
        """Upstream's claim requires claim_lock IS NULL, so a ready card still
        carrying an expired lock is skipped by every real dispatch tick and
        lands in no bucket, while a dry run promises to spawn it. The
        reconciler names it, clears the lock, and the next dispatch claims it."""
        (_HOME / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        self._old_ready("LOCKED-1", "holds an old lock", "coder")
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET claim_lock = 'dead-worker', claim_expires = ? "
                "WHERE id = 'LOCKED-1'", (int(time.time()) - 3600,))
        spawned = []
        before = kanban_db.dispatch_once(self.conn, spawn_fn=lambda t, ws, board=None:
                                         spawned.append(t.id))
        self.assertEqual(spawned, [], "precondition: upstream cannot claim it")
        self.assertNotIn("LOCKED-1", [s[0] for s in before.spawned])

        report = self._run(stranded_after=3600)
        pages = [e for e in report["escalations"] if e.get("card_id") == "LOCKED-1"]
        self.assertTrue(any("stale claim lock" in e["body"] for e in pages),
                        [e["body"][:200] for e in pages])
        row = self.conn.execute(
            "SELECT claim_lock FROM tasks WHERE id = 'LOCKED-1'").fetchone()
        self.assertIsNone(row["claim_lock"], "the reconciler did not clear the lock")
        self.assertIn("claim_cleared", event_kinds(self.conn, "LOCKED-1"))

        kanban_db.dispatch_once(self.conn, spawn_fn=lambda t, ws, board=None:
                                spawned.append(t.id))
        self.assertEqual(spawned, ["LOCKED-1"])

    def test_a_card_blocked_by_the_upstream_double_count_says_so(self):
        """Upstream b88d0007c subtracts running cards twice when
        kanban.max_in_progress is set: below the cap, with anything running,
        the dispatcher still spawns nothing and fills no bucket. The page names
        it and the configuration that avoids it."""
        for name in ("coder", "qa-tester"):
            (_HOME / "profiles" / name).mkdir(parents=True, exist_ok=True)
        self._old_ready("DOUBLE-1", "below the cap, never spawned", "coder")
        self._old_ready("RUN-1", "the one running", "qa-tester", status="running")
        report = self._run(stranded_after=3600, max_in_progress=2)
        page = [e for e in report["escalations"] if e["kind"] == "stranded"]
        self.assertEqual(len(page), 1)
        self.assertIn("counts running cards twice", page[0]["body"])
        self.assertIn("max_in_progress: null", page[0]["body"])

    def _run_history(self, card_id, durations, *, outcome="completed"):
        now = int(time.time())
        with self.conn:
            for i, d in enumerate(durations):
                start = now - 86400 + i * 10000
                self.conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, started_at, "
                    "ended_at, outcome) VALUES (?, 'coder', 'done', ?, ?, ?)",
                    (card_id, start, start + d, outcome))

    def _overrunning(self, card_id, elapsed):
        """A running card whose worker keeps producing events (so the 45-minute
        no-progress rule stays quiet) while its run is far past its history."""
        now = int(time.time())
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, started_at, assignee, "
                "worker_pid, last_heartbeat_at, claim_lock, claim_expires) "
                "VALUES (?, 'busy going nowhere', 'running', ?, ?, 'coder', 4242, ?, 'l', ?)",
                (card_id, now - 90000, now - elapsed, now - 30, now + 3600))
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) "
                "VALUES (?, 'coder', 'running', ?)", (card_id, now - elapsed))
            self.conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'commented', '{}', ?)", (card_id, now - 60))

    def test_a_run_far_past_the_cards_own_history_is_stalled(self):
        """A heartbeat proves a process lives, and a stream of events can hide
        a loop. A run at several times the median of the card's earlier
        successful runs is a free second signal."""
        self._run_history("SLOW-1", [2300, 2400, 2500])
        self._overrunning("SLOW-1", elapsed=4 * 3600)
        report = self._run()
        page = [e for e in report["escalations"] if e.get("card_id") == "SLOW-1"]
        self.assertEqual([e["kind"] for e in page], ["stalled"])
        self.assertIn("past this card's usual", page[0]["body"])

    def test_a_run_within_its_history_or_without_one_is_not_stalled(self):
        self._run_history("OK-1", [2300, 2400, 2500])
        self._overrunning("OK-1", elapsed=3000)
        self._overrunning("NOHIST-1", elapsed=4 * 3600)
        report = self._run()
        self.assertEqual([e for e in report["escalations"]
                          if e.get("card_id") in ("OK-1", "NOHIST-1")], [])

    def _silent_worker(self, card_id, *, spawned_ago, log_written_ago):
        """A running card whose heartbeat is fresh and whose worker log has not
        been written since shortly after spawn."""
        now = int(time.time())
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, started_at, assignee, "
                "worker_pid, last_heartbeat_at, claim_lock, claim_expires) "
                "VALUES (?, 'never came up', 'running', ?, ?, 'coder', 4343, ?, 'l', ?)",
                (card_id, now - spawned_ago, now - spawned_ago, now - 30, now + 3600))
            self.conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'commented', '{}', ?)", (card_id, now - 60))
        log = Path(escalator.worker_log_path("default", card_id, str(_HOME)))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Query: work kanban task\nInitializing agent...\n")
        os.utime(log, (now - log_written_ago, now - log_written_ago))

    def test_a_worker_whose_log_never_grew_is_named_as_never_started(self):
        """A heartbeat can be emitted for an agent that never initialised: the
        log stops at 'Initializing agent...' while the card looks healthy."""
        self._silent_worker("SILENT-1", spawned_ago=2 * 3600, log_written_ago=2 * 3600 - 5)
        report = self._run()
        page = [e for e in report["escalations"] if e.get("card_id") == "SILENT-1"]
        self.assertEqual([e["kind"] for e in page], ["stalled"])
        self.assertIn("worker log has not been written", page[0]["body"])

    def test_a_worker_writing_its_log_is_not_silent(self):
        self._silent_worker("TALKING-1", spawned_ago=2 * 3600, log_written_ago=60)
        report = self._run()
        self.assertEqual([e for e in report["escalations"]
                          if e.get("card_id") == "TALKING-1"], [])

    def test_a_live_claim_lock_is_left_alone(self):
        (_HOME / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        self._old_ready("LIVE-1", "claim not yet expired", "coder")
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET claim_lock = 'live', claim_expires = ? "
                "WHERE id = 'LIVE-1'", (int(time.time()) + 3600,))
        self._run(stranded_after=3600)
        row = self.conn.execute("SELECT claim_lock FROM tasks WHERE id = 'LIVE-1'").fetchone()
        self.assertEqual(row["claim_lock"], "live")

    def test_a_card_below_the_cap_does_not_blame_the_cap(self):
        (_HOME / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        self._old_ready("FREE-1", "nothing running", "coder")
        report = self._run(stranded_after=3600, max_in_progress=2)
        page = [e for e in report["escalations"] if e["kind"] == "stranded"]
        self.assertEqual(len(page), 1)
        self.assertNotIn("concurrency cap", page[0]["body"])

    def test_an_ignore_rule_suppresses_by_id_assignee_or_title(self):
        (_HOME / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        self._old_ready("IGN-1", "by id", "coder")
        self._old_ready("IGN-2", "DO NOT DISPATCH: parked", "coder")
        config = {"escalation": {"ignore": {
            "card_ids": ["IGN-1"],
            "title_patterns": ["*do not dispatch*"],
        }}}
        report = self._run(stranded_after=3600, config=config)
        self.assertEqual(
            [e for e in report["escalations"] if e["kind"] == "stranded"], []
        )
        self.assertEqual(len(report["suppressed"]), 2)

    # -- 5c. the deadlock the live dry run MISSED ----------------------------

    def _running_card(self, card_id, title, *, heartbeat_age, progress_age):
        """A running card with a fresh heartbeat and stale real progress."""
        now = int(time.time())
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, started_at, "
                "assignee, worker_pid, last_heartbeat_at, claim_lock, claim_expires) "
                "VALUES (?, ?, 'running', ?, ?, 'coder', 4242, ?, 'lock', ?)",
                (card_id, title, now - 86400, now - progress_age,
                 now - heartbeat_age, now + 3600),
            )
            self.conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'spawned', NULL, ?)",
                (card_id, now - progress_age),
            )
            # Heartbeats keep landing. They are the signal that lies.
            for age in range(0, min(progress_age, 600), 60):
                self.conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, NULL, 'heartbeat', NULL, ?)",
                    (card_id, now - age),
                )

    def test_a_running_card_with_a_live_but_idle_worker_is_escalated(self):
        """THE miss. Heartbeats every minute, healthy claim, board looks
        perfect -- and the session has been hung for hours."""
        self._running_card("HUNG-1", "looks healthy, going nowhere",
                           heartbeat_age=30, progress_age=5 * 3600 + 720)
        report = self._run(stalled_after=45 * 60)
        stalled = [e for e in report["escalations"] if e["kind"] == "stalled"]
        self.assertEqual(len(stalled), 1, "missed the live-but-idle worker")
        body = stalled[0]["body"]
        self.assertIn("worker is alive and still heartbeating", body)
        self.assertIn("no_progress_for=5h12m", body)
        self.assertIn("worker_pid=4242", body)
        self.assertIn("hermes kanban reclaim HUNG-1", body)

    def test_a_running_card_making_progress_is_not_stalled(self):
        self._running_card("BUSY-2", "actually working",
                           heartbeat_age=30, progress_age=120)
        report = self._run(stalled_after=45 * 60)
        self.assertEqual(
            [e for e in report["escalations"] if e["kind"] == "stalled"], []
        )

    def test_a_card_whose_heartbeats_stopped_is_not_stalled(self):
        """That is a crash, and the dispatcher's own reaper owns it. Claiming
        it here would double-page."""
        self._running_card("DEAD-1", "heartbeats stopped",
                           heartbeat_age=7200, progress_age=7200)
        report = self._run(stalled_after=45 * 60)
        self.assertEqual(
            [e for e in report["escalations"] if e["kind"] == "stalled"], []
        )

    def test_heartbeats_alone_never_count_as_progress(self):
        """The whole point: a heartbeat means the process lives, not that the
        work moves. A card with ONLY heartbeats since spawn is stalled."""
        self._running_card("HB-ONLY-1", "only heartbeats",
                           heartbeat_age=10, progress_age=3 * 3600)
        report = self._run(stalled_after=45 * 60)
        self.assertEqual(
            len([e for e in report["escalations"] if e["kind"] == "stalled"]), 1
        )

    def test_an_activity_probe_can_veto_a_stall(self):
        """A long think is not a hang. When the backend confirms traffic for
        this card, it is working -- do not page."""
        self._running_card("THINK-1", "a very long think",
                           heartbeat_age=10, progress_age=3 * 3600)
        seen = []

        def probe(tag):
            seen.append(tag)
            return True

        report = self._run(stalled_after=45 * 60, activity_probe=probe)
        self.assertEqual(
            [e for e in report["escalations"] if e["kind"] == "stalled"], []
        )
        self.assertEqual(seen, ["hermes:coder:default/THINK-1"])

    def test_a_probe_that_raises_does_not_suppress_a_real_stall(self):
        self._running_card("THINK-2", "probe is broken",
                           heartbeat_age=10, progress_age=3 * 3600)

        def probe(tag):
            raise RuntimeError("proxy unreachable")

        report = self._run(stalled_after=45 * 60, activity_probe=probe)
        self.assertEqual(
            len([e for e in report["escalations"] if e["kind"] == "stalled"]), 1
        )

    # -- 5d. one push per stuck card, never a stream ------------------------

    def _tick(self, at, **kw):
        """One escalator tick at wall-clock ``at``, sharing cursor + page log."""
        return escalator.run_once(
            self.db_path, board="default", config=kw.pop("config", {}) or {},
            apply=False, cursor_path=self._cursor(), state_path=self._state(),
            sender=self.sender, hermes_home=str(_HOME), notify=True,
            now=at, **kw,
        )

    def test_a_card_stalled_across_60_ticks_pages_exactly_once(self):
        """THE noise bug, measured on a live box: 60 ticks, 60 pushes for one
        card. A push must mean 'this is stuck and nothing else will come
        through' -- one per stuck card, never a stream."""
        base = int(time.time())
        self._running_card("LOUD-1", "hung worker",
                           heartbeat_age=20, progress_age=5 * 3600)
        for tick in range(60):
            at = base + tick * 60
            # The worker is hung but ALIVE: heartbeats keep landing, which is
            # exactly what keeps the card qualifying tick after tick.
            with self.conn:
                self.conn.execute(
                    "UPDATE tasks SET last_heartbeat_at = ? WHERE id = 'LOUD-1'",
                    (at - 20,),
                )
            self._tick(at)
        stalled = [m for m in self.sender.messages if "stalled" in m.title]
        self.assertEqual(len(stalled), 1, f"sent {len(stalled)} pages, expected 1")

    def test_a_stranded_card_across_many_ticks_pages_exactly_once(self):
        (_HOME / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        base = int(time.time())
        self._old_ready("QUIET-1", "nobody picked me up", "coder")
        for tick in range(30):
            self._tick(base + tick * 60, stranded_after=3600)
        stranded = [m for m in self.sender.messages if "stranded" in m.title]
        self.assertEqual(len(stranded), 1, f"sent {len(stranded)} pages, expected 1")

    def test_it_pages_again_after_repeat_after_seconds(self):
        """A still-broken card must resurface, or the cooldown becomes its own
        silence."""
        base = int(time.time())
        self._running_card("AGAIN-1", "still hung",
                           heartbeat_age=20, progress_age=5 * 3600)
        for at in (base, base + 3600, base + 6 * 3600 + 60):
            with self.conn:
                self.conn.execute(
                    "UPDATE tasks SET last_heartbeat_at = ? WHERE id = 'AGAIN-1'",
                    (at - 20,),
                )
            self._tick(at)
        stalled = [m for m in self.sender.messages if "stalled" in m.title]
        self.assertEqual(len(stalled), 2, "did not resurface after repeat_after")

    def test_a_state_change_re_pages_immediately(self):
        """A NEW worker pid is a NEW hang, not the same silence."""
        base = int(time.time())
        self._running_card("NEWPID-1", "respawned and hung again",
                           heartbeat_age=20, progress_age=5 * 3600)
        self._tick(base)
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET worker_pid = 5555, last_heartbeat_at = ? "
                "WHERE id = 'NEWPID-1'", (base + 40,),
            )
        self._tick(base + 60)
        stalled = [m for m in self.sender.messages if "stalled" in m.title]
        self.assertEqual(len(stalled), 2)

    def test_an_undelivered_page_is_not_recorded_so_the_next_tick_retries(self):
        """The cooldown must never swallow a page nobody actually received."""
        base = int(time.time())
        self._running_card("RETRY-1", "channel was down",
                           heartbeat_age=20, progress_age=5 * 3600)
        self.sender = RecordingSender(fail=True)
        self._tick(base)
        self.sender = RecordingSender()
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? WHERE id = 'RETRY-1'",
                (base + 40,),
            )
        self._tick(base + 60)
        self.assertEqual(
            len([m for m in self.sender.messages if "stalled" in m.title]), 1
        )

    def test_a_dry_run_never_consumes_a_cooldown(self):
        """Inspecting a tick must not silence the next real page."""
        base = int(time.time())
        self._running_card("DRY-3", "inspected first",
                           heartbeat_age=20, progress_age=5 * 3600)
        escalator.run_once(
            self.db_path, board="default", config={}, apply=False,
            cursor_path=self._cursor(), state_path=self._state(),
            notify=False, hermes_home=str(_HOME), now=base,
        )
        self._tick(base + 60)
        self.assertEqual(
            len([m for m in self.sender.messages if "stalled" in m.title]), 1
        )

    def test_a_corrupt_page_log_fails_open_rather_than_swallowing_a_page(self):
        base = int(time.time())
        Path(self._state()).write_text("{ this is not json")
        self._running_card("CORRUPT-1", "log was garbage",
                           heartbeat_age=20, progress_age=5 * 3600)
        self._tick(base)
        self.assertEqual(
            len([m for m in self.sender.messages if "stalled" in m.title]), 1
        )

    # -- 5e. the notify allowlist -------------------------------------------

    def test_notify_on_narrows_to_the_hard_breaks_only(self):
        (_HOME / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        self._old_ready("NARROW-1", "merely stranded", "coder")
        config = {"escalation": {
            "notify_on": ["gave_up", "blocked", "budget_exhausted", "stalled"],
        }}
        report = self._run(stranded_after=3600, config=config)
        self.assertEqual(
            [e for e in report["escalations"] if e["kind"] == "stranded"], []
        )
        self.assertEqual(self.sender.messages, [])

    def test_an_allowlist_suppression_is_still_reported(self):
        """Visible, just not pushed -- an operator must be able to see it."""
        (_HOME / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        self._old_ready("NARROW-2", "merely stranded", "coder")
        report = self._run(
            stranded_after=3600,
            config={"escalation": {"notify_on": ["gave_up"]}},
        )
        reasons = [s["suppressed_because"] for s in report["suppressed"]]
        self.assertTrue(any("notify_on" in r for r in reasons), reasons)

    def test_notify_on_unset_means_every_kind(self):
        self.assertEqual(
            escalator._notify_allowlist(None),
            frozenset(escalator.ALL_ESCALATION_KINDS),
        )

    # -- 5f. the closing line ------------------------------------------------

    def test_a_resolved_card_gets_one_closing_line(self):
        base = int(time.time())
        self._running_card("FIXED-1", "was hung, now done",
                           heartbeat_age=20, progress_age=5 * 3600)
        self._tick(base)
        self.assertEqual(
            len([m for m in self.sender.messages if "stalled" in m.title]), 1
        )
        with self.conn:
            self.conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'FIXED-1'")
        report = self._tick(base + 60)
        self.assertEqual(len(report["resolutions"]), 1)
        body = report["resolutions"][0]["body"]
        self.assertIn("moving again", body)
        self.assertIn("was=stalled", body)
        self.assertNotIn("resume it with", body)

    def test_a_closing_line_is_sent_once_not_every_tick(self):
        base = int(time.time())
        self._running_card("FIXED-2", "was hung", heartbeat_age=20,
                           progress_age=5 * 3600)
        self._tick(base)
        with self.conn:
            self.conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'FIXED-2'")
        for tick in range(1, 20):
            self._tick(base + tick * 60)
        closings = [m for m in self.sender.messages if "resolved" in m.title]
        self.assertEqual(len(closings), 1)

    def test_a_still_stuck_card_gets_no_closing_line(self):
        base = int(time.time())
        self._running_card("STILL-1", "still hung", heartbeat_age=20,
                           progress_age=5 * 3600)
        self._tick(base)
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? WHERE id = 'STILL-1'",
                (base + 40,),
            )
        report = self._tick(base + 60)
        self.assertEqual(report["resolutions"], [])

    def test_resolution_can_be_switched_off(self):
        base = int(time.time())
        self._running_card("FIXED-3", "was hung", heartbeat_age=20,
                           progress_age=5 * 3600)
        config = {"escalation": {"send_resolution": False}}
        self._tick(base, config=config)
        with self.conn:
            self.conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'FIXED-3'")
        report = self._tick(base + 60, config=config)
        self.assertEqual(report["resolutions"], [])

    def test_the_caller_purpose_tag_matches_the_documented_format(self):
        self.assertEqual(
            escalator.caller_purpose_tag("coder", "project-a", "t_0000abcd"),
            "hermes:coder:project-a/t_0000abcd",
        )

    # -- 6. delivery failures must not lose a card --------------------------

    def test_an_undelivered_escalation_does_not_advance_the_cursor(self):
        """A card nobody was told about must not be forgotten because the
        channel was down for a minute."""
        add_card(self.conn, "DOWN-1", "channel was down", status="running")
        for _ in range(2):
            kanban_db._record_task_failure(
                self.conn, "DOWN-1", "AssertionError: broken",
                outcome="crashed", failure_limit=2,
            )
        self.sender = RecordingSender(fail=True)
        first = self._run()
        self.assertFalse(first["escalations"][0]["delivered"])

        self.sender = RecordingSender()
        second = self._run()
        self.assertIn("gave_up", [e["kind"] for e in second["escalations"]])
        self.assertTrue(second["escalations"][0]["delivered"])

    # -- 7. read-only posture ------------------------------------------------

    def test_a_dry_run_never_writes_to_the_board(self):
        add_card(self.conn, "DRY-1", "dry run", status="running")
        for _ in range(2):
            kanban_db._record_task_failure(
                self.conn, "DRY-1", "upstream disconnected mid-stream",
                outcome="crashed", failure_limit=2,
            )
        report = escalator.run_once(
            self.db_path, board="default",
            config={"kanban": {"count_toward_breaker": {"backend_busy": False}}},
            apply=False, notify=False, cursor_path=self._cursor(),
        )
        self.assertTrue([r for r in report["reconciled"] if r["recovered"]])
        self.assertEqual(status_of(self.conn, "DRY-1"), "blocked",
                         "a dry run wrote to the board")

    def test_a_dry_run_sends_nothing(self):
        add_card(self.conn, "DRY-2", "dry run", status="running")
        for _ in range(2):
            kanban_db._record_task_failure(
                self.conn, "DRY-2", "AssertionError: broken",
                outcome="crashed", failure_limit=2,
            )
        report = escalator.run_once(
            self.db_path, board="default", config={},
            apply=False, notify=False, cursor_path=self._cursor(),
        )
        self.assertTrue(report["escalations"])
        self.assertEqual(
            [e["channel"] for e in report["escalations"]], ["none"],
            "a dry run used a real channel",
        )

    def test_worker_log_path_matches_the_agents_own(self):
        """The pointer we hand a human must be the file that actually exists."""
        expected = kanban_db.worker_log_path("CARD-9")
        self.assertEqual(
            escalator.worker_log_path("default", "CARD-9", str(_HOME)),
            str(expected),
        )


# =============================================================================
# retry limit 1: a real failure blocks on its first occurrence, and the cut
# goes to the planner as one split card
# =============================================================================

BOARD_YAML = REPO / "config" / "board.yaml"

HANDOVER = ("Done: parsed the first half of the export\n"
            "Method: ran the extractor on sample.csv\n"
            "Files: scripts/extract.py\n"
            "Result: 412 of 900 rows\n"
            "Could not check: none\n"
            "Review first: scripts/extract.py\n"
            "Doubts: the second half has a different header")


class TestRetryLimitOne(unittest.TestCase):
    """Budget exhaustion or a non-wait crash blocks the card the first time and
    hands the cut to the planner; a wait-class failure is a queue."""

    setUp = TestOnTheRealBoard.setUp
    _cursor = TestOnTheRealBoard._cursor
    _state = TestOnTheRealBoard._state

    def _run(self, **kw):
        import board as board_mod
        return escalator.run_once(
            self.db_path, board="default", apply=True,
            cursor_path=self._cursor(), state_path=self._state(),
            sender=self.sender, hermes_home=str(_HOME),
            board_spec=board_mod.load_board(BOARD_YAML), **{"config": {}, **kw})

    def _card_with_handover(self, card_id, title):
        """A card whose earlier run handed off (the harness writes the run
        summary and a comment), then went back to to_do for another run."""
        add_card(self.conn, card_id, title)
        now = int(time.time())
        with self.conn:
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
                "outcome, summary) VALUES (?, 'coder', 'released', ?, ?, 'handed_off', ?)",
                (card_id, now - 7200, now - 3600, HANDOVER))
        kanban_db.add_comment(self.conn, card_id, "coder",
                              f"[hand-off -> qa (to_do)]\n{HANDOVER}")

    def _fail(self, card_id, outcome, error):
        """The dispatcher's own path: claim, then record the failure, which
        closes the run and requeues the card to ready."""
        self.assertIsNotNone(kanban_db.claim_task(self.conn, card_id))
        kanban_db._record_task_failure(self.conn, card_id, error, outcome=outcome,
                                       failure_limit=3, release_claim=True, end_run=True)

    def _planner_cards(self, parent):
        return self.conn.execute(
            "SELECT * FROM tasks WHERE body LIKE ?", (f"%split-of: {parent}%",)).fetchall()

    def _block_comment(self, card_id):
        return [r["body"] for r in self.conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? "
            "AND author = 'kanban-escalator'", (card_id,))]

    def test_exhaustion_blocks_on_first_occurrence_and_makes_one_planner_card(self):
        self._card_with_handover("EXH-1", "port the exporter")
        self._fail("EXH-1", "timed_out", "max_runtime_seconds exceeded")
        self.assertEqual(status_of(self.conn, "EXH-1"), "ready")  # the dispatcher requeued it

        self._run()
        self.assertEqual(status_of(self.conn, "EXH-1"), "blocked")
        cards = self._planner_cards("EXH-1")
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["assignee"], "planner")
        self.assertEqual(card["status"], "ready")
        body = card["body"]
        self.assertIn("EXH-1", body)
        self.assertIn("port the exporter", body)
        self.assertIn(HANDOVER, body, "the hand-over must be carried verbatim")
        self.assertIn("handed_off", body)
        self.assertIn("timed_out", body)
        self.assertIn("max_runtime_seconds exceeded", body)
        # The reason is a plain sentence: it is what reaches the phone.
        comments = self._block_comment("EXH-1")
        self.assertEqual(len(comments), 1)
        self.assertRegex(comments[0], r"^budget exhausted after \S.* on the first run; "
                         rf"the cut is with the planner as {card['id']}$")
        self.assertTrue(any(f"the cut is with the planner as {card['id']}" in m.body
                            for m in self.sender.messages))

    def test_a_turn_budget_exhaustion_names_the_turns(self):
        add_card(self.conn, "EXH-2", "long goal")
        self.assertIsNotNone(kanban_db.claim_task(self.conn, "EXH-2"))
        kanban_db.block_task(self.conn, "EXH-2", reason=(
            "Goal-mode worker exhausted its turn budget (60/60) without completing "
            "the task. Last judge verdict: not done"))
        self._run()
        self.assertEqual(status_of(self.conn, "EXH-2"), "blocked")
        cards = self._planner_cards("EXH-2")
        self.assertEqual(len(cards), 1)
        self.assertEqual(self._block_comment("EXH-2"), [
            "budget exhausted after 60 turns on the first run; "
            f"the cut is with the planner as {cards[0]['id']}"])

    def test_a_turn_budget_exhaustion_pages_once_and_names_the_cut(self):
        """The worker's own block and the reconciler's are the same fact; only
        the one that names the planner card reaches the phone."""
        add_card(self.conn, "EXH-3", "long goal")
        self.assertIsNotNone(kanban_db.claim_task(self.conn, "EXH-3"))
        kanban_db.block_task(self.conn, "EXH-3", reason=(
            "Goal-mode worker exhausted its turn budget (60/60) without completing "
            "the task. Last judge verdict: not done"))
        self._run()
        pages = [m for m in self.sender.messages if "EXH-3" in m.title + m.body]
        self.assertEqual(len(pages), 1, [m.title for m in pages])
        self.assertIn("the cut is with the planner as", pages[0].body)

    def test_a_split_card_says_plainly_when_there_is_no_hand_over(self):
        """A worker that never initialised hands nothing over. A blank field
        invites the planner to invent a cut; the card says so instead."""
        add_card(self.conn, "NOHO-1", "never started")
        self._fail("NOHO-1", "timed_out", "max_runtime_seconds exceeded")
        self._run()
        body = self._planner_cards("NOHO-1")[0]["body"]
        self.assertIn("No hand-over:", body)
        self.assertIn("do not invent a cut", body.lower())
        self.assertNotIn("(none recorded)", body)

    def test_a_real_crash_blocks_on_first_occurrence(self):
        self._card_with_handover("CR-1", "fix the parser")
        self._fail("CR-1", "crashed", "AssertionError: the card's own test fails")
        self._run()
        self.assertEqual(status_of(self.conn, "CR-1"), "blocked")
        self.assertEqual(len(self._planner_cards("CR-1")), 1)

    def test_a_wait_class_crash_creates_nothing_and_stays_claimable(self):
        add_card(self.conn, "WC-1", "waited on the model")
        self._fail("WC-1", "crashed", "RemoteProtocolError: upstream disconnected mid-stream")
        add_card(self.conn, "WC-2", "rate limited")
        self._fail("WC-2", "crashed", "HTTP 429: rate limit exceeded")
        self._run()
        for cid in ("WC-1", "WC-2"):
            self.assertEqual(status_of(self.conn, cid), "ready")
            self.assertEqual(self._planner_cards(cid), [])
        self.assertIsNotNone(kanban_db.claim_task(self.conn, "WC-1"))

    def test_a_second_tick_and_a_second_exhaustion_make_no_second_card(self):
        self._card_with_handover("IDEM-1", "too big")
        self._fail("IDEM-1", "timed_out", "max_runtime_seconds exceeded")
        self._run()
        self._run()
        self.assertEqual(len(self._planner_cards("IDEM-1")), 1)
        # A human puts it back while the planner card is open, and it fails again.
        kanban_db.unblock_task(self.conn, "IDEM-1")
        self._fail("IDEM-1", "timed_out", "max_runtime_seconds exceeded")
        self._run()
        self.assertEqual(status_of(self.conn, "IDEM-1"), "blocked")
        self.assertEqual(len(self._planner_cards("IDEM-1")), 1)

    def test_the_parent_is_not_unblocked_while_the_planner_card_is_open(self):
        self._card_with_handover("PAR-1", "split me")
        self._fail("PAR-1", "timed_out", "max_runtime_seconds exceeded")
        self._run()
        kanban_db.recompute_ready(self.conn, failure_limit=3)
        self._run(config={"kanban": {"count_toward_breaker": {"timed_out": False}}})
        self._run()
        self.assertEqual(status_of(self.conn, "PAR-1"), "blocked")
        # The planner card itself is claimable: no parent link holds it in todo.
        planner = self._planner_cards("PAR-1")[0]
        self.assertIsNotNone(kanban_db.claim_task(self.conn, planner["id"]))

    def test_the_reconcilers_moves_do_not_page_as_illegal(self):
        self._card_with_handover("AUD-1", "audited")
        self._run()                              # the audit's first look
        self.assertIsNotNone(kanban_db.claim_task(self.conn, "AUD-1"))
        self._run()                              # it sees AUD-1 running
        kanban_db._record_task_failure(self.conn, "AUD-1", "max_runtime_seconds exceeded",
                                       outcome="timed_out", failure_limit=3,
                                       release_claim=True, end_run=True)
        report = self._run()
        self._run()
        self.assertEqual(status_of(self.conn, "AUD-1"), "blocked")
        self.assertEqual([e for e in report["escalations"] if e["kind"] == "illegal_move"], [])
        self.assertFalse([m for m in self.sender.messages if "not a move" in m.body])


class TestBackendNotServing(unittest.TestCase):
    """T45f/T45g (docs/harness-contract.md): a worker's open wait note is the
    evidence. Three workers waiting on one endpoint and model are one outage and
    one page; a worker in a wait is never paged as stalled."""

    setUp = TestOnTheRealBoard.setUp
    _cursor = TestOnTheRealBoard._cursor
    _state = TestOnTheRealBoard._state
    _run = TestOnTheRealBoard._run
    EP, MODEL = "127.0.0.1:4242", "qwen3.6-35b"

    def _waiting(self, card_id, *, since_ago=900, updated_ago=20, state="open",
                 heartbeat_ago=30, model=None):
        now = int(time.time())
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, started_at, assignee, "
                "worker_pid, last_heartbeat_at, claim_lock, claim_expires) "
                "VALUES (?, 'waits on the model', 'running', ?, ?, 'coder', 4343, ?, 'l', ?)",
                (card_id, now - 7200, now - 7200, now - heartbeat_ago, now + 900))
            self.conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, 'visible-wait', ?, ?)",
                (card_id, f"Waiting on the model backend.\n[visible-wait {state} "
                 f"endpoint={self.EP} model={model or self.MODEL} since={now - since_ago} "
                 f"updated={now - updated_ago}]", now - since_ago))

    def _close_waits(self):
        with self.conn:
            self.conn.execute("UPDATE task_comments SET body = replace(body, "
                              "'[visible-wait open', '[visible-wait closed')")

    def _pages(self, report):
        return [e for e in report["escalations"] if e["kind"] == "backend_not_serving"]

    def test_T45f_three_workers_on_one_endpoint_are_one_page(self):
        for cid in ("W-1", "W-2", "W-3"):
            self._waiting(cid)
        pages = self._pages(self._run())
        self.assertEqual(len(pages), 1, pages)
        self.assertIn(self.EP, pages[0]["body"])
        self.assertIn(self.MODEL, pages[0]["body"])
        self.assertEqual(self._pages(self._run()), [], "the same outage paged twice")

    def test_T45f_the_outage_closes_and_a_second_one_pages_again(self):
        self._waiting("W-1")
        self.assertEqual(len(self._pages(self._run())), 1)
        self._close_waits()
        report = self._run()
        self.assertEqual([r["resolved_kind"] for r in report["resolutions"]],
                         ["backend_not_serving"])
        with self.conn:
            self.conn.execute("DELETE FROM task_comments")
            self.conn.execute("DELETE FROM tasks")
        self._waiting("W-4")
        self.assertEqual(len(self._pages(self._run())), 1, "a new outage stayed silent")

    def test_T45f_another_model_is_another_outage(self):
        self._waiting("W-1")
        self._waiting("W-2", model="devstral")
        self.assertEqual(len(self._pages(self._run())), 2)

    def test_T45f_a_short_wait_or_a_dead_note_does_not_page(self):
        self._waiting("W-1", since_ago=60)          # under five minutes: a slow prefill
        self._waiting("W-2", updated_ago=3600)      # nobody updated it: the worker is gone
        self.assertEqual(self._pages(self._run()), [])

    def test_T45g_a_waiting_worker_is_not_paged_as_stalled(self):
        self._waiting("W-1", heartbeat_ago=30)
        log = Path(escalator.worker_log_path("default", "W-1", str(_HOME)))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("waiting\n")
        old = time.time() - 7200
        os.utime(log, (old, old))
        report = self._run()
        self.assertEqual([e for e in report["escalations"] if e["kind"] == "stalled"], [])
        self.assertEqual(len(self._pages(report)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
