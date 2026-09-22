# @user-gated
"""kanban-harness — a workflow harness for the Hermes kanban board.

A regular workflow (skills, SOUL prose, the worker prompt) can only *ask* an agent to
follow the board's process; this plugin makes the forbidden moves impossible for an
agent process. The code holds NO workflow semantics: it interprets a transition table
from ``harness.yaml``. Per board (with a ``"*"`` default), per role (roles map to
profiles), the table lists every move an agent may make:

    transitions:
      - {from_role: coding, action: handoff, to_role: qa}
      - {from_role: qa,     action: complete, when: non_root}
      - {from_role: coding, action: create, via: [tool, shell]}

Anything not listed (and not in ``always_allow``) is refused. An *action* is the
kanban tool name without its ``kanban_`` prefix (``complete``, ``block``, ``create`` …),
the ``hermes kanban <verb>`` it corresponds to when ``via`` includes ``shell``, or
``handoff`` (the plugin's own tool; target role and resulting status come from the row).

Pieces:

* ``pre_tool_call`` — refuses kanban_* tool calls and ``hermes kanban <verb>`` shell
  commands the table does not grant, plus configured side-door patterns (the kanban DB
  file, the kanban_db module, the dashboard API) in shell / code / file tool arguments.
  The harness always enforces: there is no log-only mode and no switch to disable it.
* ``kanban_handoff`` — a worker tool performing a ``handoff`` row: ends the worker's run,
  clears the claim, reassigns to the row's target role and sets the row's status, in
  one write transaction. The dispatcher then spawns the target (never a human lane,
  whose assignee is not a Hermes profile).
* Runnable self-test: ``python3 plugins/kanban-harness/__init__.py``.

Config: ``KANBAN_HARNESS_FILE`` env; else ``harness.yaml`` beside this file (git-ignored);
else the committed ``harness.yaml.example``.

FAIL-CLOSED for kanban mutations (unlike the other governance plugins): an unreadable
config or a failing check refuses the kanban call; unrelated tools are never affected.

Runs inside agent processes only. The human's own ``hermes kanban`` CLI and dashboard
never load it, which is what keeps the human as the only actor for moves no agent row
grants (e.g. done). Pattern refusal on a shell is not a sandbox: see
docs/kanban-harness.md for the residual risk and the hard boundary.
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

_TOOL_PREFIX = "kanban_"
_HANDOFF = "handoff"
# Where a hand-off may land.  `scheduled` is user_review: finished work waiting
# for the person -- deliberately not the runtime's `review`, an AGENT review
# lane the dispatcher spawns reviewers for (docs/board-design.md D9).  `done`,
# `archived` and `review` are absent on purpose: a hand-off never finishes a card.
_HANDOFF_STATUSES = ("ready", "blocked", "scheduled")
_HANDOFF_TOOL = _TOOL_PREFIX + _HANDOFF

# Mechanics, not policy: which tool arguments carry shell/code text or a file path.
_DEFAULT_TEXT_KEYS = ["command", "code", "script", "cmd", "input", "keys"]
_DEFAULT_PATH_KEYS = ["path", "file_path"]
_FLAGS_WITH_VALUE = {"--board", "-b", "--profile", "-p", "--tenant"}
_DEFAULT_ROOT_MARKER = r"(?mi)^\s*Level:\s*0\s*$"
_WHEN = ("any", "root", "non_root")

# ---------------------------------------------------------------------------
# THE BASELINE. It lives in code; configuration can only make it stricter.
# ---------------------------------------------------------------------------

#: Actions no table may ever grant an agent: each lands a card in done or archived.
_NEVER_GRANTED = frozenset({"complete", "archive"})

#: The only moves `always_allow` may hold: reading the board and talking on it.
_READ_ONLY_TOOLS = frozenset({"kanban_show", "kanban_list", "kanban_heartbeat",
                              "kanban_comment"})
_READ_ONLY_VERBS = frozenset({"list", "ls", "show", "tail", "watch", "stats",
                              "diagnostics", "context", "runs", "log", "help",
                              "boards list", "boards ls"})

#: Side doors refused in every agent shell and code tool, whatever a config says.
_BASELINE_DENY = [
    (re.compile(r"kanban\.db"), "the kanban database file (and its accept ledger)"),
    (re.compile(r"\bkanban_db\b"), "the kanban_db module"),
    (re.compile(r"/api/plugins/kanban\b"), "the dashboard kanban API"),
    (re.compile(r"\bboard_cli\b"), "the user's accept/rework/reopen command"),
]
_BASELINE_DENY_PATHS = [re.compile(r"kanban\.db"), re.compile(r"/kanban/boards(/|$)")]
_VIA = ("tool", "shell")

_CACHE = {"sig": None, "cfg": None, "err": None}


class HarnessConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _config_path():
    env = os.environ.get("KANBAN_HARNESS_FILE", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    here = Path(__file__).resolve().parent
    real = here / "harness.yaml"
    if real.exists():
        return real
    return here / "harness.yaml.example"


def _mtime(p):
    try:
        return os.stat(p).st_mtime
    except OSError:
        return -1.0


# Keys that make up one board matrix. Top-level values are the default; an entry under
# ``boards:`` overrides any of them (each key replaced as a whole).
_MATRIX_KEYS = ("roles", "transitions", "always_allow", "unknown_profile", "root_marker",
                "side_doors")


def _as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _profile_hint(profile_role, value, fix):
    """Words for a role token that was given a profile name, or ''.

    Only ever words, never a decision: the profile is not resolved to its role,
    because one move has one name. Mirrors board.profile_hint; the plugin must
    not need board.py to load."""
    role = (profile_role or {}).get(str(value))
    if not role or role == value:
        return ""
    return f" {str(value)!r} is a profile, not a role; its role is {role!r}. " + \
        fix.format(role=role)


def _normalize_matrix(raw, where):
    roles_raw = raw.get("roles") or {}
    if not isinstance(roles_raw, dict) or not roles_raw:
        raise HarnessConfigError(f"{where}: needs a non-empty 'roles' mapping")
    roles, profile_role = {}, {}
    for name, r in roles_raw.items():
        r = r or {}
        human = bool(r.get("human"))
        # A role without `profiles` is a single profile named as its own role.
        profiles = [str(p) for p in _as_list(r.get("profiles"))] or ([] if human else [name])
        roles[name] = {
            "name": name,
            "human": human,
            "profiles": profiles,
            "assignee": str(r.get("assignee") or (profiles[0] if profiles else name)),
        }
        for p in profiles:
            if p in profile_role:
                raise HarnessConfigError(f"{where}: profile {p!r} is in two roles")
            profile_role[p] = name

    transitions = []
    for i, t in enumerate(_as_list(raw.get("transitions"))):
        w = f"{where}: transitions[{i}]"
        if not isinstance(t, dict) or not t.get("from_role") or not t.get("action"):
            raise HarnessConfigError(f"{w}: needs from_role and action")
        if t["from_role"] not in roles:
            raise HarnessConfigError(
                f"{w}: unknown from_role {t['from_role']!r}."
                + _profile_hint(profile_role, t["from_role"],
                                "In harness.yaml, write from_role: {role}."))
        action = str(t["action"])
        when = str(t.get("when", "any"))
        if when not in _WHEN:
            raise HarnessConfigError(f"{w}: when must be one of {_WHEN}")
        via = [str(v) for v in _as_list(t.get("via") or "tool")]
        if any(v not in _VIA for v in via):
            raise HarnessConfigError(f"{w}: via must be from {_VIA}")
        req = t.get("requires")
        if req is not None:
            if not isinstance(req, dict) or not req.get("files"):
                raise HarnessConfigError(f"{w}: requires needs files: <glob>")
            req = {"files": str(req["files"]),
                   "matches": re.compile(req["matches"]) if req.get("matches") else None}
        row = {"from_role": t["from_role"], "action": action, "when": when, "via": via,
               "to_role": t.get("to_role"), "status": t.get("status"), "requires": req}
        if action == _HANDOFF:
            if row["to_role"] not in roles:
                raise HarnessConfigError(
                    f"{w}: handoff needs a known to_role."
                    + _profile_hint(profile_role, row["to_role"],
                                    "In harness.yaml, write to_role: {role}."))
            if row["status"] not in _HANDOFF_STATUSES:
                raise HarnessConfigError(
                    f"{w}: handoff needs status: {' | '.join(_HANDOFF_STATUSES)} "
                    f"(never done, archived or the runtime's agent `review` lane: a "
                    f"hand-off never finishes a card)")
            if row["status"] == "scheduled" and not roles[row["to_role"]]["human"]:
                raise HarnessConfigError(
                    f"{w}: status scheduled is user_review -- finished work waiting for a "
                    f"PERSON. Handing an agent role into it parks the card where nothing "
                    f"is ever spawned for it.")
            if row["status"] == "ready" and not roles[row["to_role"]]["human"] \
                    and not roles[row["to_role"]]["profiles"]:
                raise HarnessConfigError(f"{w}: to_role has no profile to spawn")
            if "shell" in via:
                raise HarnessConfigError(f"{w}: handoff has no shell form")
        if action in _NEVER_GRANTED:
            raise HarnessConfigError(
                f"{w}: action {action!r} lands a card in done or archived. No agent is "
                f"ever granted that, whatever a table says; done is reachable only "
                f"through the user's accept.")
        transitions.append(row)

    aa = raw.get("always_allow") or {}
    tools = set(str(t) for t in _as_list(aa.get("tools")))
    verbs = set(str(v) for v in _as_list(aa.get("shell_verbs")))
    if tools - _READ_ONLY_TOOLS or verbs - _READ_ONLY_VERBS:
        raise HarnessConfigError(
            f"{where}: always_allow may hold read-only moves only; these can write: "
            f"{sorted((tools - _READ_ONLY_TOOLS) | (verbs - _READ_ONLY_VERBS))}")
    sd = raw.get("side_doors") or {}
    if "enabled" in sd:
        raise HarnessConfigError(
            f"{where}: side_doors.enabled was removed. The side-door checks are always "
            "on; configuration can only add patterns.")
    if "unknown_profile" in raw and str(raw["unknown_profile"]).lower() != "deny":
        raise HarnessConfigError(
            f"{where}: unknown_profile: {raw['unknown_profile']!r} is not allowed. A "
            "profile in no role is always denied.")
    # The baseline lives in code; configuration only adds to it.
    return {
        "roles": roles,
        "profile_role": profile_role,
        "transitions": transitions,
        "always_tools": tools,
        "always_verbs": verbs,
        "unknown_profile": "deny",
        "root_marker": re.compile(raw.get("root_marker") or _DEFAULT_ROOT_MARKER),
        "side_doors": True,
        "deny_patterns": _BASELINE_DENY + [
            (re.compile(p["pattern"]), p.get("label") or p["pattern"])
            for p in _as_list(sd.get("deny_patterns"))],
        "path_patterns": _BASELINE_DENY_PATHS + [
            re.compile(p) for p in _as_list(sd.get("deny_path_patterns"))],
        "text_keys": list(dict.fromkeys(list(_DEFAULT_TEXT_KEYS)
                                        + _as_list(sd.get("text_arg_keys")))),
        "path_keys": list(dict.fromkeys(list(_DEFAULT_PATH_KEYS)
                                        + _as_list(sd.get("path_arg_keys")))),
    }


def _normalize(doc):
    """Validate the raw document and return a normalized config dict."""
    if not isinstance(doc, dict):
        raise HarnessConfigError("config root must be a mapping")
    if "mode" in doc:
        raise HarnessConfigError(
            "mode: was removed. warn (log-only) mode no longer exists; the harness "
            "always enforces -- there is no switch to make it merely observe.")
    if "enabled" in doc:
        raise HarnessConfigError(
            "enabled: was removed. There is no switch to turn the harness off; it "
            "always enforces.")
    base = {k: doc[k] for k in _MATRIX_KEYS if k in doc}
    boards = doc.get("boards")
    if boards is None:
        boards = ["*"]
    if isinstance(boards, list):
        boards = {str(b): {} for b in boards}
    if not isinstance(boards, dict):
        raise HarnessConfigError("boards must be a list of slugs or a mapping")
    # EVERY board is harnessed. `boards:` only selects per-board overrides; a
    # board it does not list gets the default table, never no table at all.
    boards.setdefault("*", {})
    matrices = {}
    for slug, override in boards.items():
        merged = dict(base)
        merged.update(override or {})
        matrices[str(slug)] = _normalize_matrix(merged, f"boards.{slug}")
    log_file = doc.get("log_file")
    return {
        "log_file": os.path.expanduser(str(log_file)) if log_file else None,
        "matrices": {k: v for k, v in matrices.items() if k != "*"},
        "default": matrices["*"],
    }


#: What the harness enforces when its own config cannot be read: no role, no
#: grant, read-only moves, every built-in side door. A broken config never opens
#: a door -- not for kanban tools and not for shell or code tools either.
_LOCKDOWN = {
    "roles": {}, "profile_role": {}, "transitions": [],
    "always_tools": set(_READ_ONLY_TOOLS), "always_verbs": set(_READ_ONLY_VERBS),
    "unknown_profile": "deny", "root_marker": re.compile(_DEFAULT_ROOT_MARKER),
    "side_doors": True, "deny_patterns": list(_BASELINE_DENY),
    "path_patterns": list(_BASELINE_DENY_PATHS),
    "text_keys": list(_DEFAULT_TEXT_KEYS), "path_keys": list(_DEFAULT_PATH_KEYS),
}


# ---------------------------------------------------------------------------
# The board specification (config/board.yaml)
# ---------------------------------------------------------------------------
#
# Two files, two questions.  board.yaml says WHAT MOVES EXIST on the user's
# board; this plugin's harness.yaml says WHO MAY MAKE THEM.  A move is legal
# only when both agree.  They are composed here at load -- never generated
# into one another, because a generated file silently diverges the first time
# somebody hand-edits it.  See docs/board-design.md.
#
# Composition is OPT-IN: it happens only when a board specification is
# deployed -- KANBAN_BOARD_FILE, or $HERMES_HOME/board.yaml (the spec's own
# header says live deployments symlink it there).  The repo's config/board.yaml is NOT picked up by fallback: a
# harness that silently changed its rules because a file appeared in a
# checkout would be worse than one that ignores the file.

def _board_module():
    """Import scripts/resilience/board.py from the repo this plugin lives in."""
    lib = Path(__file__).resolve().parents[2] / "scripts" / "resilience"
    if not (lib / "board.py").exists():
        return None
    if str(lib) not in sys.path:
        sys.path.insert(0, str(lib))
    import board  # noqa: E402  (stdlib + PyYAML only)
    return board


def _board_file(_doc=None):
    # Deliberately NOT a key inside harness.yaml: the load cache is keyed on
    # file mtimes it can see BEFORE parsing, and a path named inside the file
    # would make edits to the board silently not take effect.
    env = os.environ.get("KANBAN_BOARD_FILE", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        live = Path(os.path.expanduser(home)) / "board.yaml"
        if live.exists():
            return live
    return None


#: The harness actions that land a card in a given column.  `handoff` is
#: handled separately, because where it lands is its row's `status`.
_ACTION_REACHES = {"complete": "done", "archive": "archived", "block": "blocked"}


def _check_against_board(cfg, spec, where):
    """Fail loudly when the two files disagree.

    board.yaml's role semantics (`may_not`, `human`) are ASSERTIONS about
    harness.yaml's grant table.  Stating the same fact twice is how two files
    drift; checking one against the other turns the duplication into a guard.
    """
    matrices = [m for m in [cfg.get("default")] + list(cfg["matrices"].values()) if m]
    used_statuses = {c.status for c in spec.columns.values()}
    declared = set(spec.roles or {})
    for matrix in matrices:
        for t in matrix["transitions"]:
            # Two files describe the roles; a role this table grants a move must
            # be one the board declares, so nobody can satisfy one file and be
            # refused by the other.
            if t["from_role"] not in declared:
                raise HarnessConfigError(
                    f"{where}: role {t['from_role']!r} is granted {t['action']!r} here but "
                    f"is not declared in the board's roles ({spec.path}); declare it "
                    "there first.")
            if t["action"] == _HANDOFF and t.get("status") and t["status"] not in used_statuses:
                raise HarnessConfigError(
                    f"{where}: a hand-off from {t['from_role']!r} to {t['to_role']!r} lands "
                    f"in status {t['status']!r}, but no column on the board "
                    f"({spec.path}) lives on that status -- the card would vanish from "
                    "every column a person looks at."
                )
        for role_name, role_spec in (spec.roles or {}).items():
            if not isinstance(role_spec, dict):
                continue
            bound = matrix["roles"].get(role_name)
            if bound is None:
                continue  # a role the board describes but no profile runs yet
            if bool(role_spec.get("human")) != bool(bound.get("human")):
                raise HarnessConfigError(
                    f"{where}: role {role_name!r} is human={bool(role_spec.get('human'))} "
                    f"in {spec.path} but human={bool(bound.get('human'))} here. A human "
                    "lane with a profile behind it gets a worker spawned for it."
                )
            forbidden = {}
            for col in _as_list(role_spec.get("may_not")):
                if col in spec.columns:
                    forbidden[spec.status_of(col)] = col
            for t in matrix["transitions"]:
                if t["from_role"] != role_name:
                    continue
                lands = t.get("status") if t["action"] == _HANDOFF else _ACTION_REACHES.get(t["action"])
                if lands in forbidden:
                    raise HarnessConfigError(
                        f"{where}: {spec.path} says role {role_name!r} may not move a card to "
                        f"{forbidden[lands]!r} (may_not), but this file grants it "
                        f"{t['action']!r}, which lands there. One of the two files is wrong; "
                        "they must agree."
                    )
        _check_board_permissions(matrix, spec, where)


def _statuses(spec, columns):
    """Statuses of the named columns; a string may list several ("a, b")."""
    names = [n.strip() for c in _as_list(columns) for n in str(c).split(",")]
    unknown = [n for n in names if n and n not in spec.columns]
    if unknown:
        # A permission naming a column that does not exist permits nothing
        # it seems to: refuse to guess what was meant.
        raise HarnessConfigError(f"{spec.path}: role keys name unknown columns {unknown}")
    return {spec.status_of(n) for n in names if n}


def _check_board_permissions(matrix, spec, where):
    """The rest of board.yaml's permission keys, strictest wins: a grant here
    that the board's roles do not allow fails the load.

    - `owns`: only the owning role may land a card in those columns.
    - `hands_off_to` / `rework_to` / `receives`: a hand-off from R to T landing
      on status S needs T in R.hands_off_to, or S a column in R.rework_to, or T
      human and S a column in T.receives. A role with none of these may not
      hand off at all.
    """
    roles = {n: (s if isinstance(s, dict) else {}) for n, s in (spec.roles or {}).items()}
    owned = {}
    for name, rs in roles.items():
        for status in _statuses(spec, rs.get("owns")):
            owned[status] = name
    for t in matrix["transitions"]:
        lands = t.get("status") if t["action"] == _HANDOFF else _ACTION_REACHES.get(t["action"])
        owner = owned.get(lands)
        if owner is not None and owner != t["from_role"]:
            raise HarnessConfigError(
                f"{where}: {spec.path} says role {owner!r} owns "
                f"{spec.column_of(lands)!r} (owns), but this file grants role "
                f"{t['from_role']!r} {t['action']!r}, which lands there. The stricter "
                "file wins: remove the grant or the ownership.")
        if t["action"] != _HANDOFF:
            continue
        frm = roles.get(t["from_role"], {})
        to = roles.get(t.get("to_role"), {})
        if (t.get("to_role") in _as_list(frm.get("hands_off_to"))
                or lands in _statuses(spec, frm.get("rework_to"))
                or (to.get("human") and lands in _statuses(spec, to.get("receives")))):
            continue
        raise HarnessConfigError(
            f"{where}: this file grants {t['from_role']!r} a hand-off to "
            f"{t.get('to_role')!r} landing in {spec.column_of(lands)!r}, but {spec.path} "
            f"allows it by none of hands_off_to, rework_to or receives. The stricter "
            "file wins: remove the grant or declare the hand-off on the board.")


def _compose_board(cfg, doc, where):
    path = _board_file(doc)
    if path is None:
        cfg["board"] = None
        return
    mod = _board_module()
    if mod is None:
        raise HarnessConfigError(
            f"{where}: a board specification is deployed ({path}) but the board "
            "loader (scripts/resilience/board.py) is not next to this plugin"
        )
    try:
        default = cfg.get("default") or {}
        spec = mod.load_board(path, profile_role=default.get("profile_role"))
    except mod.BoardError as exc:
        raise HarnessConfigError(str(exc))
    if spec.errors:
        raise HarnessConfigError("; ".join(str(e) for e in spec.errors))
    _check_against_board(cfg, spec, where)
    cfg["board"] = spec


def _hermes_config_path():
    home = os.environ.get("HERMES_HOME", "").strip() or "~/.hermes"
    return Path(os.path.expanduser(home)) / "config.yaml"


def _check_auto_decompose(path):
    """The dispatcher tick can split a card into children with no tool call for
    the harness to see. Upstream defaults `kanban.auto_decompose` to true, so
    only an explicit false is safe: true, absent, or no config at all refuses."""
    if not path.exists():
        raise HarnessConfigError(
            f"{path} does not exist, so kanban.auto_decompose takes upstream's default "
            "(true): the dispatcher would split cards outside any tool call. Set "
            "kanban.auto_decompose: false there.")
    import yaml
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    kanban = doc.get("kanban") if isinstance(doc, dict) else None
    value = kanban.get("auto_decompose", "absent") if isinstance(kanban, dict) else "absent"
    if value is not False:
        raise HarnessConfigError(
            f"{path}: kanban.auto_decompose is {value!r}; it must be false in so many "
            "words. Upstream defaults it to true, and the dispatcher then splits cards "
            "outside any tool call the harness can see.")


def load_config():
    """Return the normalized config, cached by mtime. Raises HarnessConfigError."""
    path = _config_path()
    board_file = _board_file(None)
    hermes_cfg = _hermes_config_path()
    sig = (str(path), _mtime(path), str(board_file), _mtime(board_file) if board_file else None,
           str(hermes_cfg), _mtime(hermes_cfg))
    if sig == _CACHE["sig"]:
        if _CACHE["err"]:
            raise HarnessConfigError(_CACHE["err"])
        return _CACHE["cfg"]
    _CACHE["sig"] = sig
    try:
        text = path.read_text(encoding="utf-8")
        try:
            import yaml
            doc = yaml.safe_load(text)
        except ImportError:
            doc = json.loads(text)
        cfg = _normalize(doc)
        _check_auto_decompose(hermes_cfg)
        _compose_board(cfg, doc, str(path))
    except Exception as exc:
        _CACHE["cfg"], _CACHE["err"] = None, f"{path}: {exc}"
        raise HarnessConfigError(_CACHE["err"])
    _CACHE["cfg"], _CACHE["err"] = cfg, None
    return cfg


def matrix_for(cfg, board):
    """The matrix governing ``board``; None when the board is not harnessed.

    An unknown board (None) is treated as harnessed (fail-closed): the default matrix,
    else any configured one.
    """
    if board is not None and board in cfg["matrices"]:
        return cfg["matrices"][board]
    return cfg["default"]   # every board is harnessed; there is always a default


# ---------------------------------------------------------------------------
# Identity and task facts
# ---------------------------------------------------------------------------

def _current_profile():
    p = os.environ.get("HERMES_PROFILE", "").strip()
    if p:
        return p
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name()
    except Exception:
        return None


def _call_board(args):
    b = (args or {}).get("board")
    if isinstance(b, str) and b.strip():
        return b.strip()
    b = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if b:
        return b
    try:
        from hermes_cli.kanban_db import get_current_board
        return get_current_board()
    except Exception:
        return None  # unknown board -> treated as harnessed (fail-closed)


def role_for(matrix, profile):
    name = matrix["profile_role"].get(profile or "")
    return matrix["roles"].get(name) if name else None


def is_root_task(matrix, body, parents):
    """Level 0 = no parent task, or the body carries the root marker."""
    if body and matrix["root_marker"].search(body):
        return True
    return not parents


def _db_task_is_root(matrix, board, task_id):
    from hermes_cli import kanban_db as kb
    conn = kb.connect(board=board)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            return True  # unknown task: treat as root (fail-closed)
        return is_root_task(matrix, task.body, kb.parent_ids(conn, task_id))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Decisions (pure, testable): the code only interprets the table
# ---------------------------------------------------------------------------

def granted(matrix, role, action, via, is_root=None):
    """Rows of the table granting ``role`` this action by this route."""
    if role is None:
        return []
    rows = []
    for t in matrix["transitions"]:
        if t["from_role"] != role["name"] or t["action"] != action or via not in t["via"]:
            continue
        if t["when"] != "any":
            root = True if is_root is None else bool(is_root())
            if (t["when"] == "root") != root:
                continue
        rows.append(t)
    return rows


def _current_workspace():
    return os.environ.get("HERMES_KANBAN_WORKSPACE", "").strip() or None


def evidence_missing(row, workspace):
    """Why ``row``'s ``requires`` precondition fails in ``workspace``, or None when met."""
    req = row.get("requires")
    if not req:
        return None
    if not workspace or not os.path.isdir(workspace):
        return f"no workspace to check for evidence ({req['files']})"
    import glob as _glob
    files = [p for p in _glob.glob(os.path.join(workspace, req["files"]), recursive=True)
             if os.path.isfile(p)]
    if not files:
        return f"no file matching {req['files']!r} in the workspace"
    if req["matches"] is None:
        return None
    for p in files:
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                if req["matches"].search(fh.read(1_000_000)):
                    return None
        except OSError:
            continue
    return (f"no file matching {req['files']!r} contains "
            f"/{req['matches'].pattern}/")


