"""policy-gate — a Claude-Code-style permission gate for the Hermes agent.

Hermes is allow-everything by default in CLI mode (the built-in ACP edit-approval
guard is bypassed outside ACP). This plugin restores Claude Code's deny/ask
permission model AND adds a filesystem scope jail, by reading a SELF-CONTAINED
policy file in CC's ``settings.json`` format and enforcing it through Hermes'
``pre_tool_call`` hook (the analog of CC's ``PreToolUse`` exit-2 block).

Policy source (SELF-CONTAINED — no ``extends``, no external base file):
  * ``AGENT_POLICY_FILE`` env var, if set; otherwise
  * the plugin's own ``settings.hermes.json`` (your real, git-ignored policy); otherwise
  * the committed ``settings.hermes.json.example`` template (generic defaults, warn mode).

The document has two blocks:
  * ``permissions`` — CC-format ``deny`` / ``ask`` rule lists.
  * ``hermes``      — the filesystem scope jail (no CC equivalent).

CC rule -> Hermes tool translation:
    Bash(<glob>)        -> terminal(command)
    Write/Edit(<glob>)  -> write_file / edit_file (path)
    Read(<glob>)        -> read_file (path)

Folder scope (``hermes`` block):
    mode:             "warn" (log would-be-blocks, DON'T block) | "enforce" (block)
    allowedRoots:     absolute roots the agent may write within ("~" expanded)
    denyOutsideRoots: true -> writes resolving outside every root are scope violations
    scopedTools:      tools the root check applies to (default: file-write tools)

Fail-open throughout: a load or match error never blocks a tool call.
"""
import fnmatch
import json
import os
import re
from datetime import datetime
from pathlib import Path


def _policy_path():
    """Resolve the self-contained policy file.

    ``AGENT_POLICY_FILE`` wins; else the plugin's own ``settings.hermes.json``
    (your real, git-ignored policy); else the committed ``.example`` template.
    """
    env = os.environ.get("AGENT_POLICY_FILE", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    here = Path(__file__).resolve().parent
    real = here / "settings.hermes.json"
    if real.exists():
        return real
    return here / "settings.hermes.json.example"


# Hermes tool name -> (CC rule prefixes to check, arg key holding the target string)
_TOOL_MAP = {
    "terminal": (("Bash",), "command"),
    "write_file": (("Write", "Edit"), "path"),
    "edit_file": (("Write", "Edit"), "path"),
    "read_file": (("Read",), "path"),
}

_CACHE = {"sig": None, "rules": {"deny": [], "ask": []}, "scope": None}


def _mtime(p):
    try:
        return os.stat(p).st_mtime
    except OSError:
        return -1.0


def _parse_perms(perms, rules):
    for kind in ("deny", "ask"):
        for entry in perms.get(kind, []) or []:
            m = re.match(r"^([A-Za-z_]+)\((.*)\)$", str(entry).strip())
            if m:
                rules[kind].append((m.group(1), m.group(2)))


def _load():
    """Return (rules, scope), cached by the policy file's mtime signature.

    rules = {'deny': [(cc_tool, pattern)], 'ask': [...]}.
    scope = {'mode','roots','deny_outside','tools'} or None when no scope block.
    """
    policy = _policy_path()
    sig = (str(policy), _mtime(policy))
    if sig == _CACHE["sig"]:
        return _CACHE["rules"], _CACHE["scope"]

    doc = {}
    try:
        doc = json.load(open(policy)) or {}
    except Exception:
        doc = {}

    rules = {"deny": [], "ask": []}
    _parse_perms((doc.get("permissions") or {}), rules)

    scope = None
    h = doc.get("hermes") or {}
    if h.get("allowedRoots"):
        roots = []
        for r in h["allowedRoots"]:
            try:
                roots.append(os.path.realpath(os.path.expanduser(str(r))))
            except Exception:
                pass
        scope = {
            "mode": (h.get("mode") or "warn").lower(),
            "roots": roots,
            "deny_outside": bool(h.get("denyOutsideRoots", True)),
            "tools": set(h.get("scopedTools") or ["write_file", "edit_file", "patch"]),
        }

    _CACHE["sig"] = sig
    _CACHE["rules"] = rules
    _CACHE["scope"] = scope
    return rules, scope


def _matches(cc_pattern, target):
    """Match a CC permission glob against a Hermes tool target (command or path)."""
    p = cc_pattern[1:] if cc_pattern.startswith("//") else cc_pattern  # //abs -> /abs
    if fnmatch.fnmatch(target, p):
        return True
    # Bare token (no wildcard / path) -> substring match, mirroring CC's loose Bash() rules.
    if "*" not in p and "/" not in p and p in target:
        return True
    return False


def _abs_target(path):
    """Absolute realpath of a tool path arg, or None when it can't be resolved to an
    absolute location (relative paths: cwd unknown -> we skip the root check, fail-open)."""
    if not isinstance(path, str) or not path:
        return None
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        return None
    try:
        return os.path.realpath(p)
    except Exception:
        return None


def _in_roots(abspath, roots):
    for r in roots:
        if abspath == r or abspath.startswith(r.rstrip("/") + "/"):
            return True
    return False


def _scope_log_dir():
    override = os.environ.get("HERMES_CMDLOG_DIR", "").strip()
    if override:
        return Path(override)
    try:
        return Path(__file__).resolve().parents[2] / "logs"
    except Exception:
        return Path(os.path.expanduser("~/.hermes/logs"))


def _log_scope_violation(tool_name, path, mode):
    try:
        d = _scope_log_dir()
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        verb = "WOULD-BLOCK" if mode != "enforce" else "BLOCKED"
        with open(d / "scope-violations.log", "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] [{mode}] [{verb}] [{tool_name}] OUTSIDE-ROOTS {path}\n")
    except Exception:
        pass


def _on_pre_tool_call(tool_name=None, args=None, **kwargs):
    rules, scope = _load()

    # 1) deny/ask permission rules (Claude Code parity)
    mapping = _TOOL_MAP.get(tool_name)
    if mapping:
        cc_tools, arg_key = mapping
        target = (args or {}).get(arg_key)
        if isinstance(target, str) and target:
            for kind in ("deny", "ask"):
                for cc_tool, pattern in rules[kind]:
                    if cc_tool in cc_tools and _matches(pattern, target):
                        verb = "denied" if kind == "deny" else "requires human approval"
                        return {
                            "action": "block",
                            "message": (
                                f"[policy-gate] {tool_name} {verb} by policy "
                                f"— matched {cc_tool}({pattern}). Tool was NOT run; "
                                f"a human must do this."
                            ),
                        }

    # 2) filesystem scope jail (hermes.allowedRoots); warn => log only, enforce => block
    if scope and scope["deny_outside"] and tool_name in scope["tools"]:
        ap = _abs_target((args or {}).get("path"))
        if ap is not None and not _in_roots(ap, scope["roots"]):
            path = (args or {}).get("path")
            if scope["mode"] == "enforce":
                return {
                    "action": "block",
                    "message": (
                        f"[policy-gate] {tool_name} blocked — path {path} is outside the "
                        f"allowed roots {scope['roots']}. Work inside an allowed root, or a "
                        f"human must do this."
                    ),
                }
            _log_scope_violation(tool_name, path, scope["mode"])
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)


