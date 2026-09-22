# @user-gated
#!/usr/bin/env python3
"""The user's hand on the board: accept, rework, reopen.

    python3 board_cli.py init-ledger
    python3 board_cli.py accept  <card-id> [--comment TEXT]
    python3 board_cli.py accept  --children <parent-id> [--comment TEXT]
    python3 board_cli.py rework  <card-id> --comment TEXT [--to PROFILE]
    python3 board_cli.py reopen  <card-id> --reason TEXT

@user-gated -- see docs/harness-contract.md. Only the user changes this file.

The accept trace
----------------
Every accept writes an event (``completed``, ``by: accept``) AND one line in
the board's append-only accept ledger, ``<kanban.db>.accepts.jsonl``.  The
runtime's own ``gc_events`` deletes the events of old done cards, so the event
alone erodes on exactly the cards it protects; the ledger is what survives.  A
MISSING ledger on a board that holds done or archived cards is a fault: accept
and reopen refuse until it is restored, and the escalator pages.  It never
reads as "nothing was ever accepted".

The ledger's name contains ``kanban.db``, so the harness's built-in side-door
pattern already denies it to every agent.

Why these exist
---------------
Finished work waits for the person in the ``user_review`` column, which lives
on the database status ``scheduled`` (docs/board-design.md D9).  The runtime
offers no way out of it that the board wants: ``complete`` refuses the status,
``unblock`` would send the card back without the reason it failed, and
``archive`` would skip ``done`` altogether.  So the two exits are ours.

**accept is the only path to done.**  Not a convenience over a status edit --
the single door.  From user_review the database itself has no other door
(``complete_task`` refuses ``scheduled``), and no agent is granted
``complete`` by the harness.  Every accept is stamped (``completed`` event with
``by: accept``) so the escalator can tell an accepted card from one that
reached done some other way, and page about the latter.

**rework always carries the reason it failed.**  The comment is required, and
the card goes back to the worker who did it, not to the user's own name --
a to-do card assigned to a person with no profile would never be picked up.

Who may run it
--------------
The user's hand, positively identified -- anything else is an agent:
``HERMES_KANBAN_TASK`` unset, ``HERMES_PROFILE`` unset, ``HERMES_HOME`` unset
or exactly ``~/.hermes``, and stdin a real terminal.  The cost is deliberate:
accept, rework and reopen cannot be scripted or piped.  Agents are refused a
second, independent time by the harness, which denies this script as a side
door in every agent shell.

Stdlib only (PyYAML when present, for the board specification).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import board as board_mod  # noqa: E402

__all__ = ["accept", "rework", "reopen", "accept_children", "accept_trace",
           "init_ledger", "ledger_fault", "ledger_path", "db_path_for", "main",
           "AcceptError"]

ACCEPT_BY = "accept"
REWORK_BY = "rework"
REOPEN_BY = "reopen"
COMMENT_AUTHOR = "user"
LEDGER_SUFFIX = ".accepts.jsonl"


class AcceptError(RuntimeError):
    """The move is not allowed.  The message says what to do instead."""


# -- locating things ----------------------------------------------------------

def db_path_for(board: str = "default", hermes_home: Optional[str] = None) -> Path:
    """Mirror of the agent's ``kanban_db_path`` (a test pins the two together).

    ``HERMES_KANBAN_DB`` pins it; the default board is ``<home>/kanban.db``;
    any other board is ``<home>/kanban/boards/<slug>/kanban.db``.
    """
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pinned:
        return Path(os.path.expanduser(pinned))
    home = Path(os.path.expanduser(
        hermes_home or os.environ.get("HERMES_HOME") or "~/.hermes"
    ))
    if board in ("", "default", None):
        return home / "kanban.db"
    return home / "kanban" / "boards" / board / "kanban.db"


def _spec(path: Optional[str] = None):
    """The board specification; the defaults when none is deployed."""
    try:
        return board_mod.load_board(path) if path else board_mod.load_board()
    except board_mod.BoardError:
        return None


def _status(spec, column: str) -> str:
    if spec is not None and column in spec.columns:
        return spec.status_of(column)
    return board_mod.DEFAULT_COLUMN_STATUS[column]


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise AcceptError(f"no board database at {db_path} (pass --db or --board)")
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _require_users_hand(verb: str = "?") -> None:
    """Default deny: unless the caller is positively a person at a terminal,
    it is an agent, and refused."""
    why = []
    if os.environ.get("HERMES_KANBAN_TASK", "").strip():
        why.append("this is a kanban worker (HERMES_KANBAN_TASK is set)")
    if os.environ.get("HERMES_PROFILE", "").strip():
        why.append("this runs under a Hermes profile (HERMES_PROFILE is set)")
    home = os.environ.get("HERMES_HOME", "").strip()
    if home and Path(os.path.expanduser(home)).resolve() != \
            Path(os.path.expanduser("~/.hermes")).resolve():
        why.append(f"HERMES_HOME is a profile home ({home})")
    try:
        tty = sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        tty = False
    if not tty:
        why.append("stdin is not a terminal (a pipe, a script or a tool call)")
    hermes_parent = _hermes_ancestor()
    if hermes_parent:
        why.append(f"it was spawned by a Hermes process ({hermes_parent}); a terminal does "
                   "not change who started it")
    if why:
        msg = ("accept, rework and reopen are the user's hand, and this caller is not "
               f"positively the user: {'; '.join(why)}. This refusal is final. An agent's "
               "only exit is kanban_handoff.")
        _log_refusal(verb, msg)
        raise AcceptError(msg)


#: Entry points of a Hermes process: a worker, the dispatcher/gateway, a chat or
#: orchestrator session. Matched on the executable or script name, not on any
#: path in the arguments.
_HERMES_ENTRYPOINTS = frozenset({"hermes", "hermes-agent", "hermes-acp", "hermes-gateway"})


def _hermes_ancestor(pid: Optional[int] = None) -> Optional[str]:
    """The first ancestor process that is Hermes, or None.

    Raises the bar; it is not identity (docs/harness-contract.md): a process can
    be detached from its parents. It catches the ordinary case -- a worker, or a
    chat session, running this command in a terminal it opened."""
    import subprocess
    pid = os.getppid() if pid is None else pid
    for _ in range(64):                     # bounded: never loop on a strange table
        if pid <= 1:
            return None
        try:
            out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "an unreadable process table"   # cannot tell: refuse
        if not out:
            return None
        ppid, _, command = out.partition(" ")
        tokens = command.split()
        heads = [os.path.basename(t) for t in tokens[:2]]
        # A macOS framework Python re-executes itself as Python.app, so the
        # process table no longer shows the name it was launched by; the stub
        # leaves that name in __PYVENV_LAUNCHER__ in the process's environment.
        heads.append(os.path.basename(_launcher_of(pid)))
        if any(h in _HERMES_ENTRYPOINTS for h in heads) or "hermes_cli" in command:
            return f"pid {pid}: {command[:120]}"
        try:
            pid = int(ppid.strip())
        except ValueError:
            return None
    return None


def _launcher_of(pid: int) -> str:
    """The path a macOS framework Python was launched by, or ''."""
    import subprocess
    try:
        env = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"(?:^|\s)__PYVENV_LAUNCHER__=(\S+)", env)
    return m.group(1) if m else ""


#: The harness's own refusal log: one fixed path, every refusal, every caller.
REFUSAL_LOG = "~/.hermes/logs/kanban-harness.log"


def _log_refusal(verb: str, msg: str) -> None:
    """A refusal that leaves no trace is indistinguishable from no gate."""
    try:
        path = Path(os.path.expanduser(REFUSAL_LOG))
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        profile = os.environ.get("HERMES_PROFILE", "") or "-"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] [BLOCKED] board=- profile={profile} "
                     f"tool=board_cli {verb} {msg}\n")
    except OSError:
        pass


# -- the accept ledger --------------------------------------------------------

def _db_file(conn: sqlite3.Connection) -> Path:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2])
    raise AcceptError("cannot locate this board's database file")


def ledger_path(conn: sqlite3.Connection) -> Path:
    db = _db_file(conn)
    return db.with_name(db.name + LEDGER_SUFFIX)


def _ledger(conn: sqlite3.Connection) -> Dict[str, int]:
    """card id -> accept time, from the ledger. Unparseable lines are a fault."""
    accepted: Dict[str, int] = {}
    with open(ledger_path(conn), encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                accepted[str(entry["card"])] = int(entry["at"])
            except (ValueError, KeyError, TypeError) as exc:
                raise AcceptError(f"accept ledger {ledger_path(conn)} line {n} is "
                                  f"corrupt ({exc}); restore it before continuing")
    return accepted


def ledger_fault(conn: sqlite3.Connection) -> Optional[str]:
    """Why the ledger cannot be trusted, or None.  Missing on a board that holds
    done or archived cards is a fault: a deleted ledger must never read as
    'nothing was ever accepted'."""
    path = ledger_path(conn)
    if path.exists():
        try:
            _ledger(conn)
        except AcceptError as exc:
            return str(exc)
        return None
    terminal = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('done', 'archived')"
    ).fetchone()[0]
    if terminal:
        return (f"the accept ledger {path} is missing on a board with {terminal} "
                "done/archived card(s). Without it an accepted card cannot be told "
                "from an escaped one. Restore it from backup; do not re-create it.")
    return None


def _require_ledger(conn: sqlite3.Connection) -> None:
    fault = ledger_fault(conn)
    if fault:
        raise AcceptError(fault)


def init_ledger(conn: sqlite3.Connection) -> Path:
    """Create an empty ledger for a board that has never had one.

    Refuses when the board already shows accepts (an accept event survives), so
    a deleted ledger cannot be papered over with an empty one."""
    path = ledger_path(conn)
    if path.exists():
        return path
    prior = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = 'completed' "
        "AND payload LIKE '%\"by\": \"accept\"%'"
    ).fetchone()[0]
    if prior:
        raise AcceptError(
            f"this board has {prior} accept event(s) but no ledger: the ledger existed "
            "and is gone. Restore it from backup; an empty one would erase them."
        )
    path.touch()
    return path


def accept_trace(conn: sqlite3.Connection, card_id: str) -> Optional[int]:
    """When the user accepted this card, or None.  Event OR ledger, never the
    comment: the comment is for a person, the trace is the fact."""
    for (payload, created) in conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? "
        "AND kind = 'completed' ORDER BY id DESC", (card_id,)
    ):
        try:
            if json.loads(payload or "{}").get("by") == ACCEPT_BY:
                return int(created)
        except ValueError:
            continue
    if ledger_path(conn).exists():
        return _ledger(conn).get(card_id)
    return None


def _card(conn: sqlite3.Connection, card_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, title, status, assignee FROM tasks WHERE id = ?", (card_id,)
    ).fetchone()
    if row is None:
        raise AcceptError(f"no card {card_id!r} on this board")
    return row


def _require_user_review(row: sqlite3.Row, waiting_status: str, verb: str) -> None:
    if row["status"] != waiting_status:
        raise AcceptError(
            f"{row['id']} is in {row['status']!r}, not in user_review "
            f"({waiting_status!r}). {verb} only takes finished work waiting for "
            "you; a card gets there through QA's hand-off."
        )


# -- accept -------------------------------------------------------------------

def accept(
    conn: sqlite3.Connection,
    card_id: str,
    *,
    comment: Optional[str] = None,
    spec: Any = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """user_review -> done.  The only path to done."""
    now = int(now if now is not None else time.time())
    waiting, done = _status(spec, "user_review"), _status(spec, "done")
    row = _card(conn, card_id)
    _require_user_review(row, waiting, "accept")
    _require_ledger(conn)
    note = (comment or "").strip() or "accepted"
    with conn:
        # Ledger line first, inside the transaction: if it cannot be written the
        # card does not move. If the commit then fails, the ledger over-reports an
        # accept -- the safe direction (reopen refuses; nothing escapes).
        with open(ledger_path(conn), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"card": card_id, "board": _db_file(conn).parent.name,
                                 "at": now, "by": COMMENT_AUTHOR,
                                 "uid": os.getuid()}) + "\n")
        cur = conn.execute(
            "UPDATE tasks SET status = ?, completed_at = ?, result = COALESCE(result, ?), "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status = ?",
            (done, now, note, card_id, waiting),
        )
        if cur.rowcount != 1:
            raise AcceptError(f"{card_id} moved while you were accepting it; nothing changed")
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (card_id, COMMENT_AUTHOR, f"[accept] {note}", now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'completed', ?, ?)",
            (card_id, json.dumps({"by": ACCEPT_BY, "actor": COMMENT_AUTHOR,
                                  "summary": note[:400]}), now),
        )
    return {"card_id": card_id, "title": row["title"], "status": done, "comment": note}


def accept_children(
    conn: sqlite3.Connection,
    parent_id: str,
    *,
    comment: Optional[str] = None,
    spec: Any = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Accept every child of ``parent_id`` that is waiting in user_review.

    This is what keeps "no agent reaches done, not even for a child card"
    affordable: a fan-out of forty checked rows is one command, not forty.
    Children elsewhere on the board are left alone and listed, never forced.
    """
    _card(conn, parent_id)
    waiting = _status(spec, "user_review")
    children = conn.execute(
        "SELECT t.id, t.status FROM tasks t JOIN task_links l ON l.child_id = t.id "
        "WHERE l.parent_id = ? ORDER BY t.id",
        (parent_id,),
    ).fetchall()
    accepted: List[str] = []
    skipped: List[Dict[str, str]] = []
    for child in children:
        if child["status"] == waiting:
            accept(conn, child["id"], comment=comment, spec=spec, now=now)
            accepted.append(child["id"])
        else:
            skipped.append({"card_id": child["id"], "status": child["status"]})
    return {"parent": parent_id, "accepted": accepted, "skipped": skipped}


