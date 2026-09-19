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
  ``mode: warn`` logs each would-be refusal and lets the call run.
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
from datetime import datetime
from pathlib import Path

_TOOL_PREFIX = "kanban_"
_HANDOFF = "handoff"
_HANDOFF_TOOL = _TOOL_PREFIX + _HANDOFF

# Mechanics, not policy: which tool arguments carry shell/code text or a file path.
_DEFAULT_TEXT_KEYS = ["command", "code", "script", "cmd", "input", "keys"]
_DEFAULT_PATH_KEYS = ["path", "file_path"]
_FLAGS_WITH_VALUE = {"--board", "-b", "--profile", "-p", "--tenant"}
_DEFAULT_ROOT_MARKER = r"(?mi)^\s*Level:\s*0\s*$"
_WHEN = ("any", "root", "non_root")
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
            raise HarnessConfigError(f"{w}: unknown from_role {t['from_role']!r}")
        action = str(t["action"])
        when = str(t.get("when", "any"))
        if when not in _WHEN:
            raise HarnessConfigError(f"{w}: when must be one of {_WHEN}")
        via = [str(v) for v in _as_list(t.get("via") or "tool")]
        if any(v not in _VIA for v in via):
            raise HarnessConfigError(f"{w}: via must be from {_VIA}")
        row = {"from_role": t["from_role"], "action": action, "when": when, "via": via,
               "to_role": t.get("to_role"), "status": t.get("status")}
        if action == _HANDOFF:
            if row["to_role"] not in roles:
                raise HarnessConfigError(f"{w}: handoff needs a known to_role")
            if row["status"] not in ("ready", "blocked"):
                raise HarnessConfigError(f"{w}: handoff needs status: ready | blocked")
            if row["status"] == "ready" and not roles[row["to_role"]]["human"] \
                    and not roles[row["to_role"]]["profiles"]:
                raise HarnessConfigError(f"{w}: to_role has no profile to spawn")
            if "shell" in via:
                raise HarnessConfigError(f"{w}: handoff has no shell form")
        transitions.append(row)

    aa = raw.get("always_allow") or {}
    sd = raw.get("side_doors") or {}
    return {
        "roles": roles,
        "profile_role": profile_role,
        "transitions": transitions,
        "always_tools": set(_as_list(aa.get("tools"))),
        "always_verbs": set(_as_list(aa.get("shell_verbs"))),
        "unknown_profile": str(raw.get("unknown_profile", "deny")).lower(),
        "root_marker": re.compile(raw.get("root_marker") or _DEFAULT_ROOT_MARKER),
        "side_doors": bool(sd.get("enabled", True)),
        "deny_patterns": [(re.compile(p["pattern"]), p.get("label") or p["pattern"])
                          for p in _as_list(sd.get("deny_patterns"))],
        "path_patterns": [re.compile(p) for p in _as_list(sd.get("deny_path_patterns"))],
        "text_keys": _as_list(sd.get("text_arg_keys")) or _DEFAULT_TEXT_KEYS,
        "path_keys": _as_list(sd.get("path_arg_keys")) or _DEFAULT_PATH_KEYS,
    }


def _normalize(doc):
    """Validate the raw document and return a normalized config dict."""
    if not isinstance(doc, dict):
        raise HarnessConfigError("config root must be a mapping")
    mode = str(doc.get("mode", "enforce")).lower()
    if mode not in ("enforce", "warn"):
        raise HarnessConfigError("mode must be enforce | warn")
    base = {k: doc[k] for k in _MATRIX_KEYS if k in doc}
    boards = doc.get("boards")
    if boards is None:
        boards = ["*"]
    if isinstance(boards, list):
        boards = {str(b): {} for b in boards}
    if not isinstance(boards, dict):
        raise HarnessConfigError("boards must be a list of slugs or a mapping")
    matrices = {}
    for slug, override in boards.items():
        merged = dict(base)
        merged.update(override or {})
        matrices[str(slug)] = _normalize_matrix(merged, f"boards.{slug}")
    log_file = doc.get("log_file")
    return {
        "enabled": bool(doc.get("enabled", True)),
        "mode": mode,
        "log_file": os.path.expanduser(str(log_file)) if log_file else None,
        "matrices": {k: v for k, v in matrices.items() if k != "*"},
        "default": matrices.get("*"),
    }


def load_config():
    """Return the normalized config, cached by mtime. Raises HarnessConfigError."""
    path = _config_path()
    sig = (str(path), _mtime(path))
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
    if cfg["default"] is not None:
        return cfg["default"]
    if board is None and cfg["matrices"]:
        return next(iter(cfg["matrices"].values()))
    return None


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


def _refusal(matrix, role, profile, what):
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
            f"{profile!r}): not in this board's transition matrix. {hint} "
            f"The tool was NOT run.")


def check_kanban_tool(matrix, tool_name, profile, is_root=None):
    """Return a refusal string for a kanban_* tool, or None to allow."""
    if tool_name in matrix["always_tools"]:
        return None
    role = role_for(matrix, profile)
    if role is None and matrix["unknown_profile"] == "allow":
        return None
    action = tool_name[len(_TOOL_PREFIX):]
    if granted(matrix, role, action, "tool", is_root):
        return None
    return _refusal(matrix, role, profile, tool_name)


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