# ---------------------------------------------------------------------------
# Runnable self-test:  python3 plugins/policy-gate/__init__.py
# Builds a temp self-contained policy file and checks deny + warn/enforce scope.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    tmp = tempfile.mkdtemp(prefix="policy-gate-selftest-")
    policy = os.path.join(tmp, "settings.hermes.json")
    root = os.path.join(tmp, "project")
    os.makedirs(root, exist_ok=True)
    outside = os.path.join(tmp, "outside")
    os.makedirs(outside, exist_ok=True)

    json.dump({
        "permissions": {"deny": ["Bash(rm -rf /*)", "Bash(pip3 install*)"], "ask": []},
        "hermes": {"mode": "warn", "allowedRoots": [root],
                   "denyOutsideRoots": True, "scopedTools": ["write_file"]},
    }, open(policy, "w"))
    os.environ["AGENT_POLICY_FILE"] = policy
    os.environ["HERMES_CMDLOG_DIR"] = tmp

    def check(label, res, expect_block):
        ok = (res is not None) == expect_block
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: "
              f"{'BLOCK' if res else 'allow'} {('— ' + res['message'][:60]) if res else ''}")

    print("warn mode:")
    check("deny pip3", _on_pre_tool_call("terminal", {"command": "pip3 install x"}), True)
    check("deny rm -rf /", _on_pre_tool_call("terminal", {"command": "rm -rf /etc"}), True)
    check("write inside root", _on_pre_tool_call(
        "write_file", {"path": os.path.join(root, "x.py")}), False)
    check("write OUTSIDE root (warn=allow+log)", _on_pre_tool_call(
        "write_file", {"path": os.path.join(outside, "y.py")}), False)

    # flip to enforce
    json.dump({
        "permissions": {"deny": [], "ask": []},
        "hermes": {"mode": "enforce", "allowedRoots": [root],
                   "denyOutsideRoots": True, "scopedTools": ["write_file"]},
    }, open(policy, "w"))
    _CACHE["sig"] = None  # bust cache
    print("enforce mode:")
    check("write OUTSIDE root (enforce=block)", _on_pre_tool_call(
        "write_file", {"path": os.path.join(outside, "y.py")}), True)
    check("write inside root", _on_pre_tool_call(
        "write_file", {"path": os.path.join(root, "x.py")}), False)

    print("--- scope-violations.log ---")
    p = Path(tmp) / "scope-violations.log"
    print(p.read_text().rstrip() if p.exists() else "(none)")
