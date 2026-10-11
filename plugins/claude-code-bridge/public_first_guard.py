"""Public-first write guard for a bridged session (claude-code-bridge).

Installed as a PreToolUse hook (matcher ``Write|Edit|MultiEdit|NotebookEdit``) when the channel sets
``public_first_guard: true`` with a ``public_first`` block. Arguments, written by lib/session.py at launch:

- ``--private-root``: the private repo (``public_first.private_root``).
- ``--public-root``: the public repo that holds the canonical copy (``public_first.public_root``).

A write to a file under the private root is denied (exit 2) when the same relative path exists under the
public root as a REAL file: not a symlink and not reached through a symlinked directory. That is a diverging
private copy of a public file. The fix the message names: edit the public file and symlink it into the
private repo. A private path that is a symlink (it resolves into the public repo), a private-only path, and a
new path with no public counterpart all pass, and so does any write outside the private root (fail open).

Stdlib only; reads the hook JSON from stdin. It runs from the channel directory and imports nothing from the bridge.
"""

import argparse
import json
import os
import sys

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def decide(data: dict, private_root: str, public_root: str) -> str | None:
    """None to allow; otherwise the block message for stderr."""
    if data.get("tool_name") not in WRITE_TOOLS:
        return None
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or ""
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(data.get("cwd") or os.getcwd(), path)
    private = os.path.realpath(private_root)
    public = os.path.realpath(public_root)
    lex = os.path.normpath(path)
    if under(lex, os.path.normpath(private_root)):
        rel = os.path.relpath(lex, os.path.normpath(private_root))
    elif under(lex, private):
        rel = os.path.relpath(lex, private)
    else:
        return None
    if not under(os.path.realpath(path), private):
        return None  # the write resolves outside the private repo: it lands in the public one (or elsewhere)
    twin = os.path.join(public, rel)
    # A real public file: a regular file, not itself a symlink, reached without a symlinked directory on the way.
    if not (os.path.isfile(twin) and not os.path.islink(twin) and os.path.realpath(twin) == twin):
        return None
    return (
        f"BLOCKED: {lex} also exists as a real file in the public repo ({twin}).\n"
        "Edit the public file and symlink it into the private repo.\n"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--private-root", default="")
    parser.add_argument("--public-root", default="")
    args, _ = parser.parse_known_args(argv)
    if not args.private_root or not args.public_root:
        return 0  # not configured: fail open
    try:
        data = json.loads(sys.stdin.read())
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    message = decide(data, args.private_root, args.public_root)
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
