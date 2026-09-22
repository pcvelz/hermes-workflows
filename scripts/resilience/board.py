# @user-gated
#!/usr/bin/env python3
"""The user's board, as data: ``config/board.yaml``.

The kanban database validates every write against nine fixed status values
(``VALID_STATUSES`` in ``hermes_cli/kanban_db.py``).  The user's columns are
therefore a **labelling and policy layer over those statuses**, never a schema
change.  This module is that layer: it loads ``board.yaml``, maps each column
to the status it lives on, answers "is this move on the board?", and reports
where the specification contradicts itself.

It answers **what moves exist**.  *Who* may make them is the harness's
question (``plugins/kanban-harness``); a move is legal only when both agree.
See docs/board-design.md for the reasoning.

Stdlib only (PyYAML when present) -- the reconciler imports this from launchd.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wait_budget import parse_duration  # noqa: E402

__all__ = [
    "KANBAN_STATUSES",
    "DEFAULT_COLUMN_STATUS",
    "Issue",
    "Board",
    "BoardError",
    "board_path",
    "load_board",
]

#: Mirror of ``hermes_cli.kanban_db.VALID_STATUSES``.  Duplicated so this
#: module runs without the agent; a test asserts the two never drift.
KANBAN_STATUSES = frozenset({
    "triage", "todo", "scheduled", "ready", "running",
    "blocked", "review", "done", "archived",
})

#: Where each of the user's columns lives.  A column may override this with a
#: ``status:`` key in board.yaml -- recommended, because the mapping is the
#: single most important fact about the board and it should be written down
#: next to the column rather than kept in people's heads (docs/board-design.md
#: D3).
DEFAULT_COLUMN_STATUS: Dict[str, str] = {
    "refinement": "triage",
    "waiting": "todo",        # upstream recompute_ready promotes todo -> ready
    "todo": "ready",          # the user's "to do" -- the ONLY column the dispatcher claims from
    "to_do": "ready",
    "in_progress": "running",
    "question": "blocked",
    # Finished work waiting for the person. Deliberately NOT the runtime's
    # `review` status: that one is an AGENT review lane -- the dispatcher
    # claims a `review` card and spawns its assignee as a reviewer that may
    # merge and set done. `scheduled` is documented upstream as "intentionally
    # not dispatchable": no lane, no timer, and `complete_task` refuses it, so
    # the only door from here to done is our own `accept`. The column name
    # separates the two for people; this status separates them for the
    # runtime. See docs/board-design.md D9.
    "user_review": "scheduled",
    "review": "review",
    "done": "done",
    "archived": "archived",
}

#: Actors that are machines, not roles: they never appear under ``roles:``.
MACHINE_ACTORS = frozenset({"dispatcher", "reconciler"})

#: Every key a role may carry. The permission keys (human, may_not, owns,
#: hands_off_to, rework_to, receives) are enforced by the kanban harness against
#: its grant table, strictest wins; the rest describe the role to a person.
ROLE_PERMISSION_KEYS = frozenset({"human", "may_not", "owns", "hands_off_to",
                                  "rework_to", "receives"})
ROLE_KEYS = ROLE_PERMISSION_KEYS | frozenset({
    "reads", "writes_code", "produces", "evidence", "verifies_by",
    "exits_shipped_by_us"})

#: The runtime's agent-review lane. A user column on this status would get an
#: agent spawned for any card assigned to a real profile.
RUNTIME_AGENT_REVIEW_STATUS = "review"

#: The archiving policies this implementation knows how to carry out.
SUPPORTED_CLOCK_ARCHIVES = frozenset({"done_only"})
SUPPORTED_HUMAN_MAY_ARCHIVE = frozenset({"from_any_column_listing_archived"})


class BoardError(ValueError):
    """board.yaml is structurally broken -- it cannot be interpreted at all."""


@dataclass(frozen=True)
class Issue:
    """Something wrong with the specification, reported at load.

    ``error`` means the board cannot be interpreted as written.  ``warning``
    means it can, but the specification contradicts itself or is ambiguous and
    somebody has to decide -- the board still loads, so a spec question never
    takes a running board down.
    """

    severity: str          # "error" | "warning"
    code: str              # stable id, e.g. "D2", "unknown-edge"
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"


@dataclass
class Column:
    name: str
    status: str
    exit_to: List[str] = field(default_factory=list)
    requires: Dict[str, Any] = field(default_factory=dict)
    requires_to_leave: Dict[str, Any] = field(default_factory=dict)
    auto_advance: Dict[str, Any] = field(default_factory=dict)
    enter: List[str] = field(default_factory=list)
    human_only_exit: bool = False


@dataclass
class Board:
    columns: Dict[str, Column]
    archiving: Dict[str, Any]
    roles: Dict[str, Any]
    comments: Dict[str, Any]
    path: Optional[Path] = None
    issues: List[Issue] = field(default_factory=list)
    #: The table: every permitted (from, to, by) move. Anything else is refused.
    moves: List[Dict[str, str]] = field(default_factory=list)
    #: The hand-off form (``handoff:``): ``{"form": path, "fields": [...]}``,
    #: or None when the spec has no such key -- then hand-offs are unchecked.
    handoff: Optional[Any] = None

    # -- lookups ------------------------------------------------------------

    def allows(self, from_column: str, to_column: str, actor: str) -> bool:
        """Is (from, to, actor) a row of the table? Default deny."""
        return any(m["from"] == from_column and m["to"] == to_column and m["by"] == actor
                   for m in self.moves)

    def status_of(self, column: str) -> str:
        return self.columns[column].status

    def column_of(self, status: str) -> Optional[str]:
        """The user's name for a status, or None when no column uses it."""
        for col in self.columns.values():
            if col.status == status:
                return col.name
        return None

    def can_move(self, from_column: str, to_column: str) -> bool:
        """Is ``from -> to`` an edge on this board?"""
        col = self.columns.get(from_column)
        return col is not None and to_column in col.exit_to

    def auto_advance_rules(self) -> List[Column]:
        return [c for c in self.columns.values() if c.auto_advance]

    # -- archiving ----------------------------------------------------------

    @property
    def clock(self) -> Dict[str, Any]:
        clock = self.archiving.get("clock")
        return dict(clock) if isinstance(clock, Mapping) else {}

    @property
    def auto_archive_done_after(self) -> Optional[float]:
        """How long a done card waits before the clock archives it.

        ``archiving.clock.after``; the older flat ``auto_archive_done_after``
        key is still read so an un-migrated board keeps working.
        """
        value = self.clock.get("after", self.archiving.get("auto_archive_done_after"))
        return None if value in (None, "", False) else parse_duration(value)

    def human_may_archive_from(self, column: str) -> bool:
        """``human_may_archive: from_any_column_listing_archived``."""
        archived = self.column_of("archived")
        return bool(archived) and self.can_move(column, archived)

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == "warning"]