def with_evidence(rows, workspace):
    """Split granting rows into (rows whose evidence is met, reasons for the rest)."""
    ok, reasons = [], []
    for t in rows:
        why = evidence_missing(t, workspace)
        (reasons.append(why) if why else ok.append(t))
    return ok, reasons


def _evidence_refusal(role, profile, what, reasons):
    return (f"[kanban-harness] {what} refused for role {role['name']!r} (profile "
            f"{profile!r}): this move requires evidence, and {'; '.join(reasons)}. "
            f"Produce it, then try again. The tool was NOT run.")


def _refusal(matrix, role, profile, what, extra=""):
    if role is None:
        return (f"[kanban-harness] {what} refused: profile {profile!r} has no role in this "
                f"board's transition matrix. The tool was NOT run.")
    exits = [f"{t['action']}" + (f" -> {t['to_role']}" if t["to_role"] else "")
             + ("" if t["when"] == "any" else f" ({t['when']} tasks)")
             for t in matrix["transitions"] if t["from_role"] == role["name"]]
    hint = (f"Moves this role may make: {', '.join(exits)}." if exits
            else "This role may make no moves.")
    if any(t["action"] == _HANDOFF for t in matrix["transitions"]
           if t["from_role"] == role["name"]):
        hint += " To finish your part, call kanban_handoff(summary=...)."
    return (f"[kanban-harness] {what} refused for role {role['name']!r} (profile "
            f"{profile!r}): not in this board's transition matrix. {hint}"
            f"{extra} The tool was NOT run.")


