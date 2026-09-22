#!/usr/bin/env python3
"""kanban-escalator -- the reconciler that makes silence impossible.

Runs on the host, on an interval, against one board's ``kanban.db``.  Two jobs,
in this order:

1. **Reconcile the breaker (policy C).**  A card that auto-blocked with a
   ``gave_up`` whose trigger outcome is configured NOT to count toward the
   breaker had a machine failure, not a card failure.  The counter is cleared
   and the card goes back to ``ready`` -- the state it would have been in if
   the machine had behaved.

2. **Escalate (policy D).**  Every card that would be left blocked, given up,
   or stranded gets a human told over the configured channel, with the card id
   and title, the board, what happened in plain words, the last error, the
   exact resume command, and the worker log path.

Why a reconciler and not a fork of ``kanban_db.py``
---------------------------------------------------
The counting rule lives inside ``_record_task_failure``, which is upstream
code.  Replacing that file to add a policy lookup means carrying a fork of an
~8000-line file that churns upstream, and re-deriving it after every agent
upgrade.  A reconciler gets the same observable behaviour -- a machine-caused
failure does not cost the card a life -- from outside, against the board's
public schema, with no fork to maintain.  The cost is that the correction is
eventually-consistent (one tick late) rather than atomic; the card is blocked
for up to one interval before it is put back.  That is an acceptable trade
because a human is being told in the same tick either way.

The in-agent half (``error_policy`` / ``wait_budget``) still wants a small
delta in the agent's retry loop to take effect; see docs/resilience.md.  This
reconciler is useful on its own and is what runs today.

Safety
------
* Read-only by default.  Writes only with ``--apply``.
* ``--db`` is required; there is no "guess the live board" default.
* Refuses to run against a board listed in ``$HERMES_ESCALATOR_READONLY_BOARDS``
  (comma-separated) even with ``--apply``.  A supervised run in flight is
  exactly the case that must not be touched.

Stdlib only -- it runs from launchd, where third-party imports are a liability.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import escalation  # noqa: E402
import failure_policy  # noqa: E402
from error_policy import BACKEND_BUSY, looks_like_backend_busy  # noqa: E402

__all__ = [
    "ALL_ESCALATION_KINDS",
    "ESCALATE_KINDS",
    "IgnoreRules",
    "PageLog",
    "caller_purpose_tag",
    "known_profiles",
    "load_config",
    "reconcile_breaker",
    "find_stranded",
    "find_stalled",
    "run_once",
]

#: Event kinds that mean "this card is not going to move on its own".
#: Deliberately NOT including bare ``crashed`` / ``timed_out``: those drop the
#: card back to ``ready`` and it gets picked up again.  Escalating them would
#: make the channel noise that gets notifications muted, which is how a board
#: goes silent for a second time.
ESCALATE_KINDS = (
    "gave_up",
    "blocked",
    "spawn_auto_blocked",
    "budget_exhausted",
)

#: How long a ``ready``, unclaimed card may sit untouched before it counts as
#: stranded.  Generous by default (an idle dispatcher tick is 60 s): this is
#: about "nobody can run it", not "nobody has got to it yet".
DEFAULT_STRANDED_AFTER = 3600

#: How long a ``running`` card may make no progress -- no task event other than
#: ``heartbeat`` -- before it counts as stalled.  A heartbeat means the process
#: lives, not that the work moves; progress needs its own signal.
DEFAULT_STALLED_AFTER = 45 * 60

#: How recent ``last_heartbeat_at`` must be for a card to count as stalled
#: rather than crashed.  A card whose heartbeats stopped is the dispatcher's
#: problem (``detect_crashed_workers`` reaps it); a card whose heartbeats are
#: FRESH while nothing moves is the deadlock nobody else is looking for.
DEFAULT_HEARTBEAT_FRESH_WITHIN = 300

#: Pages that exist only when a board specification is deployed: they guard
#: board invariants ("accept is the only door to done"; "nothing waits for the
#: user in the runtime's agent review lane").  Without a board, the runtime's
#: `complete` is the normal door and its review lane may be in legitimate use.
BOARD_ALARM_KINDS = ("done_without_accept", "agent_review_lane", "ledger_missing",
                     "ledger_shrank", "harness_not_loaded", "illegal_move",
                     "handoff_form_missing")

#: Every kind this tool can page about.  ``escalation.notify_on`` narrows it.
ALL_ESCALATION_KINDS = (ESCALATE_KINDS + ("stranded", "stalled", "backend_not_serving")
                        + BOARD_ALARM_KINDS)

#: Alarms about something that already happened: nothing to "clear" later.
_NO_RESOLUTION = frozenset({"done_without_accept", "illegal_move"})
#: Faults detected afresh every tick; each closes on the first tick it is absent.
_FAULT_KINDS = frozenset({"harness_not_loaded", "ledger_missing", "handoff_form_missing",
                          "backend_not_serving"})

#: How long before the SAME card in the SAME condition may page again.
#: Six hours: long enough that a card stuck overnight produces one message
#: rather than six hundred, short enough that a still-broken card resurfaces
#: within a working day.  A state change re-pages immediately regardless.
DEFAULT_REPEAT_AFTER = 6 * 3600

#: Statuses that mean the card is moving again, used to close an open page.
_MOVING_STATUSES = frozenset({"ready", "running", "done", "archived"})

DEFAULT_CURSOR_NAME = ".escalator_cursor"
DEFAULT_PAGE_LOG_NAME = ".escalator_pages.json"


# -- config -----------------------------------------------------------------

def load_config(path: Optional[str]) -> Dict[str, Any]:
    """Load the resilience config.

    YAML when PyYAML is importable (the agent venv ships it), JSON otherwise,
    so the reconciler still starts on a bare system python.  An absent file is
    an empty config, which is valid -- every key has a default except
    ``escalation.channel``, whose absence is reported at send time.
    """
    if not path:
        return {}
    file_path = Path(os.path.expanduser(path))
    if not file_path.exists():
        return {}
    text = file_path.read_text()
    try:
        import yaml  # type: ignore
        loaded = yaml.safe_load(text)
    except ImportError:
        loaded = json.loads(text)
    return loaded if isinstance(loaded, dict) else {}


# -- cursor -----------------------------------------------------------------

def _read_cursor(conn: sqlite3.Connection, cursor_path: Path) -> int:
    try:
        return int(cursor_path.read_text().strip())
    except (OSError, ValueError):
        # First run: start at the current tip so history is not replayed as a
        # burst of notifications about cards a human already dealt with.
        tip = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_events"
        ).fetchone()[0]
        _write_cursor(cursor_path, int(tip))
        return int(tip)


def _write_cursor(cursor_path: Path, value: int) -> None:
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
    tmp.write_text(str(int(value)))
    os.replace(tmp, cursor_path)


# -- board helpers ----------------------------------------------------------

def worker_log_path(board: str, card_id: str, hermes_home: Optional[str] = None) -> str:
    """Mirror of the agent's ``worker_log_path``.

    Reimplemented (rather than imported) so the reconciler runs without the
    agent on the path; the layout is board-stable and covered by a test.
    """
    root = Path(os.path.expanduser(
        hermes_home or os.environ.get("HERMES_HOME") or "~/.hermes"
    ))
    if board in ("", "default"):
        return str(root / "kanban" / "logs" / f"{card_id}.log")
    return str(root / "kanban" / "boards" / board / "logs" / f"{card_id}.log")


def known_profiles(hermes_home: Optional[str] = None) -> set:
    """Profile names that actually exist, i.e. assignees a worker can run as.

    An assignee that maps to no profile is a HUMAN LANE: a review gate parked
    on a person's name, which by design no dispatcher will ever claim. Those
    cards are permanently ``ready`` and permanently fine, so escalating them
    is a false page — and a channel that pages falsely gets muted, which is
    how a board goes silent a second time.
    """
    root = Path(os.path.expanduser(
        hermes_home or os.environ.get("HERMES_HOME") or "~/.hermes"
    ))
    names = {"default"}
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        names.update(p.name for p in profiles_dir.iterdir() if p.is_dir())
    return names


class IgnoreRules:
    """Cards a human has declared permanently uninteresting.

    Three axes, all optional, all under ``escalation.ignore``::

        escalation:
          ignore:
            card_ids:       [t_0000abcd]
            assignees:      [user]
            title_patterns: ["review gate*", "*DO NOT DISPATCH*"]

    ``title_patterns`` are shell globs (``fnmatch``), matched case-insensitively
    against the card title.
    """

    def __init__(self, cfg: Optional[Mapping[str, Any]] = None) -> None:
        cfg = cfg if isinstance(cfg, Mapping) else {}
        self.card_ids = {str(v) for v in cfg.get("card_ids") or []}
        self.assignees = {str(v).lower() for v in cfg.get("assignees") or []}
        self.title_patterns = [str(v).lower() for v in cfg.get("title_patterns") or []]

    def matches(self, card_id: Any, title: Any, assignee: Any) -> Optional[str]:
        """Return the rule that matched, or None.  The reason is reported, so a
        suppressed card is visible in the tick's report rather than invisible."""
        if str(card_id) in self.card_ids:
            return f"card id {card_id} is in escalation.ignore.card_ids"
        if assignee and str(assignee).lower() in self.assignees:
            return f"assignee {assignee!r} is in escalation.ignore.assignees"
        text = str(title or "").lower()
        for pattern in self.title_patterns:
            if fnmatch.fnmatch(text, pattern):
                return f"title matches escalation.ignore.title_patterns {pattern!r}"
        return None