# -- loading ----------------------------------------------------------------

def board_path(hermes_home: Optional[str] = None) -> Path:
    """Where the live board specification is.

    ``$HERMES_BOARD_FILE`` pins it.  Otherwise ``$HERMES_HOME/board.yaml`` --
    the spec's own header says live deployments symlink it there, never copy
    it.  Otherwise the repo's ``config/board.yaml``.
    """
    pinned = os.environ.get("HERMES_BOARD_FILE", "").strip()
    if pinned:
        return Path(os.path.expanduser(pinned))
    home = Path(os.path.expanduser(
        hermes_home or os.environ.get("HERMES_HOME") or "~/.hermes"
    ))
    live = home / "board.yaml"
    if live.exists():
        return live
    return Path(__file__).resolve().parents[2] / "config" / "board.yaml"


def load_board(path: Optional[Any] = None,
               profile_role: Optional[Mapping[str, str]] = None) -> Board:
    """Load and validate board.yaml.

    Raises :class:`BoardError` only when the file cannot be interpreted at
    all.  Contradictions and ambiguities are collected in ``board.issues``.
    """
    file_path = Path(path) if path else board_path()
    try:
        text = file_path.read_text()
    except OSError as exc:
        raise BoardError(f"cannot read board specification {file_path}: {exc}") from exc
    doc = _parse(text, file_path)
    board = _build(doc, file_path)
    board.issues = validate(board, profile_role)
    return board