def check_side_door(matrix, tool_name, args, profile=None):
    """Return a refusal string when a non-kanban tool reaches the board, else None."""
    if not matrix["side_doors"] or not isinstance(args, dict):
        return None
    role = role_for(matrix, profile)
    for key in matrix["text_keys"]:
        val = args.get(key)
        if not isinstance(val, str) or not val:
            continue
        for verb in _shell_verbs(val):
            if verb in matrix["always_verbs"]:
                continue
            if role is None and matrix["unknown_profile"] == "allow":
                continue
            if granted(matrix, role, verb, "shell"):
                continue
            return _refusal(matrix, role, profile, f"{tool_name} `hermes kanban {verb}`")
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
    home = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser("~/.hermes")
    return Path(home) / "logs" / "kanban-harness.log"


def _log(cfg, verdict, tool_name, profile, board, msg):
    try:
        p = _log_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] [{cfg['mode']}] [{verdict}] board={board} profile={profile} "
                     f"tool={tool_name} {msg}\n")
    except Exception:
        pass


def _on_pre_tool_call(tool_name=None, args=None, **kwargs):
    if not tool_name:
        return None
    is_kanban = tool_name.startswith(_TOOL_PREFIX)
    try:
        cfg = load_config()
    except HarnessConfigError as exc:
        # Fail closed for anything that touches the board; leave other tools alone.
        if is_kanban:
            return {"action": "block",
                    "message": f"[kanban-harness] {tool_name} refused: harness config "
                               f"unreadable ({exc}); kanban calls are locked until it is "
                               f"fixed."}
        return None
    board = profile = None
    try:
        if not cfg["enabled"]:
            return None
        profile = _current_profile()
        if is_kanban:
            board = _call_board(args)
            matrix = matrix_for(cfg, board)
            if matrix is None:
                return None
            tid = str((args or {}).get("task_id")
                      or os.environ.get("HERMES_KANBAN_TASK", "")).strip()
            msg = check_kanban_tool(
                matrix, tool_name, profile,
                is_root=lambda: _db_task_is_root(matrix, board, tid) if tid else True)
        else:
            board = os.environ.get("HERMES_KANBAN_BOARD", "").strip() or None
            matrix = matrix_for(cfg, board)
            if matrix is None:
                # A shell command can name any board: guard with a configured matrix.
                matrix = next(iter(cfg["matrices"].values()), None)
            msg = check_side_door(matrix, tool_name, args, profile) if matrix else None
    except Exception as exc:  # fail closed on board-touching calls only
        msg = (f"[kanban-harness] {tool_name} refused: harness check failed ({exc})."
               if is_kanban else None)
    if not msg:
        return None
    if cfg["mode"] == "warn":
        _log(cfg, "WOULD-BLOCK", tool_name, profile, board, msg)
        return None
    _log(cfg, "BLOCKED", tool_name, profile, board, msg)
    return {"action": "block", "message": msg}


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
                "description": "Hand-off note for the next role: what changed, evidence, "
                               "open doubts. Required.",
            },
            "to_role": {
                "type": "string",
                "description": "Target role; only needed when your role has more than "
                               "one hand-off target.",
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


def handoff(conn, kb, matrix, task_id, profile, summary, to_role=None,
            expected_run_id=None, is_root=None):
    """Perform a handoff row. Returns (ok, payload_or_error). Pure w.r.t. env."""
    role = role_for(matrix, profile)
    rows = granted(matrix, role, _HANDOFF, "tool", is_root)
    if to_role:
        rows = [t for t in rows if t["to_role"] == to_role]
    if not rows:
        return False, _refusal(matrix, role, profile,
                               _HANDOFF_TOOL + (f" to {to_role!r}" if to_role else ""))
    if len(rows) > 1:
        return False, (f"several hand-off targets are allowed "
                       f"({', '.join(t['to_role'] for t in rows)}); pass to_role.")
    if not summary or not str(summary).strip():
        return False, "summary is required: say what you did and how you verified it."
    row, summary = rows[0], str(summary).strip()
    nxt = matrix["roles"][row["to_role"]]
    target, new_status = nxt["assignee"], row["status"]
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
    kb.add_comment(conn, task_id, profile, f"[hand-off -> {nxt['name']} ({target})]\n{summary}")
    return True, {"task_id": task_id, "handed_off_to": target, "role": nxt["name"],
                  "human": nxt["human"], "status": new_status}


def _handle_handoff(args, **kw):
    args = args or {}
    env_tid = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    tid = str(args.get("task_id") or env_tid).strip()
    if not env_tid:
        return _err("kanban_handoff is for dispatcher-spawned workers only.")
    if tid != env_tid:
        return _err(f"you may only hand off your own task ({env_tid}).")
    try:
        cfg = load_config()
    except HarnessConfigError as exc:
        return _err(f"harness config unreadable ({exc}).")
    board = os.environ.get("HERMES_KANBAN_BOARD", "").strip() or None
    try:
        from hermes_cli import kanban_db as kb
        if board is None:
            board = kb.get_current_board()
        matrix = matrix_for(cfg, board)
        if matrix is None:
            return _err(f"board {board!r} is not harnessed; kanban_handoff does not apply.")
        conn = kb.connect(board=board)
        raw = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
        run_id = int(raw) if raw.isdigit() else None
        try:
            ok, res = handoff(conn, kb, matrix, tid, _current_profile(), args.get("summary"),
                              to_role=args.get("to_role"), expected_run_id=run_id,
                              is_root=lambda: _db_task_is_root(matrix, board, tid))
        finally:
            conn.close()
    except Exception as exc:
        return _err(f"hand-off failed: {exc}")
    if not ok:
        return _err(res)
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
    if not (cfg["enabled"] and matrix):
        return False
    role = role_for(matrix, _current_profile())
    return bool(role and any(t["action"] == _HANDOFF and t["from_role"] == role["name"]
                             for t in matrix["transitions"]))


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
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