def check_kanban_tool(matrix, tool_name, profile, is_root=None, workspace=None):
    """Return a refusal string for a kanban_* tool, or None to allow."""
    if tool_name in matrix["always_tools"]:
        return None
    role = role_for(matrix, profile)
    action = tool_name[len(_TOOL_PREFIX):]
    rows = granted(matrix, role, action, "tool", is_root)
    if not rows:
        return _refusal(matrix, role, profile, tool_name)
    ok, reasons = with_evidence(rows, workspace)
    return None if ok else _evidence_refusal(role, profile, tool_name, reasons)


def _segments(text):
    return [s for s in re.split(r"[;&|\n()`]|\$\(", text) if s.strip()]


def _shell_verbs(text):
    """Yield each ``hermes … kanban <verb>`` found in shell text."""
    for seg in _segments(text):
        toks = seg.split()
        for i, tok in enumerate(toks):
            if tok.strip("'\"").rsplit("/", 1)[-1] != "kanban":
                continue
            if not any("hermes" in t for t in toks[:i]):
                continue
            rest = [t.strip("'\"") for t in toks[i + 1:]]
            j = 0
            while j < len(rest) and rest[j].startswith("-"):
                j += 2 if (rest[j] in _FLAGS_WITH_VALUE and "=" not in rest[j]) else 1
            if j >= len(rest):
                continue
            verb = rest[j]
            if verb == "boards":
                sub = next((r for r in rest[j + 1:] if not r.startswith("-")), "list")
                verb = f"boards {sub}"
            yield verb