# -- rework -------------------------------------------------------------------

def rework(
    conn: sqlite3.Connection,
    card_id: str,
    *,
    comment: str,
    to: Optional[str] = None,
    spec: Any = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """user_review -> to do, carrying the reason it failed.

    The card goes back to whoever did the work (the last coding hand-off), so
    the dispatcher picks it up again.  ``to`` overrides.  If the card still
    waits on unfinished parents it goes to waiting instead of to do -- the
    same rule upstream ``unblock`` applies.
    """
    reason = (comment or "").strip()
    if not reason:
        raise AcceptError(
            "rework needs --comment: the reason it failed, and what the worker must "
            "produce to satisfy the criterion. A card sent back without a reason "
            "comes back with the same mistake."
        )
    now = int(now if now is not None else time.time())
    waiting = _status(spec, "user_review")
    row = _card(conn, card_id)
    _require_user_review(row, waiting, "rework")
    worker = to or _last_worker(conn, card_id)
    if not worker:
        raise AcceptError(
            f"cannot tell who did the work on {card_id}; pass --to <profile>"
        )
    undone = conn.execute(
        "SELECT 1 FROM tasks t JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ? AND t.status NOT IN ('done', 'archived') LIMIT 1",
        (card_id,),
    ).fetchone()
    target = _status(spec, "waiting") if undone else _status(spec, "to_do")
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET status = ?, assignee = ?, current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status = ?",
            (target, worker, card_id, waiting),
        )
        if cur.rowcount != 1:
            raise AcceptError(f"{card_id} moved while you were reworking it; nothing changed")
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (card_id, COMMENT_AUTHOR, f"[rework] {reason}", now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'unblocked', ?, ?)",
            (card_id, json.dumps({"by": REWORK_BY, "status": target,
                                  "assignee": worker, "reason": reason[:400]}), now),
        )
    return {"card_id": card_id, "status": target, "assignee": worker, "comment": reason}