class PageLog:
    """What has already been said to a human, so it is not said again.

    ``gave_up`` and ``blocked`` are EVENTS — one row each, and the event cursor
    naturally pages them once.  ``stranded`` and ``stalled`` are not events:
    they are computed from the card's CURRENT state on every tick.  Nothing in
    the cursor stops them, so a hung card pages every 60 s for as long as it
    stays hung.  Measured before this class existed: 60 consecutive ticks
    produced 60 pages for one card.  That is how a channel gets muted, and a
    muted channel is the second silence.

    So a page is recorded per ``(card_id, kind)`` and repeats only when:

    * ``repeat_after_seconds`` has elapsed (default 6h), **or**
    * the card's state fingerprint changed — a new worker pid on a stalled
      card is a new hang and deserves a new page.

    Only a DELIVERED page is recorded.  An undelivered one leaves no trace, so
    the next tick retries it — the same rule the event cursor follows.

    The log is a small JSON file beside the board DB.  A corrupt or unreadable
    file degrades to "nothing has been paged yet": at worst one duplicate
    message, never a swallowed one.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        self.entries: Dict[str, Dict[str, Any]] = {}
        #: board -> the most accept-ledger lines ever seen. Only ever rises: it
        #: is the evidence of the ledger's former size, kept where deleting the
        #: ledger does not reach.
        self.ledger_high_water: Dict[str, int] = {}
        #: board -> {"statuses": {card: status}, "event_id": last event seen}.
        #: What the audit compares each tick against.
        self.audit: Dict[str, Dict[str, Any]] = {}
        self.dirty = False
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict) and isinstance(loaded.get("pages"), dict):
                    self.entries = {
                        k: v for k, v in loaded["pages"].items()
                        if isinstance(v, dict)
                    }
                if isinstance(loaded, dict) and isinstance(loaded.get("ledger_high_water"), dict):
                    self.ledger_high_water = {
                        str(k): int(v) for k, v in loaded["ledger_high_water"].items()}
                if isinstance(loaded, dict) and isinstance(loaded.get("audit"), dict):
                    self.audit = loaded["audit"]
            except (OSError, ValueError):
                # Fail OPEN: a lost log means we may repeat a page once.
                # Failing closed would mean silently swallowing one.
                self.entries = {}

    @staticmethod
    def key(card_id: Any, kind: Any) -> str:
        return f"{card_id}|{kind}"

    def should_page(
        self,
        card_id: Any,
        kind: Any,
        fingerprint: str,
        *,
        now: int,
        repeat_after: int,
    ) -> Tuple[bool, str]:
        """Return ``(page?, reason)``.  The reason is reported either way."""
        entry = self.entries.get(self.key(card_id, kind))
        if entry is None:
            return True, "first time this card has been in this condition"
        if entry.get("fingerprint") != fingerprint:
            return True, (
                f"state changed since the last page "
                f"({entry.get('fingerprint')!r} -> {fingerprint!r})"
            )
        age = now - int(entry.get("at") or 0)
        if age >= int(repeat_after):
            return True, (
                f"still stuck {_duration(age)} after the last page "
                f"(repeat_after {_duration(repeat_after)})"
            )
        return False, (
            f"already paged {_duration(age)} ago and nothing has changed "
            f"(repeats after {_duration(repeat_after)})"
        )

    def record(
        self,
        card_id: Any,
        kind: Any,
        fingerprint: str,
        *,
        now: int,
        title: str = "",
        status: str = "",
    ) -> None:
        self.entries[self.key(card_id, kind)] = {
            "card_id": str(card_id),
            "kind": str(kind),
            "fingerprint": fingerprint,
            "at": int(now),
            "title": title,
            "status_at_page": status,
        }
        self.dirty = True

    def close(self, card_id: Any, kind: Any) -> Optional[Dict[str, Any]]:
        entry = self.entries.pop(self.key(card_id, kind), None)
        if entry is not None:
            self.dirty = True
        return entry

    def open_entries(self) -> List[Dict[str, Any]]:
        return list(self.entries.values())

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(
            {"version": 1, "pages": self.entries,
             "ledger_high_water": self.ledger_high_water,
             "audit": self.audit}, indent=2, sort_keys=True,
        ))
        os.replace(tmp, self.path)
        self.dirty = False


# -- board rules: auto_advance and archiving (config/board.yaml) ------------
#
# These are the board's two MOVES that no agent makes: a card leaving
# `waiting` when what it waits on arrives, and a finished card being archived
# on a clock.  Both are OPT-IN -- they run only when a board specification is
# passed explicitly (--board-file / escalation.board_file).  This reconciler
# already runs with --apply against live boards; a board rule that switched
# itself on at the next pull would start moving real cards nobody asked it to.

AUTO_ADVANCE_AUTHOR = "kanban-escalator"
#: Stamped into every auto_advance comment so a re-run can tell it already
#: explained a move, and never writes the same comment twice.
AUTO_ADVANCE_MARKER = "[auto_advance]"


def auto_advance(
    conn: sqlite3.Connection,
    board_spec: Any,
    *,
    apply: bool,
    now: int,
) -> List[Dict[str, Any]]:
    """Move cards out of a waiting column when every card they wait on arrived.

    ``references_card`` means a REAL dependency link (the board's ``--parent``),
    not a mention in prose -- see docs/board-design.md.  Upstream
    ``recompute_ready`` already promotes a ``todo`` card when all its parents
    reach ``done``/``archived``.  Two things it does not do, and this does:

    * **arrive on ``review``.**  The specification waits for ``[review, done]``;
      upstream only fires on ``done``.
    * **explain the move.**  Upstream writes a bare ``promoted`` event.  The
      specification requires the move to carry a comment, so this writes it --
      and BACKFILLS it on a card upstream already promoted, because the two
      race and upstream wins whenever the parent went straight to ``done``.

    A card with several parents leaves only when EVERY one has arrived: a card
    waiting on two things is not unblocked by one of them.
    """
    moves: List[Dict[str, Any]] = []
    for column in board_spec.auto_advance_rules():
        rule = column.auto_advance
        target_col = rule.get("move_to")
        if not target_col or target_col not in board_spec.columns:
            continue
        waiting_status = column.status
        target_status = board_spec.status_of(target_col)
        reach_cols = _as_list_of_str(
            (rule.get("when") or {}).get("referenced_card_reaches")
        )
        # `archived` also counts as arrived: upstream treats an archived parent
        # exactly like a done one, and a parent that went done -> archived
        # between two ticks must not strand the child forever.
        arrived = {board_spec.status_of(c) for c in reach_cols if c in board_spec.columns}
        arrived.add("archived")
        template = rule.get("comment") or "referenced card {ref} reached {state}"

        # 1. Still waiting, and everything it waits on has arrived: move it.
        for row in conn.execute(
            "SELECT id, title FROM tasks WHERE status = ?", (waiting_status,)
        ).fetchall():
            parents = _parents(conn, row["id"])
            if not parents or not all(p["status"] in arrived for p in parents):
                continue
            comment = _render_advance_comment(template, parents, board_spec)
            if apply:
                _advance(conn, row["id"], waiting_status, target_status, comment, now)
            moves.append({
                "card_id": row["id"], "title": row["title"],
                "from": column.name, "to": target_col,
                "comment": comment, "backfilled": False, "applied": apply,
            })

        # 2. Upstream won the race: already moved, with no explanation. Say why.
        for row in conn.execute(
            "SELECT id, title FROM tasks WHERE status = ?", (target_status,)
        ).fetchall():
            parents = _parents(conn, row["id"])
            if not parents or not all(p["status"] in arrived for p in parents):
                continue
            if not _promoted_without_explanation(conn, row["id"]):
                continue
            comment = _render_advance_comment(template, parents, board_spec)
            if apply:
                _write_comment(conn, row["id"], comment, now)
            moves.append({
                "card_id": row["id"], "title": row["title"],
                "from": column.name, "to": target_col,
                "comment": comment, "backfilled": True, "applied": apply,
            })
    return moves


def auto_archive(
    conn: sqlite3.Connection,
    board_spec: Any,
    *,
    apply: bool,
    now: int,
) -> List[Dict[str, Any]]:
    """Archive finished cards on a clock -- the only clock-driven move.

    Touches ONLY cards in the done column.  Whatever the specification decides
    about ``never_archive_unless_done`` (docs/board-design.md D2), the clock
    itself never archives anything that is not finished.
    """
    after = board_spec.auto_archive_done_after
    done_col = board_spec.column_of("done")
    archived_col = board_spec.column_of("archived")
    if not after or not done_col or not archived_col:
        return []
    if not board_spec.can_move(done_col, archived_col):
        return []  # the board has no done -> archived edge; the clock may not invent one
    cutoff = now - int(after)
    archived: List[Dict[str, Any]] = []
    for row in conn.execute(
        "SELECT id, title, completed_at FROM tasks "
        "WHERE status = 'done' AND completed_at IS NOT NULL AND completed_at < ?",
        (cutoff,),
    ).fetchall():
        if apply:
            _archive(conn, row["id"], int(after), now)
        archived.append({
            "card_id": row["id"], "title": row["title"],
            "done_for": _duration(now - int(row["completed_at"])),
            "applied": apply,
        })
    return archived


def _parents(conn: sqlite3.Connection, card_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT t.id, t.status FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id WHERE l.child_id = ? "
        "ORDER BY t.id",
        (card_id,),
    ).fetchall()


def _render_advance_comment(template: str, parents: List[sqlite3.Row], board_spec: Any) -> str:
    parts = []
    for parent in parents:
        state = board_spec.column_of(parent["status"]) or parent["status"]
        try:
            parts.append(template.format(ref=parent["id"], state=state))
        except (KeyError, IndexError, ValueError):
            parts.append(f"referenced card {parent['id']} reached {state}")
    return f"{AUTO_ADVANCE_MARKER} " + "; ".join(parts)


def _promoted_without_explanation(conn: sqlite3.Connection, card_id: str) -> bool:
    """True when the latest move into this column was upstream's bare
    ``promoted`` and no auto_advance comment has been written since."""
    event = conn.execute(
        "SELECT created_at, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'promoted' ORDER BY id DESC LIMIT 1",
        (card_id,),
    ).fetchone()
    if event is None:
        return False
    payload = _payload(event["payload"])
    if payload.get("by") == AUTO_ADVANCE_AUTHOR:
        return False  # we made this move and explained it at the time
    explained = conn.execute(
        "SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? "
        "AND body LIKE ? AND created_at >= ? LIMIT 1",
        (card_id, AUTO_ADVANCE_AUTHOR, f"{AUTO_ADVANCE_MARKER}%", int(event["created_at"])),
    ).fetchone()
    return explained is None


def _write_comment(conn: sqlite3.Connection, card_id: str, body: str, now: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (card_id, AUTO_ADVANCE_AUTHOR, body, now),
        )


def _advance(
    conn: sqlite3.Connection,
    card_id: str,
    from_status: str,
    to_status: str,
    comment: str,
    now: int,
) -> None:
    """The move and its explanation, in one transaction -- a card must never
    arrive in to-do without the comment saying why it is there."""
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND status = ?",
            (to_status, card_id, from_status),
        )
        if cur.rowcount != 1:
            return  # somebody else moved it this tick; theirs wins
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (card_id, AUTO_ADVANCE_AUTHOR, comment, now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'promoted', ?, ?)",
            (card_id, json.dumps({"by": AUTO_ADVANCE_AUTHOR, "reason": comment}), now),
        )


def _archive(conn: sqlite3.Connection, card_id: str, after: int, now: int) -> None:
    """Same shape as upstream ``archive_task`` for a finished card: status,
    cleared claim, an ``archived`` event.  A done card has no run in flight,
    and archiving a done parent unblocks nothing that done had not already."""
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status = 'done'",
            (card_id,),
        )
        if cur.rowcount != 1:
            return
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'archived', ?, ?)",
            (card_id, json.dumps({
                "by": AUTO_ADVANCE_AUTHOR,
                "reason": f"done for longer than auto_archive_done_after ({_duration(after)})",
            }), now),
        )


def _as_list_of_str(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


# -- the audit: every status change checked against the table --------------------

#: Event kinds the dispatcher writes when it moves a card on its own.
_DISPATCHER_EVENTS = frozenset({"claimed", "spawned", "crashed", "timed_out", "gave_up",
                                "reclaimed", "spawn_failed", "spawn_auto_blocked",
                                "rate_limited", "protocol_violation"})


def _actor(kind: str, payload: Mapping[str, Any]) -> str:
    """Who made a move, from the event that explains it. Anything not written
    by the machine, the harness or the user's own commands is taken as the
    user's hand at a CLI or dashboard -- and still has to be a table row."""
    by = str(payload.get("by") or "")
    if by in ("accept", "rework", "reopen"):
        return "user"
    if by in (AUTO_ADVANCE_AUTHOR, "kanban-escalator"):
        return "reconciler"
    if kind == "handed_off":
        return str(payload.get("from_role") or "unknown")
    if kind in _DISPATCHER_EVENTS or by == "dispatcher":
        return "dispatcher"
    return "user"


def audit_moves(conn: sqlite3.Connection, spec: Any, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Status changes since the last tick whose (from, to, actor) is no row of
    the table. The first tick only records what it sees. Compares snapshots,
    so a card that moves and moves back within one tick is not seen."""
    seen: Dict[str, str] = state.setdefault("statuses", {})
    last_event = int(state.get("event_id") or 0)
    rows = conn.execute("SELECT id, title, status FROM tasks").fetchall()
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
    illegal: List[Dict[str, Any]] = []
    first = not seen
    for row in rows:
        before, now = seen.get(row["id"]), row["status"]
        seen[row["id"]] = now
        if first or before is None or before == now:
            continue
        events = conn.execute(
            "SELECT id, kind, payload FROM task_events WHERE task_id = ? AND id > ? "
            "AND kind != 'heartbeat' ORDER BY id", (row["id"], last_event)).fetchall()
        # The event that explains the move: a hand-off first (it writes a
        # follow-up event of its own), else the latest one.
        cause = next((e for e in events if e["kind"] == "handed_off"),
                     events[-1] if events else None)
        kind = cause["kind"] if cause else "none"
        actor = _actor(kind, _payload(cause["payload"]) if cause else {})
        frm, to = spec.column_of(before), spec.column_of(now)
        if frm and to and spec.allows(frm, to, actor):
            continue
        illegal.append({"card_id": row["id"], "title": row["title"] or "",
                        "from": frm or before, "to": to or now, "actor": actor,
                        "event": kind, "event_id": cause["id"] if cause else 0})
    state["event_id"] = int(top)
    return illegal


# -- install check: every kanban profile has THE gated harness loaded ----------

#: The one harness a profile may load: this repository's, gated.
def _canonical_harness() -> Optional[Path]:
    """The gated harness in the hermes-workflows checkout, or None.

    launchd runs a COPY of this file outside the checkout, where its own
    location says nothing; there HERMES_WORKFLOWS_REPO names the checkout.
    Unlocatable is reported as a fault on every profile, never guessed."""
    env = os.environ.get("HERMES_WORKFLOWS_REPO", "").strip()
    repo = Path(os.path.expanduser(env)) if env else Path(__file__).resolve().parents[2]
    harness = (repo / "plugins" / "kanban-harness").resolve()
    return harness if (harness / "__init__.py").is_file() else None


CANONICAL_HARNESS = _canonical_harness()


def _profile_has_kanban(cfg: Mapping[str, Any]) -> bool:
    """Mirror of the agent's `_profile_has_kanban_toolset`: the `kanban`
    toolset in `toolsets` or any `platform_toolsets` list."""
    lists: List[Any] = [cfg.get("toolsets")]
    platform = cfg.get("platform_toolsets")
    if isinstance(platform, Mapping):
        lists.extend(platform.values())
    return any(isinstance(l, (list, tuple)) and "kanban" in l for l in lists)


def harness_install_faults(hermes_home: Optional[str] = None) -> List[Dict[str, str]]:
    """Every profile that can reach kanban tools but does not have the gated
    harness LOADED. Checks what loads, not what is listed: listed-but-not-linked
    is exactly how the harness was absent for a whole run."""
    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover
        return [{"profile": "*", "why": "PyYAML missing: cannot read profile configs"}]
    root = Path(os.path.expanduser(hermes_home or os.environ.get("HERMES_HOME") or "~/.hermes"))
    canonical = _canonical_harness()
    faults: List[Dict[str, str]] = []
    for profile in sorted((root / "profiles").glob("*")):
        cfg_path = profile / "config.yaml"
        if not cfg_path.exists():
            continue
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception as exc:
            faults.append({"profile": profile.name, "why": f"config unreadable ({exc})"})
            continue
        if not isinstance(cfg, Mapping) or not _profile_has_kanban(cfg):
            continue
        enabled = ((cfg.get("plugins") or {}).get("enabled") or []) \
            if isinstance(cfg.get("plugins"), Mapping) else []
        link = profile / "plugins" / "kanban-harness"
        if "kanban-harness" not in enabled:
            why = "has kanban tools but kanban-harness is not in plugins.enabled"
        elif not link.exists():
            why = f"lists kanban-harness but it is not linked at {link}, so it never loads"
        elif canonical is None:
            why = ("cannot locate the gated harness to compare against: this escalator "
                   "runs outside the hermes-workflows checkout; set HERMES_WORKFLOWS_REPO")
        elif link.resolve() != canonical:
            why = (f"loads {link.resolve()}, which is not the gated harness "
                   f"({canonical})")
        elif not (link / "__init__.py").read_text().startswith("# @user-gated"):
            why = "the loaded harness is not marked @user-gated"
        else:
            continue
        faults.append({"profile": profile.name, "why": why})
    return faults


def caller_purpose_tag(profile: str, board: str, card_id: str) -> str:
    """The ``X-Caller-Purpose`` value a worker for this card sends upstream.

    The backend proxy now records ``hermes:<profile>:<board>/<task_id>`` on
    every request, which makes "is this card's worker actually using the box?"
    answerable from OUTSIDE the agent.  That is the corroborating signal for a
    stalled card: fresh heartbeats AND no board progress AND no model traffic
    is a deadlock, not a long think.

    This helper only BUILDS the tag.  Querying the backend is deliberately not
    done here -- the proxy belongs to another owner and a monitoring job has no
    business dialling it.  Pass your own probe into :func:`find_stalled` as
    ``activity_probe`` if you want the third signal; without it the detection
    falls back to the two board-side signals, which already catch the case.
    """
    return f"hermes:{profile or 'default'}:{board or 'default'}/{card_id}"


def _payload(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _trigger_outcome(payload: Mapping[str, Any]) -> str:
    """What actually caused this gave_up.

    ``_record_task_failure`` puts the originating outcome in the event payload
    as ``trigger_outcome``.  When the error text shows a local-queue abort we
    refine it further to ``backend_busy`` -- that is how a card killed by a
    ten-hour model outage gets recognised as machine-caused even though the
    dispatcher labelled it ``crashed``.
    """
    outcome = failure_policy.classify_outcome(payload.get("trigger_outcome"))
    error = str(payload.get("error") or "")
    if error and looks_like_backend_busy(error):
        return BACKEND_BUSY
    return outcome or "unknown"


# -- 1. reconcile the breaker -----------------------------------------------

def reconcile_breaker(
    conn: sqlite3.Connection,
    *,
    policy: Mapping[str, bool],
    apply: bool,
    now: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Give back the lives the machine took.

    For every card sitting at ``blocked`` because the breaker tripped, look at
    the ``gave_up`` event that put it there.  When the trigger outcome is
    configured NOT to count, clear ``consecutive_failures`` and return the card
    to ``ready``.

    Returns one record per card considered, whether or not it was changed, so
    the caller can log a complete picture of a tick.
    """
    now = int(now if now is not None else time.time())
    records: List[Dict[str, Any]] = []

    rows = conn.execute(
        "SELECT id, title, status, consecutive_failures, last_failure_error "
        "FROM tasks WHERE status = 'blocked' AND consecutive_failures > 0"
    ).fetchall()

    for row in rows:
        card_id = row["id"]
        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'gave_up' "
            "ORDER BY id DESC LIMIT 1",
            (card_id,),
        ).fetchone()
        if event is None:
            # Blocked for some other reason (a worker asked for review). Not
            # ours to undo -- only to escalate, which the event pass handles.
            continue
        if open_split_card(conn, card_id):
            # Its cut is with the planner: not retried while that card is open.
            continue

        payload = _payload(event["payload"])
        outcome = _trigger_outcome(payload)
        counts = policy.get(outcome, True)
        record = {
            "card_id": card_id,
            "title": row["title"],
            "trigger_outcome": outcome,
            "counts_toward_breaker": counts,
            "failures": int(row["consecutive_failures"] or 0),
            "recovered": False,
        }
        if not counts:
            if apply:
                _recover(conn, card_id, outcome, record["failures"], now)
            record["recovered"] = True
        records.append(record)

    return records


def _recover(
    conn: sqlite3.Connection,
    card_id: str,
    outcome: str,
    failures: int,
    now: int,
) -> None:
    """Clear the counter and put the card back on the board.

    Both halves are required and must happen together: ``recompute_ready``
    refuses to auto-recover a card whose counter is at the limit, so resetting
    the status without resetting the counter would put the card straight back
    into the same trap on its next failure.  The ``unblocked`` event is the
    kind the agent's own sticky-block predicate understands, so an auto-recover
    and a human ``hermes kanban unblock`` leave the board in the same state.
    """
    with conn:
        conn.execute(
            "UPDATE tasks SET status = 'ready', consecutive_failures = 0, "
            "last_failure_error = NULL, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status = 'blocked'",
            (card_id,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'unblocked', ?, ?)",
            (
                card_id,
                json.dumps({
                    "by": "kanban-escalator",
                    "reason": "machine-caused failure does not count toward the breaker",
                    "trigger_outcome": outcome,
                    "failures_cleared": failures,
                }),
                now,
            ),
        )


# -- 1b. retry limit 1: a real failure blocks, the cut goes to the planner ---

#: The goals engine's block reason when a goal-mode worker ran out of turns.
_TURN_BUDGET = re.compile(r"exhausted its turn budget \((\d+)/(\d+)\)")
#: A crash whose error says the provider rate-limited us is a queue, not a fault.
_RATE_LIMITED = re.compile(r"\b429\b|rate.?limit|too many requests|overloaded", re.I)
SPLIT_MARKER = "split-of:"
_ORDINALS = ("first", "second", "third")


def _real_failure(run: Mapping[str, Any]) -> Optional[str]:
    """'budget' or 'crash' when this ended run is a failure that counts under
    retry limit 1, else None. A wait-class crash (backend busy or absent, a rate
    limit) never counts: the machine had no capacity, the card did not fail."""
    outcome = str(run["outcome"] or "")
    text = str(run["error"] or run["summary"] or "")
    if outcome == "timed_out":
        return "budget"
    if outcome == "blocked" and _TURN_BUDGET.search(str(run["summary"] or "")):
        return "budget"
    if outcome == "crashed":
        if looks_like_backend_busy(text) or _RATE_LIMITED.search(text):
            return None
        return "crash"
    return None


def open_split_card(conn: sqlite3.Connection, card_id: str) -> Optional[str]:
    """The id of an open planner card cut from ``card_id``, if there is one.
    The relation is the body line ``split-of: <id>``, not a task_links row:
    upstream's claim_task demotes a child of an undone parent to todo, so a
    linked planner card could never be claimed while its parent is blocked."""
    for row in conn.execute(
            "SELECT id, body FROM tasks WHERE body LIKE ? "
            "AND status NOT IN ('done', 'archived') ORDER BY created_at, id",
            (f"%{SPLIT_MARKER} {card_id}%",)):
        if f"{SPLIT_MARKER} {card_id}" in (row["body"] or "").splitlines():
            return row["id"]
    return None


def _last_handover(conn: sqlite3.Connection, card_id: str) -> str:
    """The worker's last hand-over, verbatim. The harness writes it twice: as
    the run summary (outcome handed_off) and as a comment; the run is exact."""
    row = conn.execute(
        "SELECT summary FROM task_runs WHERE task_id = ? AND outcome = 'handed_off' "
        "AND summary IS NOT NULL AND summary != '' ORDER BY id DESC LIMIT 1",
        (card_id,)).fetchone()
    if row:
        return row["summary"]
    row = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE '[hand-off%' "
        "ORDER BY id DESC LIMIT 1", (card_id,)).fetchone()
    return row["body"] if row else ""


