"""Write scope guard for a bridged session (claude-code-bridge): a session writes only inside its own repo.

Installed as a PreToolUse hook (matcher ``Write|Edit|MultiEdit|NotebookEdit``) in a repo channel's
``.claude/settings.json`` when the channel sets ``repo_scope_guard: true``. Arguments, all written by
lib/session.py at launch:

- ``--root``: the code root that holds the repos (``repo_scope_root``). A write outside it is not ours to judge.
- ``--session-dir``: the channel's ``cwd``. Its git toplevel is the session's own repo (a worktree maps back
  to the main repo through the common git dir).
- ``--companion`` (repeatable): extra directories the session may also write (``repo_scope_companions``).

A write passes when its path is inside the session's own repo, inside that repo's ``.claude`` (resolved),
or inside a companion. A write under ``--root`` to any other repo is denied: exit 2, and the message on
stderr names the owning repo, which goes back to the model. Anything else fails open (exit 0): a path
outside the root, no session repo, an unreadable payload, a tool that is not a write.

Stdlib only; reads the hook JSON from stdin. It runs from the channel directory and imports nothing from the bridge.
"""

import argparse
import json
import os
import subprocess
import sys

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def nearest_dir(path: str) -> str:
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            return ""
        path = parent
    return path


def git(directory: str, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", directory, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def session_roots(session_dir: str) -> set[str]:
    """The session's repo: its git toplevel, plus the main repo when the session is a worktree."""
    sd = nearest_dir(os.path.realpath(session_dir))
    top = git(sd, "rev-parse", "--show-toplevel") if sd else ""
    if not top:
        return set()
    roots = {os.path.realpath(top)}
    common = git(sd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common and os.path.basename(common.rstrip("/")) == ".git":
        roots.add(os.path.realpath(os.path.dirname(common.rstrip("/"))))
    return roots


def own_repo(session_dir: str) -> str:
    """The session's own repo name: the main repo of a worktree, else the git toplevel ("" when none)."""
    sd = nearest_dir(os.path.realpath(session_dir))
    if not sd:
        return ""
    common = git(sd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common and os.path.basename(common.rstrip("/")) == ".git":
        return os.path.basename(os.path.dirname(common.rstrip("/")))
    return os.path.basename(git(sd, "rev-parse", "--show-toplevel"))


def decide(data: dict, root: str, session_dir: str, companions: list[str]) -> str | None:
    """None to allow; otherwise the block message for stderr."""
    if data.get("tool_name") not in WRITE_TOOLS:
        return None
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or ""
    if not path:
        return None
    cwd = data.get("cwd") or os.getcwd()
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    code_root = os.path.realpath(root)
    lex = os.path.normpath(path)
    real = os.path.realpath(path)
    if not any(under(p, r) for p in (lex, real) for r in (code_root, os.path.normpath(root))):
        return None  # outside the code root: not this guard's business
    roots = session_roots(session_dir)
    if not roots:
        return None  # no repo to scope to: fail open
    allowed = set(roots)
    for r in roots:
        allowed.add(os.path.realpath(os.path.join(r, ".claude")))
    allowed |= {os.path.realpath(os.path.expanduser(c)) for c in companions}
    if any(under(p, a) for p in (lex, real) for a in allowed):
        return None
    # Owning repo of the target: its nearest git toplevel, else the first path component under the root.
    target_dir = nearest_dir(real)
    owner_top = git(target_dir, "rev-parse", "--show-toplevel") if target_dir else ""
    owner = os.path.basename(owner_top) if owner_top else ""
    if not owner:
        base = code_root if under(real, code_root) else os.path.normpath(root)
        owner = os.path.relpath(real if under(real, code_root) else lex, base).split(os.sep)[0]
    own = own_repo(session_dir) or os.path.basename(sorted(roots)[0])
    return (
        f"BLOCKED: this session belongs to repo '{own}' and may not write into repo '{owner}'.\n"
        f"Target: {lex}\n"
        f"Message the owner of repo '{owner}' instead; it makes the change under its own gates.\n"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", default="")
    parser.add_argument("--session-dir", default="")
    parser.add_argument("--companion", action="append", default=[])
    args, _ = parser.parse_known_args(argv)
    if not args.root or not args.session_dir:
        return 0  # not configured: fail open
    try:
        data = json.loads(sys.stdin.read())
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    message = decide(data, args.root, args.session_dir, args.companion)
    if message is None:
        return 0
    sys.stderr.write(message)
    return 2


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception:
        code = 0  # a broken guard never stalls a write: fail open
    sys.exit(code)