# -- reopen -------------------------------------------------------------------

def reopen(
    conn: sqlite3.Connection,
    card_id: str,
    *,
    reason: str,
    spec: Any = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """done or archived -> user_review, for a card that got there WITHOUT an
    accept.  The repair for a harness escape: same card, same history, back in
    front of the user with the reason on it.

    Refused for a card the user accepted -- the ledger or event says so.
    Idempotent: a card this path already reopened, still waiting, is a no-op.
    """
    reason = (reason or "").strip()
    if not reason:
        raise AcceptError("reopen needs --reason: why this card is going back to you. "
                          "The board must carry its own explanation.")
    now = int(now if now is not None else time.time())
    waiting = _status(spec, "user_review")
    terminal = {_status(spec, "done"), _status(spec, "archived")}
    row = _card(conn, card_id)
    _require_ledger(conn)

    if row["status"] == waiting:
        last = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (card_id,)).fetchone()
        if last and last[0] == "reopened":
            return {"card_id": card_id, "status": waiting, "noop": True}
    if row["status"] not in terminal:
        raise AcceptError(
            f"{card_id} is in {row['status']!r}. reopen only repairs a card that reached "
            "done or archived without your accept.")
    accepted_at = accept_trace(conn, card_id)
    if accepted_at is not None:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(accepted_at))
        raise AcceptError(
            f"{card_id} was accepted by you at {when}. reopen only repairs a card that "
            "reached done without an accept; an accepted card is not reopened this way.")
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET status = ?, assignee = ?, completed_at = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status = ?",
            (waiting, COMMENT_AUTHOR, card_id, row["status"]),
        )
        if cur.rowcount != 1:
            raise AcceptError(f"{card_id} moved while you were reopening it; nothing changed")
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (card_id, COMMENT_AUTHOR,
             f"[reopen] was {row['status']} without an accept: {reason}", now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'reopened', ?, ?)",
            (card_id, json.dumps({"by": REOPEN_BY, "actor": COMMENT_AUTHOR,
                                  "from": row["status"], "reason": reason[:400]}), now),
        )
    return {"card_id": card_id, "status": waiting, "from": row["status"], "noop": False}