def _parse(text: str, where: Path) -> Mapping[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise BoardError(
            f"{where}: PyYAML is required to read board.yaml (run with the agent venv)"
        ) from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BoardError(f"{where}: not valid YAML: {exc}") from exc
    if not isinstance(doc, Mapping):
        raise BoardError(f"{where}: top level must be a mapping")
    return doc


def _build(doc: Mapping[str, Any], where: Path) -> Board:
    board_doc = doc.get("board")
    if not isinstance(board_doc, Mapping):
        raise BoardError(f"{where}: missing `board:` mapping")
    raw_columns = board_doc.get("columns")
    if not isinstance(raw_columns, Mapping) or not raw_columns:
        raise BoardError(f"{where}: `board.columns` must be a non-empty mapping")

    columns: Dict[str, Column] = {}
    for name, spec in raw_columns.items():
        spec = spec or {}
        if not isinstance(spec, Mapping):
            raise BoardError(f"{where}: column {name!r} must be a mapping")
        status = spec.get("status") or DEFAULT_COLUMN_STATUS.get(str(name))
        if not status:
            raise BoardError(
                f"{where}: column {name!r} has no `status:` and no default mapping; "
                f"add `status: <one of {sorted(KANBAN_STATUSES)}>`"
            )
        columns[str(name)] = Column(
            name=str(name),
            status=str(status),
            exit_to=[str(c) for c in _as_list(spec.get("exit_to"))],
            requires=dict(spec.get("requires") or {}),
            requires_to_leave=dict(spec.get("requires_to_leave") or {}),
            auto_advance=dict(spec.get("auto_advance") or {}),
            enter=[str(r) for r in _as_list(spec.get("enter"))],
            human_only_exit=bool(spec.get("human_only_exit", False)),
        )

    return Board(
        columns=columns,
        archiving=dict(board_doc.get("archiving") or {}),
        roles=dict(doc.get("roles") or {}),
        comments=dict(doc.get("comments") or {}),
        handoff=(dict(doc["handoff"]) if isinstance(doc.get("handoff"), Mapping)
                 else doc.get("handoff")),
        path=where,
        moves=[
            {"from": str(m.get("from")), "to": str(m.get("to")), "by": str(m.get("by")),
             "when": str(m.get("when") or "")}
            for m in _as_list(board_doc.get("moves")) if isinstance(m, Mapping)
        ],
    )


# -- validation -------------------------------------------------------------

def profile_hint(profile_role: Optional[Mapping[str, str]], value: Any, fix: str) -> str:
    """Words for a role token that was given a profile name, or ''.

    ``fix`` is the correction with ``{role}`` in it. Only ever words: the caller
    has already decided, and must decide the same without this."""
    role = (profile_role or {}).get(str(value))
    if not role or role == value:
        return ""
    return f" {str(value)!r} is a profile, not a role; its role is {role!r}. " + \
        fix.format(role=role)


def validate(board: Board, profile_role: Optional[Mapping[str, str]] = None) -> List[Issue]:
    """Every way the specification can be wrong, found at load.

    Structural problems are errors.  The D-numbered findings in
    docs/board-design.md are warnings: the spec owner has to decide them, and
    until they do the board must keep working.
    """
    issues: List[Issue] = []
    cols = board.columns
    names = set(cols)

    # -- structure (errors) ---------------------------------------------------
    seen: Dict[str, str] = {}
    for col in cols.values():
        if col.status not in KANBAN_STATUSES:
            issues.append(Issue(
                "error", "unknown-status",
                f"column {col.name!r} maps to status {col.status!r}, which the "
                f"kanban database will refuse on write; valid: {sorted(KANBAN_STATUSES)}",
            ))
        if col.status in seen:
            issues.append(Issue(
                "error", "shared-status",
                f"columns {seen[col.status]!r} and {col.name!r} both map to status "
                f"{col.status!r}; the board could not tell them apart",
            ))
        seen.setdefault(col.status, col.name)
        for target in col.exit_to:
            if target not in names:
                issues.append(Issue(
                    "error", "unknown-edge",
                    f"{col.name!r}.exit_to names {target!r}, which is not a column",
                ))

    for col in board.auto_advance_rules():
        move_to = col.auto_advance.get("move_to")
        if move_to and move_to not in col.exit_to:
            issues.append(Issue(
                "error", "auto-advance-off-board",
                f"{col.name!r}.auto_advance.move_to is {move_to!r}, but that is not "
                f"in {col.name!r}.exit_to {col.exit_to} -- an automatic move must be "
                "a move a person could also make",
            ))
        for state in _as_list((col.auto_advance.get("when") or {}).get("referenced_card_reaches")):
            if state not in names:
                issues.append(Issue(
                    "error", "auto-advance-unknown-column",
                    f"{col.name!r}.auto_advance waits for {state!r}, which is not a column",
                ))

    # -- D1: requires with no direction ---------------------------------------
    inbound = {c: 0 for c in names}
    for col in cols.values():
        for target in col.exit_to:
            if target in inbound:
                inbound[target] += 1
    for col in cols.values():
        if col.requires and inbound.get(col.name, 0) == 0:
            issues.append(Issue(
                "warning", "D1",
                f"{col.name!r} has `requires` but no column can move INTO it, so the "
                "condition can only mean 'to leave'. `requires` has no direction; "
                "use `requires_to_leave` here. Until then this rule is not enforced.",
            ))

    # -- archiving policy: only what this implementation can actually do -----
    clock = board.clock
    if clock:
        archives = clock.get("archives")
        if archives is not None and archives not in SUPPORTED_CLOCK_ARCHIVES:
            issues.append(Issue(
                "error", "unsupported-clock",
                f"archiving.clock.archives is {archives!r}; this implementation only "
                f"carries out {sorted(SUPPORTED_CLOCK_ARCHIVES)} -- the clock never "
                "archives a card that is not done",
            ))
        if clock.get("after") is not None:
            try:
                parse_duration(clock["after"])
            except ValueError as exc:
                issues.append(Issue("error", "bad-duration", f"archiving.clock.after: {exc}"))
    human = board.archiving.get("human_may_archive")
    if human is not None and human not in SUPPORTED_HUMAN_MAY_ARCHIVE:
        issues.append(Issue(
            "error", "unsupported-human-archive",
            f"archiving.human_may_archive is {human!r}; supported: "
            f"{sorted(SUPPORTED_HUMAN_MAY_ARCHIVE)}",
        ))

    # -- a user column on the runtime's AGENT review lane ----------------------
    for col in cols.values():
        if col.status == RUNTIME_AGENT_REVIEW_STATUS:
            issues.append(Issue(
                "warning", "agent-review-lane",
                f"column {col.name!r} lives on status {col.status!r}, which is the "
                "runtime's agent review lane: the dispatcher claims any such card "
                "assigned to a real profile and spawns that agent as a reviewer that "
                "may merge and set done. For work waiting on a person use a column on "
                "`scheduled` (see docs/board-design.md D9).",
            ))

    # -- D2 (legacy key): never_archive_unless_done vs archived edges ---------
    if board.archiving.get("never_archive_unless_done"):
        archived = board.column_of("archived")
        done = board.column_of("done")
        offenders = [
            c.name for c in cols.values()
            if archived and archived in c.exit_to and c.name != done
        ]
        if offenders:
            issues.append(Issue(
                "warning", "D2",
                f"archiving.never_archive_unless_done is true, but "
                f"{', '.join(repr(o) for o in offenders)} list {archived!r} in "
                "exit_to. One of them has to lose; until decided, only the clock's "
                "own archiving is restricted to done cards.",
            ))

    # -- D3: a column key that is also a different status's name --------------
    for col in cols.values():
        if col.name in KANBAN_STATUSES and col.name != col.status:
            issues.append(Issue(
                "warning", "D3",
                f"column {col.name!r} lives on status {col.status!r}, but "
                f"{col.name!r} is ALSO a status name -- used by column "
                f"{board.column_of(col.name)!r}. Anyone reading this file against "
                "the database will read the opposite. Add an explicit `status:` "
                "to the column, or rename the key.",
            ))

    # -- the table (errors: a move the harness would enforce must be sound) ----
    # The actors are the roles this spec declares, plus the two machines. Read
    # from the file rather than kept here too: two copies of one list disagree
    # silently. A name no role declares can never be granted a move.
    actors = MACHINE_ACTORS | set(board.roles or {})
    seen_moves = set()
    for m in board.moves:
        triple = (m["from"], m["to"], m["by"])
        if m["from"] not in names or m["to"] not in names:
            issues.append(Issue("error", "move-unknown-column",
                                f"move {triple} names a column that does not exist"))
        elif not board.can_move(m["from"], m["to"]):
            issues.append(Issue("error", "move-not-an-edge",
                                f"move {triple}: {m['to']!r} is not in "
                                f"{m['from']!r}.exit_to {cols[m['from']].exit_to}"))
        if m["by"] not in actors:
            issues.append(Issue("error", "move-unknown-actor",
                                f"move {triple}: by must be one of {sorted(actors)}."
                                + profile_hint(profile_role, m["by"],
                                               "In board.yaml, write by: {role}.")))
        if triple in seen_moves:
            issues.append(Issue("error", "move-duplicate", f"move {triple} is listed twice"))
        seen_moves.add(triple)
    if board.moves:
        for col in cols.values():
            for target in col.exit_to:
                if not any(m["from"] == col.name and m["to"] == target for m in board.moves):
                    issues.append(Issue(
                        "error", "edge-without-move",
                        f"{col.name!r} -> {target!r} is in exit_to but no move says who "
                        "may make it; an edge nobody may take is a spec error"))

    # -- role keys: a closed list (errors) -------------------------------------
    # A key nobody reads governs nothing while looking like it does. The list is
    # closed rather than "keys that look like permissions", because deciding what
    # looks like a permission is itself a guess. Permission keys are enforced
    # against the harness's grants; descriptive keys are for the person reading.
    for role_name, role_spec in (board.roles or {}).items():
        if role_spec is None:
            continue
        if not isinstance(role_spec, Mapping):
            issues.append(Issue("error", "role-not-a-mapping",
                                f"role {role_name!r} must be a mapping"))
            continue
        for key in role_spec:
            if key not in ROLE_KEYS:
                issues.append(Issue(
                    "error", "role-unknown-key",
                    f"role {role_name!r} has key {key!r}, which nothing reads, so it "
                    f"would govern nothing. Known keys: {sorted(ROLE_KEYS)}"))

    # -- the hand-off form (errors: the harness refuses hand-offs by it) ------
    if board.handoff is not None:
        h = board.handoff
        fields = h.get("fields") if isinstance(h, Mapping) else None
        if not isinstance(h, Mapping):
            issues.append(Issue("error", "handoff-form",
                                "handoff must be a mapping with `form` and `fields`"))
        elif not isinstance(h.get("form"), str) or not h["form"].strip():
            issues.append(Issue("error", "handoff-form",
                                "handoff.form must be the path of the form, a string"))
        if isinstance(h, Mapping) and (
                not isinstance(fields, list) or not fields
                or any(not isinstance(f, str) or not f.strip() for f in fields)):
            issues.append(Issue("error", "handoff-form",
                                "handoff.fields must be a non-empty list of non-empty "
                                "strings"))

    return issues


_BOOL = re.compile(r"^(true|false)$", re.IGNORECASE)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def main(argv: Optional[List[str]] = None) -> int:
    """``python3 board.py [path]`` -- lint a board specification."""
    args = argv if argv is not None else sys.argv[1:]
    try:
        board = load_board(args[0] if args else None)
    except BoardError as exc:
        print(f"board.yaml: {exc}", file=sys.stderr)
        return 2
    print(f"{board.path}: {len(board.columns)} columns")
    for col in board.columns.values():
        print(f"  {col.name:<12} -> status {col.status:<9} exit_to {col.exit_to}")
    for issue in board.issues:
        print(f"  {issue}")
    return 1 if board.errors else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