# ---------------------------------------------------------------------------
# Unresolvable shell. A verb parser only sees what is written literally, so
# `H=hermes; $H kanban complete` walked straight past it (observed 2026-09-22:
# eleven forms moved a scratch card to done). The rule is not a keyword filter:
# a command whose PROGRAM WORD the gate cannot read is refused, whatever it
# might run, because a gate that guesses what a variable holds guesses wrong
# in the unsafe direction. Literal wrappers are resolved, not refused.
# ---------------------------------------------------------------------------

#: Keys whose text is shell. `code` is Python: a `$` in a string is not a
#: program word, and parsing it as shell would refuse ordinary code.
_SHELL_KEYS = ("command", "cmd")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
#: Wrappers that run the next word as the program: (options that take a value).
_WRAPPERS = {
    "env": frozenset({"-u", "-C", "-S"}), "exec": frozenset({"-a"}),
    "nohup": frozenset(), "command": frozenset(), "time": frozenset(),
    "nice": frozenset({"-n"}), "timeout": frozenset({"-s", "-k"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-d", "-s", "-E", "-a"}),
}
#: Words that open or close a compound command; the command follows them.
_KEYWORDS = frozenset({"if", "then", "else", "elif", "fi", "do", "done", "while",
                       "until", "!", "{", "}", "time"})
#: Compound commands whose own words are names and values, never programs.
_SKIP_COMPOUND = frozenset({"for", "case", "select", "esac", ";;"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_UNRESOLVED_HINT = ("write the command literally; the gate cannot read a command it "
                    "cannot resolve")


def _hidden(word):
    return "$" in word or "`" in word


def _substitutions(text):
    """The bodies of every `$(...)` and backtick substitution in raw shell text."""
    out, i = [], 0
    while i < len(text):
        if text.startswith("$((", i):          # arithmetic, not a command
            i += 3
            continue
        if text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            out.append(text[i + 2:j - 1])
            i = j
        elif text[i] == "`":
            j = text.find("`", i + 1)
            if j < 0:
                out.append(text[i + 1:])
                break
            out.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return out


def _split_heredocs(text):
    """Return (text without heredoc bodies, [(line that opened it, body)])."""
    lines, kept, docs, i = text.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for m in _HEREDOC.finditer(line.replace("<<<", "   ")):
            body = []
            while i < len(lines) and lines[i].strip() != m.group(2):
                body.append(lines[i])
                i += 1
            i += 1
            docs.append((line, "\n".join(body)))
    return "\n".join(kept), docs


def _simple_commands(text):
    """Split shell text into simple commands: lists of words, quotes removed.
    Raises ValueError when the text does not parse (unbalanced quotes)."""
    import shlex
    lex = shlex.shlex(text.replace("\\\n", " "), posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    cmds, cur, skip_target = [], [], False
    for tok in lex:
        if tok and set(tok) <= set(";&|()<>\n"):
            if tok[0] in "<>" or tok in (">&", "&>", ">>&", "&>>"):
                skip_target = True           # a redirection: its target is no word
                continue
            if cur:
                cmds.append(cur)
            cur = []
            continue
        if skip_target:
            skip_target = False
            continue
        cur.append(tok)
    if cur:
        cmds.append(cur)
    return cmds


def _unresolved_program(words, depth):
    """Why this simple command's program cannot be read, or None."""
    i = 0
    while i < len(words) and (words[i] in _KEYWORDS or _ASSIGNMENT.match(words[i])):
        i += 1
    if i < len(words) and words[i] in _SKIP_COMPOUND:
        return None
    while i < len(words):
        prog = words[i]
        if _hidden(prog):
            return f"its program word `{prog}` is not a literal"
        name = prog.rsplit("/", 1)[-1]
        if name == "eval":
            return "`eval` re-reads text as a command"
        if name in _WRAPPERS:
            valued, i = _WRAPPERS[name], i + 1
            if name == "timeout":
                while i < len(words) and words[i].startswith("-"):
                    i += 2 if words[i] in valued else 1
                i += 1                                   # the duration
            while i < len(words) and (words[i].startswith("-") or
                                      (name == "env" and _ASSIGNMENT.match(words[i]))):
                i += 2 if words[i] in valued else 1
            if name == "xargs" and i < len(words) and \
                    words[i].rsplit("/", 1)[-1] == "hermes":
                return "`xargs hermes` takes its words from stdin, which the gate cannot see"
            continue
        rest = words[i + 1:]
        if name in _SHELLS and "-c" in rest:
            k = rest.index("-c")
            if k + 1 >= len(rest):
                return f"`{name} -c` has no command text"
            return _unresolved(rest[k + 1], depth + 1)
        if name == "hermes":
            return _unresolved_hermes(rest)
        return None
    return None


def _unresolved_hermes(rest):
    """Everything up to and including the kanban verb decides WHICH RULE
    APPLIES: the board names the table, the verb names the action. A board or
    verb the gate cannot read is not a known-bad move -- it is a move the gate
    would have to guess at, so it refuses. Words after the verb (`complete $ID`)
    do not change which rule applies, so variables there stay allowed."""
    j, seen_kanban = 0, False
    while j < len(rest):
        w = rest[j]
        if _hidden(w):
            return f"`hermes` is given `{w}` where its board, command or verb must be literal"
        if w.startswith("-"):
            if w in _FLAGS_WITH_VALUE and j + 1 < len(rest):
                if _hidden(rest[j + 1]):
                    return f"`hermes {w}` is given `{rest[j + 1]}`, which the gate cannot read"
                j += 2
            else:
                j += 1
            continue
        if not seen_kanban:
            if w.rsplit("/", 1)[-1] != "kanban":
                return None                      # another hermes command
            seen_kanban = True
            j += 1
            continue
        if w == "boards" and j + 1 < len(rest) and _hidden(rest[j + 1]):
            return f"`hermes kanban boards` is given `{rest[j + 1]}`"
        return None                              # the verb, literal
    return None


def _unresolved(text, depth=0):
    """Why this shell text contains a command the gate cannot read, or None."""
    if depth > 6:
        return "commands nested too deep to read"
    text, docs = _split_heredocs(text)
    for opener, body in docs:
        # A heredoc fed to a shell is a script typed inline: read it as one.
        words = opener.split()
        if words and any(w.rsplit("/", 1)[-1] in _SHELLS for w in words[:3]):
            why = _unresolved(body, depth + 1)
            if why:
                return why
    for inner in _substitutions(text):
        why = _unresolved(inner, depth + 1)
        if why:
            return why
    try:
        cmds = _simple_commands(text)
    except ValueError as exc:
        return f"the shell text does not parse ({exc})"
    for words in cmds:
        why = _unresolved_program(words, depth)
        if why:
            return why
    return None


def check_side_door(matrix, tool_name, args, profile=None):
    """Return a refusal string when a non-kanban tool reaches the board, else None."""
    if not isinstance(args, dict):
        return None
    role = role_for(matrix, profile)
    for key in matrix["text_keys"]:
        val = args.get(key)
        if not isinstance(val, str) or not val:
            continue
        if key in _SHELL_KEYS:
            why = _unresolved(val)
            if why:
                return (f"[kanban-harness] {tool_name} refused: {why}; "
                        f"{_UNRESOLVED_HINT}. The tool was NOT run.")
        for verb in _shell_verbs(val):
            if verb in matrix["always_verbs"]:
                continue
            what = f"{tool_name} `hermes kanban {verb}`"
            rows = granted(matrix, role, verb, "shell")
            if not rows:
                return _refusal(matrix, role, profile, what)
            ok, reasons = with_evidence(rows, _current_workspace())
            if not ok:
                return _evidence_refusal(role, profile, what, reasons)
        for rx, label in matrix["deny_patterns"]:
            if rx.search(val):
                return (f"[kanban-harness] {tool_name} refused: it reaches {label}, a side "
                        f"door around the board's transition matrix. Use the kanban_* "
                        f"tools instead. The tool was NOT run.")
    for key in matrix["path_keys"]:
        val = args.get(key)
        if isinstance(val, str) and any(rx.search(os.path.expanduser(val))
                                        for rx in matrix["path_patterns"]):
            return (f"[kanban-harness] {tool_name} refused: {val} is board storage, a side "
                    f"door around the transition matrix. The tool was NOT run.")
    return None


def _log_path(cfg):
    if cfg and cfg.get("log_file"):
        return Path(cfg["log_file"])
    # The shared home, NOT $HERMES_HOME: a worker's HERMES_HOME is its own
    # profile dir, and refusals across every profile belong in one log.
    return Path(os.path.expanduser("~/.hermes")) / "logs" / "kanban-harness.log"


def _log(cfg, tool_name, profile, board, msg):
    try:
        p = _log_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] [BLOCKED] board={board} profile={profile} "
                     f"tool={tool_name} {msg}\n")
    except Exception:
        pass


def _on_pre_tool_call(tool_name=None, args=None, **kwargs):
    if not tool_name:
        return None
    try:
        _run_started()  # the run's first tool call snapshots its starting state
    except Exception:
        pass
    is_kanban = tool_name.startswith(_TOOL_PREFIX)
    try:
        cfg = load_config()
    except HarnessConfigError as exc:
        # Fail closed. Kanban tools are refused outright; shell and code tools
        # still face every built-in side door (the lockdown table), so a broken
        # config opens no door anywhere.
        profile = _current_profile()
        board = str((args or {}).get("board") or os.environ.get("HERMES_KANBAN_BOARD") or "")
        if is_kanban:
            msg = (f"[kanban-harness] {tool_name} refused: harness config "
                   f"unreadable ({exc}); kanban calls are locked until the user fixes it.")
        else:
            try:
                msg = check_side_door(_LOCKDOWN, tool_name, args, profile)
            except Exception as check_exc:
                msg = f"[kanban-harness] {tool_name} refused: side-door check failed ({check_exc})."
            if msg:
                msg = f"{msg} (harness config unreadable: {exc})"
        if not msg:
            return None
        msg = _final(msg, None, profile)
        # A refusal that leaves no trace is indistinguishable from no harness:
        # log it at the fixed default path (no config means no log_file).
        _log(None, tool_name, profile, board, msg)
        return {"action": "block", "message": msg}
    board = profile = None
    try:
        profile = _current_profile()
        if is_kanban:
            board = _call_board(args)
            matrix = matrix_for(cfg, board)
            tid = str((args or {}).get("task_id")
                      or os.environ.get("HERMES_KANBAN_TASK", "")).strip()
            msg = check_kanban_tool(
                matrix, tool_name, profile,
                is_root=lambda: _db_task_is_root(matrix, board, tid) if tid else True,
                workspace=_current_workspace())
            if not msg and tool_name == "kanban_create":
                msg = _terminal_key_refusal(board, (args or {}).get("idempotency_key"))
        else:
            board = os.environ.get("HERMES_KANBAN_BOARD", "").strip() or None
            matrix = matrix_for(cfg, board)
            msg = check_side_door(matrix, tool_name, args, profile)
    except Exception as exc:  # fail closed: a check that cannot decide refuses
        msg = f"[kanban-harness] {tool_name} refused: harness check failed ({exc})."
        matrix = None
    if not msg:
        return None
    msg = _final(msg, matrix, profile)
    _log(cfg, tool_name, profile, board, msg)
    return {"action": "block", "message": msg}


def _final(msg, matrix, profile):
    """Every refusal ends the same way: it names the caller's role, says it is
    final, and names the one legal way forward. A refusal that reads like a
    transient failure produces a retry storm against a wall."""
    role = role_for(matrix, profile) if matrix else None
    who = f"role {role['name']!r}" if role else f"profile {profile!r} (no role)"
    return (f"{msg} This refusal is final for {who}: do not repeat the call or try "
            f"another status. The only way to finish your part is kanban_handoff.")


def _terminal_key_refusal(board, key):
    """An idempotency key that resolves to a finished card would hand that card
    back to the caller as if fresh -- a way into a terminal state with no move
    at all. Refuse, and name the card."""
    if not key:
        return None
    from hermes_cli import kanban_db as kb
    conn = kb.connect(board=board)
    try:
        row = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ? "
            "AND status IN ('done', 'archived') ORDER BY created_at DESC LIMIT 1",
            (str(key),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return (f"[kanban-harness] kanban_create refused: idempotency_key {key!r} already "
            f"belongs to card {row[0]}, which is {row[1]}. A finished card is never "
            f"handed back as a new one; use a new key for new work.")


# ---------------------------------------------------------------------------
# kanban_handoff — performs a `handoff` row of the table
# ---------------------------------------------------------------------------

HANDOFF_SCHEMA = {
    "name": _HANDOFF_TOOL,
    "description": (
        "Hand your kanban task to the next role in this board's workflow. On a harnessed "
        "board this is how you finish your part of a task: moves the board's transition "
        "matrix does not grant you (often kanban_complete / kanban_block) are refused. "
        "Put what you did, how you verified it, and anything unverified or risky in the "
        "summary; the next role reviews it. After this call, stop working on the task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Hand-off note for the next role: each acceptance "
                               "criterion as met, not met, or could not check (an "
                               "empty reading is could not check unless you show it "
                               "could have found something), with evidence paths; "
                               "then open doubts. Required.",
            },
            "to_role": {
                "type": "string",
                "description": "Target role; only needed when your role has more than "
                               "one hand-off target.",
            },
            "question": {
                "type": "boolean",
                "description": "True when you cannot continue until a person decides -- "
                               "for example the card has no acceptance criteria. Parks "
                               "the card as a question for the user instead of handing "
                               "over finished work. Never guess instead.",
            },
            "task_id": {
                "type": "string",
                "description": "Your task id. Defaults to HERMES_KANBAN_TASK.",
            },
        },
        "required": ["summary"],
    },
}


def _err(msg):
    msg = str(msg)
    if not msg.startswith("[kanban-harness]"):
        msg = f"[kanban-harness] {msg}"
    return json.dumps({"error": msg}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# config/board.yaml at transition time: the edge, `requires`, `requires_to_leave`
# ---------------------------------------------------------------------------

#: What "acceptance criteria present" means on a card body: a heading or a line
#: that starts with "Acceptance criteria". The same shape as `root_marker`.
ACCEPTANCE_CRITERIA_MARKER = re.compile(r"(?mi)^\s*(#+\s*)?acceptance criteria\b")

_TEACH = {
    "comment": "put what you did, how you checked it, and the evidence paths in `summary`",
    "references_card": ("a card waits only on something named: link it to the card it waits "
                        "for (kanban_link) before moving it here"),
    "acceptance_criteria": ("the card has no acceptance criteria. Do not invent them and do not "
                            "work around it: route the card to the user as a question"),
}


def _role_assignees(matrix, role_name):
    """Every assignee value a role stands for (a human lane's name, or its profiles)."""
    role = matrix["roles"].get(role_name)
    if role is None:
        return {role_name}
    names = set(role.get("profiles") or [])
    if role.get("assignee"):
        names.add(role["assignee"])
    return names or {role_name}


def board_refusal(conn, spec, matrix, task_id, from_status, to_status, target, summary,
                  actor=None):
    """Why ``board.yaml`` forbids this move, or None when it allows it.

    Checks, in order: the move is an EDGE on the board; the table has a row for
    this (from, to, actor); the source column's `requires_to_leave`; the target
    column's `requires` (to enter). Each refusal names the rule and teaches the
    move that would satisfy it.
    """
    src, dst = spec.column_of(from_status), spec.column_of(to_status)
    if src is None or dst is None:
        return (f"moving a card from status {from_status!r} to {to_status!r} is not on the "
                f"board ({spec.path}): no column lives on "
                f"{from_status if src is None else to_status!r}.")
    if not spec.can_move(src, dst):
        return (f"{src!r} -> {dst!r} is not a move on this board; from {src!r} a card may go "
                f"to {spec.columns[src].exit_to}.")
    if spec.moves and not spec.allows(src, dst, actor or ""):
        mine = [f"{m['from']} -> {m['to']}" for m in spec.moves if m["by"] == actor]
        return (f"{src!r} -> {dst!r} is not a move role {actor!r} may make on this board. "
                f"Its moves: {', '.join(mine) or 'none'}. This refusal is final.")
    failed = []
    checks = [(spec.columns[src].requires_to_leave, f"to leave {src!r}"),
              (spec.columns[dst].requires, f"to enter {dst!r}")]
    for conditions, why in checks:
        for name, wanted in (conditions or {}).items():
            if wanted in (None, False):
                continue
            if name == "comment":
                ok = bool(summary and str(summary).strip())
            elif name == "assignee":
                ok = target in _role_assignees(matrix, str(wanted))
                if not ok:
                    failed.append(f"{why}, the card must be assigned to {wanted!r} "
                                  f"(hand off to that role; this move assigns it to {target!r})")
                    continue
            elif name == "references_card":
                ok = conn.execute("SELECT 1 FROM task_links WHERE child_id = ? LIMIT 1",
                                  (task_id,)).fetchone() is not None
            elif name == "acceptance_criteria":
                body = conn.execute("SELECT body FROM tasks WHERE id = ?",
                                    (task_id,)).fetchone()
                ok = bool(body and ACCEPTANCE_CRITERIA_MARKER.search(body[0] or ""))
            else:
                ok = False  # a condition this build does not know: fail closed, say so
                failed.append(f"{why}: unknown condition {name!r} in {spec.path}")
                continue
            if not ok:
                failed.append(f"{why}: {_TEACH.get(name, name)}")
    if failed:
        return "the board refuses this move — " + "; ".join(failed) + "."
    return None


# ---------------------------------------------------------------------------
# The hand-off form (board.yaml `handoff:`): the summary is checked against the
# form's fields and against what this run actually did
# ---------------------------------------------------------------------------
#
# Per task, in this worker process: the files file tools wrote, the tool calls
# that failed (and later successes that recovered them), and a snapshot of the
# workspace's git state taken the first time the harness sees the task.

_WRITE_TOOLS = frozenset({"write_file", "patch"})
_V4A_PATH = re.compile(r"(?m)^\*\*\* (?:Add File|Update File|Delete File|Move to):\s*(.+?)\s*$")
_RUNS = {}


def _task_record(tid):
    rec = _RUNS.get(tid)
    if rec is None:
        rec = _RUNS[tid] = {"writes": [], "failures": [], "ok": {}, "n": 0,
                            "git": None, "git_taken": False}
    return rec


def _run_started():
    """The record of the task this worker runs; snapshots git on first sight."""
    tid = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not tid:
        return None
    rec = _task_record(tid)
    if not rec["git_taken"]:
        rec["git_taken"] = True
        rec["git"] = _git_state(_current_workspace())
    return rec


def _file_hash(path):
    import hashlib
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except OSError:
        return None  # deleted, or a directory


def _git_state(workspace):
    """{"top": repo root, "files": {abs path: content hash}} of every path
    `git status` lists in the workspace, or None when it is not a git repo."""
    if not workspace or not os.path.isdir(workspace):
        return None
    import subprocess
    try:
        top = subprocess.run(["git", "-C", workspace, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return None
        out = subprocess.run(["git", "-C", workspace, "status", "--porcelain", "-z",
                              "--untracked-files=all"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
    except Exception:
        return None
    root = top.stdout.strip()
    files, entries, i = {}, out.stdout.split("\0"), 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        paths = [entry[3:]]
        if entry[0] in "RC" and i < len(entries):
            paths.append(entries[i])   # the rename's source path
            i += 1
        for p in paths:
            full = os.path.join(root, p)
            files[full] = _file_hash(full)
    return {"top": root, "files": files}


def _git_changed(before, workspace):
    """Paths that APPEARED or CHANGED in the workspace since the run started.

    A worker must never be refused for a file it did not touch, or the refusal
    stops meaning anything: the workspace may hold other people's dirty files,
    so only differences from the run's own starting snapshot are demanded."""
    if not before:
        return []
    after = _git_state(workspace)
    if not after:
        return []
    changed = []
    for full in sorted(set(before["files"]) | set(after["files"])):
        now = after["files"].get(full, _file_hash(full)) if full in after["files"] \
            else _file_hash(full)
        if full not in before["files"] or before["files"][full] != now:
            changed.append(full)
    return changed


def _key_args(args):
    keys = list(dict.fromkeys(_DEFAULT_PATH_KEYS + _DEFAULT_TEXT_KEYS + ["patch", "mode"]))
    return tuple((k, args[k]) for k in keys if isinstance(args.get(k), str) and args[k])


def _written_paths(tool_name, args):
    paths = [args[k] for k in _DEFAULT_PATH_KEYS if isinstance(args.get(k), str) and args[k]]
    if tool_name == "patch" and isinstance(args.get("patch"), str):
        paths += _V4A_PATH.findall(args["patch"])
    return paths


def _on_post_tool_call(tool_name=None, args=None, result=None, status=None,
                       error_message=None, **kwargs):
    """Observe only: remember what the run wrote and which calls failed."""
    try:
        rec = _run_started()
        if rec is None or not tool_name or tool_name == _HANDOFF_TOOL:
            return None
        args = args if isinstance(args, dict) else {}
        if status is None:  # upstream derives it the same way when not given
            try:
                parsed = json.loads(result) if isinstance(result, str) else result
                if isinstance(parsed, dict) and parsed.get("error"):
                    status, error_message = "error", str(parsed["error"])
                else:
                    status = "ok"
            except Exception:
                status = "ok"
        rec["n"] += 1
        key = (tool_name, _key_args(args))
        if status == "error":
            rec["failures"].append({"tool": tool_name, "key": key, "n": rec["n"],
                                    "error": str(error_message or "")})
        elif status == "ok":
            rec["ok"][key] = rec["n"]
            if tool_name in _WRITE_TOOLS:
                cwd = _current_workspace() or os.getcwd()
                for p in _written_paths(tool_name, args):
                    rec["writes"].append(os.path.abspath(
                        os.path.join(cwd, os.path.expanduser(p))))
    except Exception:
        pass
    return None


def _form_values(summary, fields):
    """{field (lower case): its text} for every `<Field>:` line in the summary.
    A field's text runs until the next field's line."""
    names = sorted(fields, key=len, reverse=True)
    rx = re.compile(r"(?im)^[ \t>*_-]*(" + "|".join(re.escape(f) for f in names)
                    + r")[ \t*_]*:(.*)$")
    values, last, last_end = {}, None, 0
    for m in rx.finditer(summary):
        if last is not None:
            values[last] += summary[last_end:m.start()]
        last = m.group(1).lower()
        values.setdefault(last, "")
        values[last] += m.group(2)
        last_end = m.end()
    if last is not None:
        values[last] += summary[last_end:]
    return values


def _prompt_line(basename):
    """The first line of this agent's system prompt (its SOUL.md) naming the form."""
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        try:
            text = (Path(os.path.expanduser(home)) / "SOUL.md").read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            if basename in line:
                return line.strip()
    return None


# A file on the Files line holding fewer bytes than this, whitespace stripped,
# cannot be the work (T47). 64 catches a placeholder -- "hello", "TODO", "wip",
# a lone heading -- and nothing more: a 65-byte lie passes, and so does any
# full-length file that is wrong. See the contract, "Placeholder deliverables".
_MIN_DELIVERABLE_BYTES = 64
_SATISFY = ("list the file that holds the work; if it really is meant to be tiny "
            "(an empty __init__.py), give it content -- a docstring is enough")


def _files_entries(listed):
    """The entries of a Files line: comma or newline separated, list bullets
    and backticks stripped, spaces inside an entry kept."""
    out = []
    for raw in re.split(r"[,\n]", listed):
        e = re.sub(r"^[ \t]*(?:[-*+][ \t]+)?", "", raw).strip().strip("`").strip()
        if e:
            out.append(e)
    return out


def _in_head(top, full):
    """True when `full` (inside the git top) is tracked at HEAD: a file the run
    deleted, which the Files line must list and cannot point at."""
    import subprocess
    try:
        rel = os.path.relpath(full, top)
        r = subprocess.run(["git", "-C", top, "cat-file", "-e", f"HEAD:{rel}"],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _markdown_hollow(text):
    """Why a markdown file is structurally empty, or None: only headings, or a
    table with a header and separator but no data row. Neither says the
    content is true."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and all(ln.startswith("#") for ln in lines):
        return "only headings"
    sep = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")
    for i, ln in enumerate(lines):
        if sep.match(ln) and i > 0 and lines[i - 1].startswith("|"):
            if i + 1 >= len(lines) or not lines[i + 1].startswith("|"):
                return "a table with no rows"
    return None


def deliverable_refusal(field, listed, workspace, top):
    """Why a path on the Files line cannot be work, or None (T47).

    Fails closed: a path is trusted only when it resolves, symlinks followed,
    inside the run's workspace or its git top. With no workspace there is no
    tree to contain a path, so no path can be trusted and only `none` passes.
    Do NOT default the workspace to the current directory: that hands back the
    bypass this closes (`Files: /etc/hosts`)."""
    if listed.strip().lower().rstrip(".") == "none":
        return None
    ws = workspace if workspace and os.path.isdir(workspace) else None
    if ws is None:
        return (f"the `{field}:` line names paths, but this run has no workspace, so no "
                f"path can be checked against a work tree; with no workspace only "
                f"`{field}: none` passes")
    roots = [os.path.realpath(r) for r in (ws, top) if r]
    for entry in _files_entries(listed):
        if entry.lower().rstrip(".") == "none":
            continue
        exp = os.path.expanduser(entry)
        cands = [exp] if os.path.isabs(exp) else [os.path.join(r, exp) for r in (ws, top) if r]
        hit = next((c for c in cands if os.path.lexists(c)), None)
        if hit is None:
            if top and any(_in_head(os.path.realpath(top), os.path.realpath(c)) for c in cands
                           if (os.path.realpath(c) + os.sep).startswith(
                               os.path.realpath(top) + os.sep)):
                continue  # a recorded deletion
            if " " in entry.strip() and "/" not in entry and "." not in entry:
                return (f"the `{field}:` line takes paths only -- one per line or comma "
                        f"separated -- or the single word `none`; {entry!r} is not a path")
            return (f"{entry} on the `{field}:` line does not exist in the work tree "
                    f"({ws}); the `{field}:` line takes paths only, or `none` -- {_SATISFY}")
        real = os.path.realpath(hit)
        if not any((real + os.sep).startswith(r + os.sep) for r in roots):
            return (f"{entry} resolves to {real}, outside the work tree ({ws}); a file on "
                    f"the `{field}:` line must live in the work tree")
        if os.path.isdir(real):
            return (f"{entry} is a directory; list the files, not the directory")
        try:
            data = Path(real).read_bytes()
            text = data.decode("utf-8") if real.lower().endswith(".md") else None
        except (OSError, UnicodeDecodeError) as exc:
            return (f"{entry} cannot be read ({type(exc).__name__}), so it cannot be shown "
                    f"to hold the work; a markdown file must be readable UTF-8")
        n = len(data.strip())
        if n < _MIN_DELIVERABLE_BYTES:
            return (f"{entry} is {n} bytes after stripping whitespace (under "
                    f"{_MIN_DELIVERABLE_BYTES}); a file this short cannot be the work -- "
                    f"{_SATISFY}")
        hollow = _markdown_hollow(text) if text is not None else None
        if hollow:
            return (f"{entry} holds {hollow}; a findings file carries its findings -- "
                    f"fill it in before handing off")
    return None


def form_refusal(form, summary, rec, workspace):
    """Why the summary does not meet the board's hand-off form, or None."""
    fields = [str(f) for f in form["fields"]]
    values = _form_values(summary, fields)
    why = None
    for f in fields:
        if f.lower() not in values:
            why = f"the summary has no `{f}:` line (write `{f}: none` if there is nothing)"
            break
    files_f = next((f for f in fields if f.lower() == "files"), None)
    could_f = next((f for f in fields if f.lower() == "could not check"), None)
    rec = rec or {"writes": [], "failures": [], "ok": {}, "git": None}
    if why is None and files_f:
        listed = values[files_f.lower()]
        top = (rec.get("git") or {}).get("top")
        demanded = list(dict.fromkeys(rec["writes"] + _git_changed(rec.get("git"), workspace)))
        for full in demanded:
            forms = {full}
            for base in (workspace, top):
                if base and (full + os.sep).startswith(os.path.abspath(base) + os.sep):
                    forms.add(os.path.relpath(full, base))
            if not any(f in listed for f in forms):
                shown = min(forms, key=len)
                why = (f"this run wrote or changed {shown}, and the `{files_f}:` line does "
                       f"not list it")
                break
    if why is None and files_f:
        why = deliverable_refusal(files_f, values[files_f.lower()], workspace,
                                  (rec.get("git") or {}).get("top"))
    if why is None and could_f and values[could_f.lower()].strip().lower().rstrip(".") == "none":
        for fail in rec["failures"]:
            if fail["key"][1] and rec["ok"].get(fail["key"], 0) > fail["n"]:
                continue  # the same call succeeded after it: recovered
            err = fail["error"].strip().replace("\n", " ")
            err = err[:120] + ("..." if len(err) > 120 else "")
            why = (f"`{could_f}: none`, but a failed {fail['tool']} call was recorded "
                   f"({err}); if you recovered from it, say so under {could_f}")
            break
    if why is None:
        return None
    base = os.path.basename(str(form["form"]))
    line = _prompt_line(base)
    said = (f'your system prompt says: "{line}"' if line
            else f"your system prompt does not mention {base}")
    return (f"[kanban-harness] kanban_handoff refused: {why}. The hand-off form is "
            f"{form['form']}; {said}.")


def handoff(conn, kb, matrix, task_id, profile, summary, to_role=None,
            expected_run_id=None, is_root=None, workspace=None, board_spec=None,
            question=False, form_check=None):
    """Perform a handoff row. Returns (ok, payload_or_error). Pure w.r.t. env.

    ``question=True`` picks the row that parks the card as a question
    (status ``blocked``) when a role has both that and a finished-work row to
    the same target. ``form_check(summary)`` returns why the summary misses
    the board's hand-off form, or None."""
    role = role_for(matrix, profile)
    rows = granted(matrix, role, _HANDOFF, "tool", is_root)
    if to_role:
        rows = [t for t in rows if t["to_role"] == to_role]
    # A question parks the card for a person (status blocked); anything else is
    # finished work or rework. The flag picks between them -- never the status.
    rows = [t for t in rows if (t["status"] == "blocked") == bool(question)]
    # With no to_role, finished work goes to the person when the role can reach
    # one (QA's pass lands in user review); rework must name its target.
    if not to_role and len(rows) > 1:
        human = [t for t in rows if matrix["roles"][t["to_role"]]["human"]]
        if len(human) == 1:
            rows = human
    what = _HANDOFF_TOOL + (f" to {to_role!r}" if to_role else "")
    if not rows:
        extra = _profile_hint(matrix["profile_role"], to_role,
                              "Pass to_role='{role}'.") if to_role else ""
        return False, _refusal(matrix, role, profile, what, extra)
    rows, reasons = with_evidence(rows, workspace)
    if not rows:
        return False, _evidence_refusal(role, profile, what, reasons)
    if len(rows) > 1:
        return False, (f"several hand-off targets are allowed "
                       f"({', '.join(t['to_role'] for t in rows)}); pass to_role.")
    if not summary or not str(summary).strip():
        return False, "summary is required: say what you did and how you verified it."
    row, summary = rows[0], str(summary).strip()
    nxt = matrix["roles"][row["to_role"]]
    target, new_status = nxt["assignee"], row["status"]
    if board_spec is not None:
        why = board_refusal(conn, board_spec, matrix, task_id, "running", new_status,
                            target, summary, actor=role["name"] if role else None)
        if why:
            return False, f"[kanban-harness] kanban_handoff refused: {why}"
    if form_check is not None:
        why = form_check(summary)
        if why:
            return False, why
    with kb.write_txn(conn):
        cur_row = conn.execute(
            "SELECT status, assignee, claim_lock, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if cur_row is None:
            return False, f"unknown task {task_id}."
        if cur_row["status"] != "running" or cur_row["assignee"] != profile:
            return False, (f"task {task_id} is {cur_row['status']} and assigned to "
                           f"{cur_row['assignee']!r}; only its running assignee can hand "
                           f"it off.")
        if expected_run_id is not None and cur_row["current_run_id"] not in (None, expected_run_id):
            return False, f"task {task_id} is on another run; this worker no longer owns it."
        run_id = kb._end_run(
            conn, task_id, outcome="handed_off", status="released", summary=summary,
            metadata={"handoff_from": profile, "handoff_to": target,
                      "from_role": role["name"], "to_role": nxt["name"]},
        )
        cur = conn.execute(
            "UPDATE tasks SET status = ?, assignee = ?, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ? AND status = 'running' "
            "AND claim_lock IS ?",
            (new_status, target, task_id, cur_row["claim_lock"]),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"hand-off of {task_id} lost a race; nothing changed")
        kb._append_event(conn, task_id, "handed_off", {
            "from": profile, "to": target, "from_role": role["name"],
            "to_role": nxt["name"], "status": new_status, "summary": summary[:2000],
        }, run_id=run_id)
        if new_status == "blocked":
            # A 'blocked' event makes the block sticky: recompute_ready won't promote it.
            kb._append_event(conn, task_id, "blocked", {
                "reason": f"awaiting {nxt['name']} ({target})"}, run_id=run_id)
        elif new_status == "scheduled":
            # user_review: finished work waiting for the person. The same event
            # upstream's own schedule_task writes. Nothing dispatches from here,
            # and the runtime has no door to done from here either: only the
            # user's `accept` leaves this column for done.
            kb._append_event(conn, task_id, "scheduled", {
                "reason": f"awaiting {nxt['name']} ({target}) in user_review"},
                run_id=run_id)
        else:
            # The task re-entered 'ready': record it as such, so age-based checks
            # (stranded_in_ready) measure from now, not from the card's creation.
            kb._append_event(conn, task_id, "promoted", {
                "by": "kanban-harness", "reason": f"handed off to {nxt['name']}"},
                run_id=run_id)
    kb.add_comment(conn, task_id, profile, f"[hand-off -> {nxt['name']} ({target})]\n{summary}")
    return True, {"task_id": task_id, "handed_off_to": target, "role": nxt["name"],
                  "human": nxt["human"], "status": new_status}


def _handle_handoff(args, **kw):
    args = args or {}
    env_tid = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    tid = str(args.get("task_id") or env_tid).strip()
    board = os.environ.get("HERMES_KANBAN_BOARD", "").strip() or None
    cfg = None

    def refused(msg):
        # Every refused hand-off reaches the agent AND the disk (D11), final.
        try:
            matrix = matrix_for(cfg, board) if cfg else None
        except Exception:
            matrix = None
        if "This refusal is final" not in msg:
            msg = _final(msg, matrix, _current_profile())
        _log(cfg, _HANDOFF_TOOL, _current_profile(), board or "", msg)
        return _err(msg)

    if not env_tid:
        return refused("kanban_handoff is for dispatcher-spawned workers only.")
    if tid != env_tid:
        return refused(f"you may only hand off your own task ({env_tid}).")
    try:
        cfg = load_config()
    except HarnessConfigError as exc:
        return refused(f"harness config unreadable ({exc}). This refusal is final.")
    try:
        from hermes_cli import kanban_db as kb
        if board is None:
            board = kb.get_current_board()
        matrix = matrix_for(cfg, board)
        conn = kb.connect(board=board)
        raw = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
        run_id = int(raw) if raw.isdigit() else None
        spec = cfg.get("board")
        form = getattr(spec, "handoff", None) if spec is not None else None
        form_check = None
        if isinstance(form, dict):
            rec = _run_started()
            form_check = lambda s: form_refusal(form, s, rec, _current_workspace())  # noqa: E731
        try:
            ok, res = handoff(conn, kb, matrix, tid, _current_profile(), args.get("summary"),
                              to_role=args.get("to_role"), expected_run_id=run_id,
                              is_root=lambda: _db_task_is_root(matrix, board, tid),
                              workspace=_current_workspace(),
                              board_spec=cfg.get("board"),
                              question=bool(args.get("question")),
                              form_check=form_check)
        finally:
            conn.close()
    except Exception as exc:
        return refused(f"hand-off failed: {exc}")
    if not ok:
        return refused(res)
    who = ("a human reviews it next; no worker is spawned" if res["human"]
           else f"the dispatcher will spawn {res['handed_off_to']!r} next")
    return json.dumps({"ok": True, **res,
                       "note": f"Handed off; {who}. Stop working on this task now."})


def _handoff_available():
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return False
    try:
        cfg = load_config()
        matrix = matrix_for(cfg, os.environ.get("HERMES_KANBAN_BOARD", "").strip() or None)
    except HarnessConfigError:
        return False
    if not matrix:
        return False
    role = role_for(matrix, _current_profile())
    return bool(role and any(t["action"] == _HANDOFF and t["from_role"] == role["name"]
                             for t in matrix["transitions"]))


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    try:
        _run_started()  # a worker process: its run starts here
    except Exception:
        pass
    ctx.register_tool(
        name=_HANDOFF_TOOL,
        toolset="kanban",
        schema=HANDOFF_SCHEMA,
        handler=_handle_handoff,
        check_fn=_handoff_available,
        description=HANDOFF_SCHEMA["description"],
        emoji="🤝",
    )


# ---------------------------------------------------------------------------
# Runnable self-test (decision logic only; no hermes-agent needed):
#   python3 plugins/kanban-harness/__init__.py
# The end-to-end test against the real kanban_db is tests/smoke/test_kanban_harness.py.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    os.environ["KANBAN_HARNESS_FILE"] = str(Path(__file__).resolve().parent
                                           / "harness.yaml.example")
    m = matrix_for(load_config(), "default")
    fails = 0

    def check(label, res, expect_block):
        global fails
        ok = (res is not None) == expect_block
        fails += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {'BLOCK' if res else 'allow'}")

    check("coder kanban_complete", check_kanban_tool(m, "kanban_complete", "coder"), True)
    check("qa kanban_block", check_kanban_tool(m, "kanban_block", "qa-tester"), True)
    check("coder kanban_comment", check_kanban_tool(m, "kanban_comment", "coder"), False)
    check("coder kanban_create", check_kanban_tool(m, "kanban_create", "coder"), False)
    check("stranger kanban_unblock", check_kanban_tool(m, "kanban_unblock", "x"), True)
    check("shell complete", check_side_door(
        m, "terminal", {"command": "hermes kanban --board b complete t_1"}, "coder"), True)
    check("shell list", check_side_door(
        m, "terminal", {"command": "hermes kanban list"}, "coder"), False)
    check("sqlite", check_side_door(
        m, "terminal", {"command": "sqlite3 ~/.hermes/kanban.db 'update tasks'"}), True)
    check("plain shell", check_side_door(m, "terminal", {"command": "pytest -q"}), False)
    raise SystemExit(1 if fails else 0)