def _run_line(run: Mapping[str, Any]) -> str:
    took = (_duration(int(run["ended_at"]) - int(run["started_at"]))
            if run["ended_at"] and run["started_at"] else "still open")
    text = " ".join(str(run["error"] or run["summary"] or "").split())
    if len(text) > 160:
        text = text[:157] + "..."
    return f"- run {run['id']}: {run['outcome'] or 'running'}, {took}" + (
        f", {text}" if text else "")


def split_on_real_failure(
    conn: sqlite3.Connection,
    board_spec: Any,
    *,
    apply: bool,
    now: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Retry limit 1. A card whose latest run exhausted its budget, or crashed
    for a reason that was not waiting, is blocked on that first occurrence with
    a plain-sentence comment, and exactly one planner card is cut from it.

    The block is taken whichever status the card is in when this tick sees the
    failed run: the dispatcher has usually requeued it to ready already. The
    audit then sees in_progress -> question by reconciler when its last look
    was the running card, and to_do -> question when a tick saw it requeued
    first -- the latter is no row of the table. Nothing here can stop the
    dispatcher from claiming the card again before this tick runs."""
    now = int(now if now is not None else time.time())
    planner_role = (board_spec.roles or {}).get("planner")
    if planner_role is None:
        return []
    planner = str((planner_role or {}).get("assignee") or "planner")
    done: List[Dict[str, Any]] = []
    cards = conn.execute(
        "SELECT id, title, status, assignee FROM tasks "
        "WHERE status NOT IN ('done', 'archived', 'running')").fetchall()
    for card in cards:
        if card["assignee"] == planner:
            continue  # a planner's own failure is not cut again
        runs = conn.execute(
            "SELECT id, outcome, started_at, ended_at, summary, error FROM task_runs "
            "WHERE task_id = ? ORDER BY id", (card["id"],)).fetchall()
        if not runs or runs[-1]["ended_at"] is None:
            continue
        last = runs[-1]
        kind = _real_failure(last)
        if kind is None:
            continue
        handled = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "AND run_id = ? AND payload LIKE ?",
            (card["id"], last["id"], f'%"by": "{AUTO_ADVANCE_AUTHOR}"%')).fetchone()
        if handled:
            continue
        nth = sum(1 for r in runs if _real_failure(r))
        on = f"the {_ORDINALS[nth - 1]} run" if nth <= len(_ORDINALS) else f"run {nth}"
        split_id = open_split_card(conn, card["id"])
        new_card = split_id is None
        if new_card:
            split_id = f"t_{secrets.token_hex(4)}"
        if kind == "budget":
            turns = _TURN_BUDGET.search(str(last["summary"] or ""))
            spent = (f"{turns.group(1)} turns" if turns else
                     _duration(int(last["ended_at"]) - int(last["started_at"] or last["ended_at"])))
            reason = f"budget exhausted after {spent} on {on}"
        else:
            error = " ".join(str(last["error"] or "").split())[:120] or "no error recorded"
            reason = f"crashed on {on} ({error})"
        reason += f"; the cut is with the planner as {split_id}"
        record = {"card_id": card["id"], "title": card["title"], "kind": kind,
                  "reason": reason, "planner_card": split_id, "created": new_card}
        done.append(record)
        if not apply:
            continue
        handover = _last_handover(conn, card["id"])
        body = "\n".join([
            f"{SPLIT_MARKER} {card['id']}",
            f"Split of {card['id']}: {card['title']}",
            f"It was blocked: {reason}.",
            "Cut it into child cards, each fitting its budget.",
            "",
            "Run history:",
            *[_run_line(r) for r in runs],
            "",
            "Last hand-over (verbatim):",
            handover or ("No hand-over: the failed card's runs recorded none, so this "
                         "card carries no account of what was learned inside the work "
                         "(a worker that never initialised writes none). Do not invent a "
                         "cut from the title alone: read the card and its worker log, "
                         "and ask in the question column if the work is unclear."),
        ])
        with conn:
            conn.execute(
                "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL WHERE id = ?", (card["id"],))
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, 'blocked', ?, ?)",
                (card["id"], last["id"], json.dumps({
                    "by": AUTO_ADVANCE_AUTHOR, "reason": reason,
                    "planner_card": split_id}), now))
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)", (card["id"], AUTO_ADVANCE_AUTHOR, reason, now))
            if new_card:
                # Born straight into ready (to_do). The table's row for this birth
                # (refinement -> to_do by reconciler) is not checked at run time:
                # audit_moves only judges a status change of a card it saw on an
                # earlier tick, and a card born this tick has no earlier status.
                conn.execute(
                    "INSERT INTO tasks (id, title, body, assignee, status, created_by, "
                    "created_at) VALUES (?, ?, ?, ?, 'ready', ?, ?)",
                    (split_id, f"Split {card['id']}: {card['title']}"[:200], body,
                     planner, AUTO_ADVANCE_AUTHOR, now))
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, NULL, 'created', ?, ?)",
                    (split_id, json.dumps({"by": AUTO_ADVANCE_AUTHOR,
                                           "split_of": card["id"]}), now))
    return done


# -- 2. stranded cards ------------------------------------------------------

def find_stranded(
    conn: sqlite3.Connection,
    *,
    stranded_after: int = DEFAULT_STRANDED_AFTER,
    now: Optional[int] = None,
    profiles: Optional[set] = None,
    ignore: Optional[IgnoreRules] = None,
    max_in_progress: Optional[int] = None,
    max_in_progress_per_profile: Optional[int] = None,
) -> Tuple[List[sqlite3.Row], List[Dict[str, Any]]]:
    """Cards that are ``ready`` and **nobody can run**.

    Returns ``(stranded_rows, suppressed_records)``.  Suppressions are returned
    rather than silently dropped so a tick's report shows what was considered
    and why it was not escalated.

    The candidate shape is: still ``ready``, no live claim, and the most recent
    activity on the card (its last run, else its creation) older than
    ``stranded_after``.  That alone is not enough — it is the flaw in the naive
    rule, which scores one card without ever looking at the board.  Three
    suppressions turn "nobody has run it yet" into "nobody can run it":

    1. **The human lane.**  An assignee that maps to no profile is a review
       gate parked on a person's name.  No dispatcher will ever claim it; that
       is the point.  Permanently ``ready`` and permanently fine.

    2. **An explicit ignore rule.**  Card ids, assignees, or title globs a
       human has declared permanently uninteresting.

    3. **Queued behind capacity.**  The dispatcher is at ``max_in_progress``
       AND this card's assignee already has a worker running elsewhere.  The
       card is not stuck, it is next in line.  Both halves are required: a full
       board with this assignee idle means the card really is being passed
       over, which is worth a page.
    """
    now = int(now if now is not None else time.time())
    cutoff = now - int(stranded_after)
    profiles = known_profiles() if profiles is None else profiles
    ignore = ignore or IgnoreRules()

    candidates = conn.execute(
        "SELECT t.id, t.title, t.assignee, t.created_at, "
        "       t.last_failure_error, t.claim_lock, t.claim_expires, "
        "       COALESCE(MAX(r.started_at), t.created_at) AS last_activity "
        "FROM tasks t LEFT JOIN task_runs r ON r.task_id = t.id "
        "WHERE t.status = 'ready' "
        "  AND (t.claim_lock IS NULL OR COALESCE(t.claim_expires, 0) < ?) "
        "GROUP BY t.id "
        "HAVING last_activity < ? "
        "ORDER BY last_activity",
        (now, cutoff),
    ).fetchall()
    if not candidates:
        return [], []

    running_total, busy_assignees = _running_snapshot(conn)
    per_assignee: Dict[str, int] = {}
    for r in conn.execute("SELECT assignee FROM tasks WHERE status = 'running'"):
        per_assignee[r[0] or ""] = per_assignee.get(r[0] or "", 0) + 1
    at_capacity = (
        max_in_progress is not None and running_total >= int(max_in_progress)
    )

    stranded: List[sqlite3.Row] = []
    suppressed: List[Dict[str, Any]] = []
    for row in candidates:
        assignee = row["assignee"] or ""
        reason = ignore.matches(row["id"], row["title"], assignee)
        if reason is None and assignee and assignee not in profiles:
            reason = (
                f"assignee {assignee!r} maps to no profile -- this is the human "
                "lane, and no worker is ever meant to claim it"
            )
        if (reason is None and max_in_progress_per_profile is not None
                and per_assignee.get(assignee, 0) >= int(max_in_progress_per_profile)):
            reason = (
                f"{assignee!r} is at its per-profile cap "
                f"({per_assignee[assignee]}/{max_in_progress_per_profile} running) "
                "-- the card is next in line for its role, not stuck"
            )
        if reason is None and at_capacity and assignee in busy_assignees:
            reason = (
                f"dispatcher is at capacity ({running_total}/{max_in_progress}) "
                f"and {assignee!r} already has a worker running -- the card is "
                "queued, not stuck"
            )
        if reason is None:
            stranded.append(row)
        else:
            suppressed.append({
                "card_id": row["id"],
                "title": row["title"],
                "kind": "stranded",
                "suppressed_because": reason,
            })
    return stranded, suppressed


def _clear_stale_claim(conn: sqlite3.Connection, row: Mapping[str, Any], now: int,
                       apply: bool) -> str:
    """A ready card still carrying an expired claim lock can never be claimed:
    upstream's claim requires ``claim_lock IS NULL``, and the dispatcher skips
    it without a word. With --apply, clear the lock (ready, expired, nothing
    running) and record why. Returns the sentence for the page."""
    age = _duration(now - int(row["claim_expires"] or now))
    said = (f"ready but holds a stale claim lock ({row['claim_lock']}, expired {age} "
            "ago): the dispatcher can never claim it")
    if not apply:
        return said
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET claim_lock = NULL, claim_expires = NULL "
            "WHERE id = ? AND status = 'ready' AND claim_lock = ? "
            "AND COALESCE(claim_expires, 0) < ? AND NOT EXISTS ("
            "  SELECT 1 FROM task_runs r WHERE r.task_id = tasks.id AND r.ended_at IS NULL)",
            (row["id"], row["claim_lock"], now))
        if cur.rowcount != 1:
            return said
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'claim_cleared', ?, ?)",
            (row["id"], json.dumps({"by": AUTO_ADVANCE_AUTHOR, "lock": row["claim_lock"],
                                    "reason": "stale claim lock on a ready card"}), now))
    return said + "; the lock is now cleared"


#: A run this many times its card's median successful run is worth a page.
OVERRUN_FACTOR = 3
#: Successful runs a card needs before its history is trusted.
OVERRUN_MIN_HISTORY = 2
_SUCCESSFUL_OUTCOMES = ("completed", "handed_off", "done")


def find_overrunning(conn: sqlite3.Connection, *, now: Optional[int] = None,
                     ignore: Optional[IgnoreRules] = None) -> List[Dict[str, Any]]:
    """Running cards whose current run is far past the card's own history.

    The second stalled signal. A worker that keeps writing events defeats the
    no-progress rule while going nowhere; its run length does not lie. Only a
    card with enough successful runs to have a history is judged, and nothing
    is killed: a slow run on a busy box is a queue, so this only pages."""
    now = int(now if now is not None else time.time())
    ignore = ignore or IgnoreRules()
    found: List[Dict[str, Any]] = []
    rows = conn.execute(
        "SELECT t.id, t.title, t.assignee, t.worker_pid, "
        "       (SELECT MAX(r.started_at) FROM task_runs r "
        "        WHERE r.task_id = t.id AND r.ended_at IS NULL) AS run_started "
        "FROM tasks t WHERE t.status = 'running'").fetchall()
    marks = ",".join("?" * len(_SUCCESSFUL_OUTCOMES))
    for row in rows:
        if not row["run_started"] or ignore.matches(row["id"], row["title"], row["assignee"]):
            continue
        past = sorted(int(r[0]) for r in conn.execute(
            f"SELECT ended_at - started_at FROM task_runs WHERE task_id = ? "
            f"AND ended_at IS NOT NULL AND outcome IN ({marks})",
            (row["id"], *_SUCCESSFUL_OUTCOMES)))
        if len(past) < OVERRUN_MIN_HISTORY:
            continue
        median = past[len(past) // 2]
        elapsed = now - int(row["run_started"])
        if median > 0 and elapsed >= OVERRUN_FACTOR * median:
            found.append({"row": row, "elapsed": elapsed, "median": median})
    return found


#: A worker whose log has not been written for this long, while its heartbeat
#: is fresh, is not doing anything a person could see.
SILENT_LOG_AFTER = 30 * 60


def find_silent_workers(conn: sqlite3.Connection, *, board: str,
                        hermes_home: Optional[str], now: Optional[int] = None,
                        heartbeat_fresh_within: int = DEFAULT_HEARTBEAT_FRESH_WITHIN,
                        ignore: Optional[IgnoreRules] = None) -> List[Dict[str, Any]]:
    """Running cards with a fresh heartbeat whose worker log has gone quiet.

    The heartbeat can be emitted for an agent that never came up: the log stops
    at 'Initializing agent...' while the card looks healthy in every view. The
    log's mtime is free and unambiguous. Pages; never kills (a worker waiting
    on a busy model writes nothing either, and that is a queue)."""
    now = int(now if now is not None else time.time())
    ignore = ignore or IgnoreRules()
    found: List[Dict[str, Any]] = []
    for row in conn.execute(
            "SELECT id, title, assignee, worker_pid, started_at, last_heartbeat_at "
            "FROM tasks WHERE status = 'running' AND last_heartbeat_at >= ?",
            (now - int(heartbeat_fresh_within),)).fetchall():
        if ignore.matches(row["id"], row["title"], row["assignee"]):
            continue
        log = Path(worker_log_path(board, row["id"], hermes_home))
        try:
            written = int(log.stat().st_mtime)
        except OSError:
            continue  # no log to read is not evidence of silence
        if now - written >= SILENT_LOG_AFTER:
            found.append({"row": row, "silent_for": now - written, "log": str(log)})
    return found


#: A worker's open wait note, written by the visible-wait agent patch
#: (scripts/harness-patches/build.py) under a sentence a person reads.
_WAIT_NOTE = re.compile(r"\[visible-wait open endpoint=(\S+) model=(\S+) "
                        r"since=(\d+) updated=(\d+)\]")
#: A wait shorter than this is a slow first token, not an outage.
BACKEND_PAGE_AFTER = 5 * 60
#: A worker updates its note every 30 s. A note nobody updated for this long
#: belongs to a worker that is gone, and proves nothing about the backend.
WAIT_NOTE_FRESH_WITHIN = 3 * 60


def find_backend_waits(conn: sqlite3.Connection, *,
                       now: Optional[int] = None) -> Tuple[set, Dict[Tuple[str, str], Dict[str, Any]]]:
    """Running cards whose worker is waiting on a backend, and the outages.

    Returns ``(waiting card ids, {(endpoint, model): outage})``. An outage is
    one endpoint and model that at least one worker has waited on for
    ``BACKEND_PAGE_AFTER``: three workers on it are one fact, not three."""
    now = int(now if now is not None else time.time())
    waiting: set = set()
    outages: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in conn.execute(
            "SELECT c.task_id, c.body FROM task_comments c JOIN tasks t ON t.id = c.task_id "
            "WHERE t.status = 'running' AND c.body LIKE '%[visible-wait open %' "
            "ORDER BY c.id").fetchall():
        m = _WAIT_NOTE.search(row["body"] or "")
        if not m or now - int(m.group(4)) > WAIT_NOTE_FRESH_WITHIN:
            continue
        waiting.add(row["task_id"])
        since = int(m.group(3))
        if now - since < BACKEND_PAGE_AFTER:
            continue
        outage = outages.setdefault((m.group(1), m.group(2)), {"cards": [], "since": since})
        outage["cards"].append(row["task_id"])
        outage["since"] = min(outage["since"], since)
    return waiting, outages


def _running_snapshot(conn: sqlite3.Connection) -> Tuple[int, set]:
    """How many cards are running, and which assignees are occupied."""
    rows = conn.execute(
        "SELECT assignee FROM tasks WHERE status = 'running'"
    ).fetchall()
    return len(rows), {r["assignee"] or "" for r in rows}


# -- 3. stalled cards: alive, heartbeating, going nowhere -------------------

def find_stalled(
    conn: sqlite3.Connection,
    *,
    stalled_after: int = DEFAULT_STALLED_AFTER,
    heartbeat_fresh_within: int = DEFAULT_HEARTBEAT_FRESH_WITHIN,
    now: Optional[int] = None,
    ignore: Optional[IgnoreRules] = None,
    activity_probe: Optional[Any] = None,
    board: str = "default",
) -> List[Dict[str, Any]]:
    """Running cards whose worker is alive but idle.

    **A heartbeat means the process lives, not that the work moves.**  This is
    the deadlock everything else misses: the card is ``running``, the claim is
    healthy, heartbeats land every minute, and the board looks perfect — while
    the session it dispatched has been hung for hours with nothing happening.
    Nothing reaps it until the ``max_runtime`` cap, which can be most of a day.

    Two board-side signals, both required:

    * ``last_heartbeat_at`` is FRESH (within ``heartbeat_fresh_within``).  A
      card whose heartbeats stopped is a crash, and the dispatcher's own
      ``detect_crashed_workers`` already handles that one.
    * No task event **other than** ``heartbeat`` for longer than
      ``stalled_after``.  Heartbeats are excluded precisely because they are
      the signal that lies; every other event kind means something moved.

    ``activity_probe`` is the optional third signal: a callable taking the
    ``X-Caller-Purpose`` tag (see :func:`caller_purpose_tag`) and returning
    True when the backend has seen traffic for this card recently.  When it
    returns True the card is thinking, not hung, and is not escalated.  It is
    injected rather than implemented here on purpose — the backend proxy has
    its own owner, and a monitoring job has no business dialling it.
    """
    now = int(now if now is not None else time.time())
    ignore = ignore or IgnoreRules()
    progress_cutoff = now - int(stalled_after)
    heartbeat_cutoff = now - int(heartbeat_fresh_within)

    rows = conn.execute(
        "SELECT t.id, t.title, t.assignee, t.worker_pid, t.started_at, "
        "       t.last_heartbeat_at, "
        "       COALESCE("
        "         (SELECT MAX(e.created_at) FROM task_events e "
        "          WHERE e.task_id = t.id AND e.kind != 'heartbeat'), "
        "         t.started_at, t.created_at"
        "       ) AS last_progress "
        "FROM tasks t "
        "WHERE t.status = 'running' "
        "  AND t.last_heartbeat_at IS NOT NULL "
        "  AND t.last_heartbeat_at >= ? "
        "ORDER BY last_progress",
        (heartbeat_cutoff,),
    ).fetchall()

    stalled: List[Dict[str, Any]] = []
    for row in rows:
        last_progress = int(row["last_progress"] or now)
        if last_progress >= progress_cutoff:
            continue
        if ignore.matches(row["id"], row["title"], row["assignee"]):
            continue
        if activity_probe is not None:
            try:
                tag = caller_purpose_tag(row["assignee"] or "", board, row["id"])
                if activity_probe(tag):
                    continue  # the model is working; a long think is not a stall
            except Exception:
                # A probe that cannot answer must not suppress a real stall.
                pass
        stalled.append({
            "row": row,
            "idle_seconds": now - last_progress,
            "heartbeat_age": now - int(row["last_heartbeat_at"] or now),
        })
    return stalled


# -- the tick ---------------------------------------------------------------

def run_once(
    db_path: str,
    *,
    board: str = "default",
    config: Optional[Mapping[str, Any]] = None,
    apply: bool = False,
    cursor_path: Optional[str] = None,
    sender: Optional[escalation.Sender] = None,
    notify: bool = True,
    stranded_after: int = DEFAULT_STRANDED_AFTER,
    stalled_after: int = DEFAULT_STALLED_AFTER,
    heartbeat_fresh_within: int = DEFAULT_HEARTBEAT_FRESH_WITHIN,
    max_in_progress: Optional[int] = None,
    activity_probe: Optional[Any] = None,
    repeat_after: int = DEFAULT_REPEAT_AFTER,
    state_path: Optional[str] = None,
    board_spec: Optional[Any] = None,
    now: Optional[int] = None,
    hermes_home: Optional[str] = None,
) -> Dict[str, Any]:
    """One reconcile + escalate pass.  Returns a structured report.

    A push must mean *"this is stuck and nothing else will come through"* —
    one push per stuck card, never a stream.  Two mechanisms keep it that way:
    the event cursor for event-shaped escalations, and :class:`PageLog` for
    the state-derived ones (``stranded`` / ``stalled``) that are recomputed
    every tick and would otherwise repeat forever.

    ``notify=False`` swaps in the null sender: every message is still rendered
    and reported, nothing leaves the box, and nothing is recorded in the page
    log — so inspecting a tick never pages anyone and never silences a later
    real page.
    """
    config = config or {}
    now = int(now if now is not None else time.time())
    policy = failure_policy.resolve_policy(config)
    if sender is None and not notify:
        sender = escalation.NullSender()

    esc_cfg = config.get("escalation") if isinstance(config.get("escalation"), Mapping) else {}
    ignore = IgnoreRules(esc_cfg.get("ignore"))
    kanban_cfg = config.get("kanban") if isinstance(config.get("kanban"), Mapping) else {}
    if max_in_progress is None:
        max_in_progress = kanban_cfg.get("max_in_progress")
    max_in_progress_per_profile = kanban_cfg.get("max_in_progress_per_profile")
    stranded_after = int(esc_cfg.get("stranded_after_seconds") or stranded_after)
    stalled_after = int(esc_cfg.get("stalled_after_seconds") or stalled_after)
    heartbeat_fresh_within = int(
        esc_cfg.get("heartbeat_fresh_within_seconds") or heartbeat_fresh_within
    )
    repeat_after = int(esc_cfg.get("repeat_after_seconds") or repeat_after)
    notify_on = _notify_allowlist(esc_cfg.get("notify_on"))
    send_resolution = _as_bool(esc_cfg.get("send_resolution"), default=True)

    uri = f"file:{db_path}?mode={'rw' if apply else 'ro'}"
    conn = sqlite3.connect(uri, uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        report: Dict[str, Any] = {
            "board": board,
            "apply": apply,
            "policy": dict(policy),
            "reconciled": [],
            "escalations": [],
            "suppressed": [],
            "resolutions": [],
            "advanced": [],
            "archived": [],
            "split": [],
            "board_issues": [],
            "cursor": None,
        }

        report["reconciled"] = reconcile_breaker(
            conn, policy=policy, apply=apply, now=now,
        )

        # Board rules run only when a specification was passed explicitly.
        if board_spec is not None:
            report["board_issues"] = [str(i) for i in board_spec.issues]
            report["split"] = split_on_real_failure(conn, board_spec, apply=apply, now=now)
            report["advanced"] = auto_advance(conn, board_spec, apply=apply, now=now)
            report["archived"] = auto_archive(conn, board_spec, apply=apply, now=now)
        recovered = {
            r["card_id"] for r in report["reconciled"] if r["recovered"]
        }

        # Persisting the page log follows the SAME rule as the cursor, and is
        # additionally gated on ``notify``: a dry run must never consume a
        # cooldown and silence the next real page.
        persist = bool(apply or cursor_path or state_path)
        log_file = Path(state_path) if state_path else _default_page_log(db_path)
        page_log = PageLog(log_file if persist else None)
        record_pages = notify and persist

        def page(esc: escalation.Escalation, fingerprint: str, status: str = "") -> bool:
            """Escalate once.  Returns True when the message was delivered.

            Every suppression is REPORTED, never hidden — an operator must be
            able to see that a card was considered and why it stayed quiet."""
            if esc.kind not in notify_on:
                report["suppressed"].append({
                    "card_id": esc.card_id, "title": esc.title, "kind": esc.kind,
                    "suppressed_because": (
                        f"{esc.kind!r} is not in escalation.notify_on "
                        f"({', '.join(sorted(notify_on))})"
                    ),
                })
                return True
            ok, reason = page_log.should_page(
                esc.card_id, esc.kind, fingerprint,
                now=now, repeat_after=repeat_after,
            )
            if not ok:
                report["suppressed"].append({
                    "card_id": esc.card_id, "title": esc.title, "kind": esc.kind,
                    "suppressed_because": reason,
                })
                return True
            record = escalation.escalate(esc, config, sender=sender)
            record["page_reason"] = reason
            report["escalations"].append(record)
            if record["delivered"] and record_pages and esc.kind not in _NO_RESOLUTION:
                page_log.record(
                    esc.card_id, esc.kind, fingerprint,
                    now=now, title=esc.title, status=status,
                )
            return bool(record["delivered"])

        cursor_file = Path(cursor_path) if cursor_path else _default_cursor(db_path)
        cursor = _read_cursor(conn, cursor_file)
        rows = conn.execute(
            "SELECT e.id AS eid, e.task_id, e.kind, e.payload, e.run_id, t.title "
            "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
            "WHERE e.id > ? ORDER BY e.id",
            (cursor,),
        ).fetchall()

        last = cursor
        for row in rows:
            if row["kind"] == "completed" and board_spec is not None:
                # accept is the only door to done on a board; anything else
                # that closed a card is made visible. Detection, not
                # prevention: a human using the runtime's own `complete` is the
                # board's owner using their own tool.
                payload = _payload(row["payload"])
                if payload.get("by") != "accept":
                    esc = escalation.Escalation(
                        card_id=row["task_id"], title=row["title"] or "", board=board,
                        kind="done_without_accept",
                        last_error=str(payload.get("summary") or ""),
                        worker_log=worker_log_path(board, row["task_id"], hermes_home),
                    )
                    if not page(esc, f"event:{row['eid']}", "done"):
                        break
                last = row["eid"]
                continue
            if row["kind"] not in ESCALATE_KINDS:
                last = row["eid"]
                continue
            payload = _payload(row["payload"])
            if (row["kind"] == "blocked" and payload.get("by") != AUTO_ADVANCE_AUTHOR
                    and conn.execute(
                        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                        "AND run_id IS ? AND id > ? AND payload LIKE ?",
                        (row["task_id"], row["run_id"], row["eid"],
                         f'%"by": "{AUTO_ADVANCE_AUTHOR}"%')).fetchone()):
                # The same run was blocked again by the reconciler, whose event
                # names the planner card: one page, the one that names the cut.
                last = row["eid"]
                continue
            esc = _escalation_for_event(
                row, payload, board=board, recovered=recovered,
                hermes_home=hermes_home,
            )
            status = _card_status(conn, row["task_id"])
            if not page(esc, f"event:{row['eid']}", status):
                # Leave the cursor BEFORE this event so the next tick retries.
                # A card nobody was told about must not be forgotten because
                # the channel was down for a minute.
                break
            last = row["eid"]

        stranded, suppressed = find_stranded(
            conn, stranded_after=stranded_after, now=now,
            profiles=known_profiles(hermes_home),
            ignore=ignore, max_in_progress=max_in_progress,
            max_in_progress_per_profile=max_in_progress_per_profile,
        )
        report["suppressed"].extend(suppressed)
        # A board at max_in_progress spawns nothing and upstream's dispatcher
        # says so nowhere; name the cap and what holds it on every such page.
        held_by = ""
        if stranded and max_in_progress is not None:
            running = [r[0] for r in conn.execute(
                "SELECT id FROM tasks WHERE status = 'running' ORDER BY started_at")]
            if len(running) >= int(max_in_progress):
                held_by = (f"held by the concurrency cap ({len(running)} running: "
                           f"{', '.join(running)})")
            elif running:
                # Upstream b88d0007c dispatch_once subtracts the running cards
                # twice when max_in_progress is set, so below the cap it still
                # spawns nothing and fills no bucket.
                held_by = (f"below the cap ({len(running)}/{max_in_progress} running), but "
                           "the dispatcher counts running cards twice when "
                           "kanban.max_in_progress is set and spawns nothing: set "
                           "max_in_progress: null and bound it with "
                           "max_in_progress_per_profile")
        open_now = set()
        for row in stranded:
            open_now.add((row["id"], "stranded"))
            esc = escalation.Escalation(
                card_id=row["id"],
                title=row["title"] or "",
                board=board,
                kind="stranded",
                last_error=row["last_failure_error"] or "",
                worker_log=worker_log_path(board, row["id"], hermes_home),
                facts={
                    "assignee": row["assignee"] or "(unassigned)",
                    "idle_for": _duration(now - int(row["last_activity"] or now)),
                    **({"why": held_by} if held_by else {}),
                    **({"claim": _clear_stale_claim(conn, row, now, apply)}
                       if row["claim_lock"] else {}),
                },
            )
            # A stranded card's condition is "ready, assigned to X". If either
            # changes the situation is genuinely different and worth re-saying;
            # while both hold, the card is the same silence as an hour ago.
            page(esc, f"ready:{row['assignee'] or ''}", "ready")

        # A worker waiting on a backend is a queue, never a stall: the page is
        # about the backend, once per endpoint and model, not per worker.
        waiting, outages = find_backend_waits(conn, now=now)
        for (endpoint, model), outage in sorted(outages.items()):
            key = f"backend:{endpoint}|{model}"
            open_now.add((key, "backend_not_serving"))
            page(escalation.Escalation(
                card_id=key, title=f"backend {endpoint} is not serving {model}",
                board=board, kind="backend_not_serving",
                last_error=(f"{len(outage['cards'])} worker(s) asked {endpoint} for {model} "
                            f"and received nothing for {_duration(now - outage['since'])}"),
                resume_command=(f"check that {model} answers on {endpoint} (a request, not "
                                "the model list); the waiting cards resume on their own"),
                facts={"endpoint": endpoint, "model": model,
                       "waiting_cards": ", ".join(outage["cards"])},
            ), "backend", "fault")

        def still_waiting(row) -> bool:
            if row["id"] not in waiting:
                return False
            report["suppressed"].append({
                "card_id": row["id"], "title": row["title"] or "", "kind": "stalled",
                "suppressed_because": "its worker is waiting on a model backend "
                                      "(the note on the card names it); a wait is not a stall",
            })
            return True

        for entry in find_stalled(
            conn, stalled_after=stalled_after, now=now,
            heartbeat_fresh_within=heartbeat_fresh_within,
            ignore=ignore, activity_probe=activity_probe, board=board,
        ):
            row = entry["row"]
            if still_waiting(row):
                continue
            open_now.add((row["id"], "stalled"))
            esc = escalation.Escalation(
                card_id=row["id"],
                title=row["title"] or "",
                board=board,
                kind="stalled",
                worker_log=worker_log_path(board, row["id"], hermes_home),
                facts={
                    "assignee": row["assignee"] or "(unassigned)",
                    "worker_pid": row["worker_pid"] or "(unknown)",
                    "no_progress_for": _duration(entry["idle_seconds"]),
                    "last_heartbeat": _duration(entry["heartbeat_age"]) + " ago",
                },
            )
            # A NEW worker pid means a new hang, not the same one — worth
            # re-saying. The same pid still wedged is the same silence.
            page(esc, f"running:{row['assignee'] or ''}:{row['worker_pid']}", "running")

        for entry in find_silent_workers(conn, board=board, hermes_home=hermes_home,
                                         now=now, ignore=ignore,
                                         heartbeat_fresh_within=heartbeat_fresh_within):
            row = entry["row"]
            if (row["id"], "stalled") in open_now or still_waiting(row):
                continue
            open_now.add((row["id"], "stalled"))
            page(escalation.Escalation(
                card_id=row["id"], title=row["title"] or "", board=board, kind="stalled",
                worker_log=entry["log"],
                facts={
                    "assignee": row["assignee"] or "(unassigned)",
                    "worker_pid": row["worker_pid"] or "(unknown)",
                    "why": (f"the worker log has not been written for "
                            f"{_duration(entry['silent_for'])} while the heartbeat is "
                            "fresh: the agent may never have started"),
                },
            ), f"silent:{row['assignee'] or ''}:{row['worker_pid']}", "running")

        for entry in find_overrunning(conn, now=now, ignore=ignore):
            row = entry["row"]
            if (row["id"], "stalled") in open_now or still_waiting(row):
                continue  # already paged as stalled this tick, or waiting
            open_now.add((row["id"], "stalled"))
            page(escalation.Escalation(
                card_id=row["id"], title=row["title"] or "", board=board, kind="stalled",
                worker_log=worker_log_path(board, row["id"], hermes_home),
                facts={
                    "assignee": row["assignee"] or "(unassigned)",
                    "worker_pid": row["worker_pid"] or "(unknown)",
                    "why": (f"this run is {_duration(entry['elapsed'])}, far past this "
                            f"card's usual {_duration(entry['median'])} (median of its "
                            "successful runs); alive is not the same as progressing"),
                },
            ), f"overrun:{row['assignee'] or ''}:{row['worker_pid']}", "running")

        # Not board-opt-in: a profile that can reach kanban tools without the
        # harness is a fault on every board, every tick.
        for fault in harness_install_faults(hermes_home):
            key = f"profile:{fault['profile']}"
            open_now.add((key, "harness_not_loaded"))
            page(escalation.Escalation(
                card_id=key, title=f"profile {fault['profile']} runs without the harness",
                board=board, kind="harness_not_loaded", last_error=fault["why"],
                resume_command=(f"link {CANONICAL_HARNESS} into "
                                f"~/.hermes/profiles/{fault['profile']}/plugins/ and list "
                                "kanban-harness in its plugins.enabled"),
            ), fault["why"], "fault")

        if board_spec is None:
            # Not checked this tick, so not cleared: an open ledger page stays open.
            open_now.add((f"ledger:{board}", "ledger_missing"))
            open_now.add((f"handoff-form:{board}", "handoff_form_missing"))
        else:
            # A spec without a hand-off form is not an error -- the harness
            # simply does not check hand-offs -- but it must never be silent.
            if board_spec.handoff is None:
                why = (f"board {board} has a spec but no hand-off form; hand-offs are "
                       "unchecked")
                open_now.add((f"handoff-form:{board}", "handoff_form_missing"))
                page(escalation.Escalation(
                    card_id=f"handoff-form:{board}", title=why,
                    board=board, kind="handoff_form_missing", last_error=why,
                    resume_command=(f"add a `handoff:` key (form, fields) to "
                                    f"{board_spec.path}"),
                ), "handoff-form", "fault")
            # The accept ledger is the durable trace of every accept. A missing or
            # corrupt one is a fault: until it is restored, every accepted card on
            # this board looks like an escaped one.
            import board_cli
            fault = board_cli.ledger_fault(conn)
            if fault:
                open_now.add((f"ledger:{board}", "ledger_missing"))
                page(escalation.Escalation(
                    card_id=f"ledger:{board}", title=f"accept ledger of board {board}",
                    board=board, kind="ledger_missing", last_error=fault,
                    resume_command=f"restore {board_cli.ledger_path(conn)} from backup",
                ), "ledger", "fault")
            else:
                # The ledger never shrinks. If it holds fewer accepts than it ever
                # has, it was deleted and re-created, or truncated -- even after GC
                # erased every accept event that would otherwise show it.
                ledger = board_cli.ledger_path(conn)
                lines = 0
                if ledger.exists():
                    with open(ledger, encoding="utf-8") as fh:
                        lines = sum(1 for line in fh if line.strip())
                high = page_log.ledger_high_water.get(board, 0)
                if lines < high:
                    open_now.add((f"ledger:{board}", "ledger_shrank"))
                    page(escalation.Escalation(
                        card_id=f"ledger:{board}", title=f"accept ledger of board {board}",
                        board=board, kind="ledger_shrank",
                        last_error=f"the accept ledger shrank from {high} to {lines} line(s)",
                        resume_command=f"restore {ledger} from backup",
                    ), f"{high}->{lines}", "fault")
                elif lines > high and record_pages:
                    page_log.ledger_high_water[board] = lines
                    page_log.dirty = True

            # THE AUDIT: moves the harness cannot prevent (the dispatcher, a
            # person's own CLI or dashboard) are checked against the table. A
            # status change whose (from, to, actor) is not a row pages once.
            for bad in audit_moves(conn, board_spec, page_log.audit.setdefault(board, {})):
                page(escalation.Escalation(
                    card_id=bad["card_id"], title=bad["title"], board=board,
                    kind="illegal_move",
                    last_error=(f"{bad['from']} -> {bad['to']} by {bad['actor']}: not a "
                                f"move on this board (event: {bad['event']})"),
                ), f"{bad['from']}->{bad['to']}:{bad['event_id']}", "audit")
            page_log.dirty = True

            profiles = known_profiles(hermes_home)
            for row in conn.execute(
                "SELECT id, title, assignee FROM tasks WHERE status = 'review'"
            ).fetchall():
                if not row["assignee"] or row["assignee"] not in profiles:
                    continue  # a person's name: the dispatcher skips it
                open_now.add((row["id"], "agent_review_lane"))
                esc = escalation.Escalation(
                    card_id=row["id"], title=row["title"] or "", board=board,
                    kind="agent_review_lane",
                    worker_log=worker_log_path(board, row["id"], hermes_home),
                    facts={"assignee": row["assignee"]},
                )
                page(esc, f"review:{row['assignee']}", "review")

        if send_resolution:
            report["resolutions"] = _close_resolved(
                conn, page_log, open_now,
                board=board, config=config, sender=sender,
                record=record_pages,
            )

        if apply or cursor_path:
            _write_cursor(cursor_file, last)
        if record_pages:
            page_log.save()
        report["cursor"] = last
        return report
    finally:
        conn.close()


def _escalation_for_event(
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    *,
    board: str,
    recovered: Iterable[str],
    hermes_home: Optional[str],
) -> escalation.Escalation:
    card_id = row["task_id"]
    kind = row["kind"]
    facts: Dict[str, Any] = {}
    for key in ("failures", "effective_limit", "trigger_outcome", "budget_seconds"):
        if payload.get(key) is not None:
            facts[key] = payload[key]
    if card_id in set(recovered):
        facts["auto_recovered"] = (
            "yes -- machine-caused, put back to ready by the escalator"
        )
    return escalation.Escalation(
        card_id=card_id,
        title=row["title"] or "",
        board=board,
        kind="gave_up" if kind == "spawn_auto_blocked" else kind,
        last_error=str(payload.get("error") or payload.get("reason") or ""),
        worker_log=worker_log_path(board, card_id, hermes_home),
        facts=facts,
    )


def _default_cursor(db_path: str) -> Path:
    return Path(db_path).resolve().parent / DEFAULT_CURSOR_NAME


def _default_page_log(db_path: str) -> Path:
    return Path(db_path).resolve().parent / DEFAULT_PAGE_LOG_NAME


def _notify_allowlist(value: Any) -> frozenset:
    """Resolve ``escalation.notify_on``.

    Unset means every kind — the safe default, since the point of this tool is
    that nothing goes unsaid.  An operator who only wants the hard breaks can
    narrow it to e.g. ``[gave_up, blocked, budget_exhausted, stalled]``; the
    kinds left out are still REPORTED in the tick output, just not pushed.
    """
    if value is None:
        return frozenset(ALL_ESCALATION_KINDS)
    if isinstance(value, str):
        value = [value]
    kinds = {str(v).strip().lower() for v in value if str(v).strip()}
    return frozenset(kinds) if kinds else frozenset(ALL_ESCALATION_KINDS)


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"false", "no", "off", "0", ""}


def _card_status(conn: sqlite3.Connection, card_id: Any) -> str:
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (card_id,)
    ).fetchone()
    return (row["status"] if row else "") or ""


def _close_resolved(
    conn: sqlite3.Connection,
    page_log: PageLog,
    open_now: set,
    *,
    board: str,
    config: Mapping[str, Any],
    sender: Optional[escalation.Sender],
    record: bool,
) -> List[Dict[str, Any]]:
    """Send one closing line for each page whose card has moved on.

    Without this a page never ends and the reader has to go and look, which is
    its own small tax on trusting the channel.  One line, then the entry is
    dropped so the card can page again cleanly if it gets stuck later.

    Two shapes of "moved on":

    * a state-derived page (``stranded`` / ``stalled``) is resolved as soon as
      the card is no longer in that condition this tick;
    * an event-derived page is resolved when the card's status has both
      CHANGED since the page and become one a human would call moving.  The
      change requirement matters: ``budget_exhausted`` fires on a card that is
      already ``ready``, and without it every such page would close itself one
      tick later for no reason.
    """
    resolutions: List[Dict[str, Any]] = []
    for entry in page_log.open_entries():
        card_id, kind = entry.get("card_id"), entry.get("kind")
        status = _card_status(conn, card_id)
        if kind in _NO_RESOLUTION:
            continue
        if kind in _FAULT_KINDS:
            # Keyed to a profile or a ledger, not a card: detection re-runs every
            # tick, so absent from this tick means the condition has cleared.
            if (card_id, kind) in open_now:
                continue
        elif kind in ("stranded", "stalled", "agent_review_lane"):
            if (card_id, kind) in open_now:
                continue
            if not status:
                continue  # card vanished; leave the entry rather than guess
        else:
            if status == entry.get("status_at_page") or status not in _MOVING_STATUSES:
                continue
        esc = escalation.Escalation(
            card_id=card_id,
            title=entry.get("title") or "",
            board=board,
            kind="resolved",
            facts={"was": kind, "now": status or "(gone)"},
        )
        result = escalation.escalate(esc, config, sender=sender)
        result["resolved_kind"] = kind
        resolutions.append(result)
        if result["delivered"] and record:
            page_log.close(card_id, kind)
    return resolutions


def _duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


# -- CLI --------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kanban-escalator",
        description="Reconcile the kanban failure breaker and escalate stuck cards.",
    )
    parser.add_argument("--db", required=True, help="path to the board's kanban.db")
    parser.add_argument("--board", default="default", help="board slug (message + log paths)")
    parser.add_argument("--config", default=os.environ.get("HERMES_RESILIENCE_CONFIG"),
                        help="resilience.yaml (escalation channel + counting policy)")
    parser.add_argument("--cursor", default=None, help="event cursor file")
    parser.add_argument("--state", default=None,
                        help="page-log file: what has already been said to a human, "
                             "so a stuck card pages once instead of every tick "
                             f"(default <db dir>/{DEFAULT_PAGE_LOG_NAME})")
    parser.add_argument("--repeat-after", type=int, default=DEFAULT_REPEAT_AFTER,
                        help="seconds before the same card in the same condition may "
                             f"page again (default {DEFAULT_REPEAT_AFTER}); a state "
                             "change re-pages immediately regardless")
    parser.add_argument("--stranded-after", type=int, default=DEFAULT_STRANDED_AFTER,
                        help=f"seconds before a ready card counts as stranded (default {DEFAULT_STRANDED_AFTER})")
    parser.add_argument("--stalled-after", type=int, default=DEFAULT_STALLED_AFTER,
                        help="seconds a running card may make no progress (no task event "
                             f"other than heartbeat) before it counts as stalled (default {DEFAULT_STALLED_AFTER})")
    parser.add_argument("--max-in-progress", type=int, default=None,
                        help="kanban.max_in_progress, so a card queued behind capacity is "
                             "not mistaken for a stranded one (read from --config if unset)")
    parser.add_argument("--board-file", default=None,
                        help="config/board.yaml: turns ON the board's own moves (auto_advance "
                             "out of waiting, clock-driven archive of done cards). OFF unless "
                             "given -- this job runs --apply against live boards, and a rule "
                             "that switched itself on at the next pull would move real cards")
    parser.add_argument("--apply", action="store_true",
                        help="actually write to the board (default: report only)")
    parser.add_argument("--notify", action="store_true",
                        help="send messages during a dry run too (--apply implies it)")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    readonly = {
        b.strip() for b in
        os.environ.get("HERMES_ESCALATOR_READONLY_BOARDS", "").split(",")
        if b.strip()
    }
    if args.apply and args.board in readonly:
        print(
            f"refusing --apply: board {args.board!r} is listed in "
            "HERMES_ESCALATOR_READONLY_BOARDS",
            file=sys.stderr,
        )
        return 2

    board_spec = None
    if args.board_file:
        import board as board_mod  # local: only needed when board rules are on
        try:
            board_spec = board_mod.load_board(args.board_file)
        except board_mod.BoardError as exc:
            print(f"refusing to start: {exc}", file=sys.stderr)
            return 2
        if board_spec.errors:
            for issue in board_spec.errors:
                print(f"refusing to start: {issue}", file=sys.stderr)
            return 2

    report = run_once(
        args.db,
        board_spec=board_spec,
        board=args.board,
        config=load_config(args.config),
        apply=args.apply,
        cursor_path=args.cursor,
        notify=args.apply or args.notify,
        stranded_after=args.stranded_after,
        stalled_after=args.stalled_after,
        max_in_progress=args.max_in_progress,
        repeat_after=args.repeat_after,
        state_path=args.state,
    )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        recovered = [r for r in report["reconciled"] if r["recovered"]]
        sent = [e for e in report["escalations"] if e["delivered"]]
        failed = [e for e in report["escalations"] if not e["delivered"]]
        print(
            f"board={report['board']} apply={report['apply']} "
            f"recovered={len(recovered)} escalated={len(sent)} "
            f"undelivered={len(failed)} suppressed={len(report['suppressed'])} "
            f"resolved={len(report['resolutions'])}"
        )
        for r in recovered:
            print(f"  recovered {r['card_id']} ({r['trigger_outcome']} does not count)")
        for e in sent:
            print(f"  told a human about {e['card_id']} ({e['kind']}) via {e['channel']}")
        for s in report["suppressed"]:
            print(f"  not paging {s['card_id']}: {s['suppressed_because']}")
        for r in report["resolutions"]:
            print(f"  cleared {r['card_id']} ({r['resolved_kind']}) -- closing line sent")
        verb = "" if report["apply"] else "would "
        for a in report["advanced"]:
            what = "explained" if a["backfilled"] else f"{verb}move {a['from']} -> {a['to']}"
            print(f"  auto_advance {a['card_id']}: {what} -- {a['comment']}")
        for a in report["archived"]:
            print(f"  {verb}archive {a['card_id']} (done for {a['done_for']})")
        for issue in report["board_issues"]:
            print(f"  board.yaml {issue}")
        for e in failed:
            print(f"  UNDELIVERED {e['card_id']} ({e['kind']}): {e['error']}", file=sys.stderr)
        if failed:
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