def _last_worker(conn: sqlite3.Connection, card_id: str) -> Optional[str]:
    """Who handed this card to QA -- the worker whose work is being sent back."""
    for (payload,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'handed_off' "
        "ORDER BY id DESC",
        (card_id,),
    ):
        try:
            data = json.loads(payload or "{}")
        except ValueError:
            continue
        if data.get("from_role") == "coding" and data.get("from"):
            return str(data["from"])
    return None


# -- CLI ----------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="board_cli.py",
        description="The user's hand: accept (the only path to done), rework (back to "
                    "to do, with the reason), reopen (repair a card that reached done "
                    "without an accept). Run it yourself, at a terminal.",
    )
    parser.add_argument("--db", default=None, help="the board's kanban.db")
    parser.add_argument("--board", default="default", help="board slug (locates kanban.db)")
    parser.add_argument("--board-file", default=None, help="config/board.yaml (column mapping)")
    sub = parser.add_subparsers(dest="verb", required=True)

    p_accept = sub.add_parser("accept", help="user_review -> done")
    target = p_accept.add_mutually_exclusive_group(required=True)
    target.add_argument("card_id", nargs="?")
    target.add_argument("--children", metavar="PARENT_ID",
                        help="accept every child of PARENT_ID that waits in user_review")
    p_accept.add_argument("--comment", default=None)

    p_rework = sub.add_parser("rework", help="user_review -> to do, with the reason")
    p_rework.add_argument("card_id")
    p_rework.add_argument("--comment", required=True,
                          help="why it failed, and what the worker must produce")
    p_rework.add_argument("--to", default=None, help="profile to send it to (default: who did it)")

    p_reopen = sub.add_parser("reopen", help="done/archived without an accept -> user_review")
    p_reopen.add_argument("card_id")
    p_reopen.add_argument("--reason", required=True, help="why it goes back to you")

    sub.add_parser("init-ledger", help="create the accept ledger for a board that has none")

    args = parser.parse_args(argv)
    try:
        _require_users_hand(args.verb)
        spec = _spec(args.board_file)
        conn = _connect(Path(args.db) if args.db else db_path_for(args.board))
        try:
            if args.verb == "accept" and args.children:
                result = accept_children(conn, args.children, comment=args.comment, spec=spec)
                print(f"accepted {len(result['accepted'])}: {', '.join(result['accepted']) or '-'}")
                for s in result["skipped"]:
                    print(f"  left alone {s['card_id']} ({s['status']}): not waiting for you")
            elif args.verb == "accept":
                result = accept(conn, args.card_id, comment=args.comment, spec=spec)
                print(f"accepted {result['card_id']} -> done")
            elif args.verb == "reopen":
                result = reopen(conn, args.card_id, reason=args.reason, spec=spec)
                print(f"{result['card_id']} already reopened; nothing to do" if result["noop"]
                      else f"reopened {result['card_id']}: {result['from']} -> user_review")
            elif args.verb == "init-ledger":
                print(f"accept ledger: {init_ledger(conn)}")
            else:
                result = rework(conn, args.card_id, comment=args.comment, to=args.to, spec=spec)
                print(f"sent {result['card_id']} back to {result['assignee']} "
                      f"({result['status']}) with the reason")
        finally:
            conn.close()
    except AcceptError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
