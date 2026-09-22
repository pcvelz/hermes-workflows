"""file-read — reading without writing: the `file_read` toolset.

Upstream's `file` toolset grants read_file, write_file, patch and search_files as
one thing, so a role that must not edit (the planner) could only be given no file
tools at all, and lost reading with them. This plugin registers ONE tool,
`file_read`, in its own toolset of the same name. It can open, list and search.
It has no code path that writes, creates, moves or deletes anything: the limit is
the absence of the verb (docs/enforcement.md), not an instruction.

Fail closed:
  * an `op` other than exactly "open", "list" or "search" is refused;
  * a file is opened (or searched) only when every signal agrees it is a
    readable format; where name, mode bit and content disagree, it is refused.

There is no mode, flag or env var that turns a refusal into a warning.
"""
import fnmatch
import json
import os
import re
import stat
from pathlib import Path

TOOLSET = "file_read"
TOOL = "file_read"
OPS = ("open", "list", "search")

# What the grant covers: the formats agents plan and communicate in.
PLANNING_FORMATS = {
    ".md", ".markdown", ".txt",
    ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv",
    ".log",
}

# Source code that is neither a script nor a planning format. Allowed to be READ:
# a planner that cannot see the code can only cut work someone else already
# understood, and its prompt tells it to read code to size the work. It still
# cannot change a byte of it -- this tool has no write path. A named list, not
# "whatever is not refused"; add to it deliberately.
SOURCE_FORMATS = {
    ".php", ".phtml", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".java", ".kt", ".cs", ".swift", ".scala",
    ".html", ".htm", ".xml", ".css", ".scss", ".less",
    ".twig", ".tpl", ".mustache", ".hbs", ".j2", ".jinja",
    ".graphql", ".proto", ".ini", ".cfg", ".conf", ".env.example",
}

# Refused by name: executable and script files. A planner does not need to read a
# script to cut work, and every script it can read is a script it can quote
# verbatim into a card for someone else to run. That is not writing, and it is not
# really reading either. The executable bit and a `#!` first line are refused the
# same way whatever the file is called (see _classify).
SCRIPT_FORMATS = {
    ".sh", ".bash", ".zsh", ".fish", ".ksh", ".csh",
    ".py", ".pyw", ".rb", ".pl", ".pm", ".js", ".mjs", ".cjs", ".ts", ".tsx",
    ".jsx", ".lua", ".tcl", ".ps1", ".bat", ".cmd", ".command", ".applescript",
    ".scpt", ".exe", ".bin", ".so", ".dylib", ".app",
}

# Extensionless names that are programs all the same.
SCRIPT_NAMES = {
    ".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zprofile",
    ".zshenv", ".zlogin", ".kshrc", ".cshrc", ".tcshrc",
    "makefile", "gnumakefile", "justfile", "rakefile", "gemfile", "procfile",
}

READABLE = PLANNING_FORMATS | SOURCE_FORMATS

MAX_BYTES = 2_000_000
MAX_LINES = 2000
MAX_RESULTS = 200
SKIP_DIRS = {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv"}

SCHEMA = {
    "name": TOOL,
    "description": (
        "Read files without changing them. op=open reads a file's lines; "
        "op=list lists file names under a directory (optionally filtered by a glob "
        "pattern); op=search finds lines matching a regex in readable files under a "
        "path. Reads markdown, text, json/yaml/toml/csv/tsv, logs and named source "
        "formats. Refuses scripts and executables (by extension, executable bit or "
        "#! line). This tool cannot write, create, move or delete anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": list(OPS),
                   "description": "open | list | search"},
            "path": {"type": "string",
                     "description": "File (open) or directory/file (list, search)."},
            "pattern": {"type": "string",
                        "description": "list: glob on the file name. search: regex."},
            "offset": {"type": "integer", "default": 1, "minimum": 1,
                       "description": "open: first line, 1-indexed."},
            "limit": {"type": "integer", "default": 500, "maximum": MAX_LINES,
                      "description": "open: max lines. list/search: max results."},
        },
        "required": ["op", "path"],
    },
}


def _refuse(reason):
    return json.dumps({"error": f"file_read refused: {reason}", "success": False})


def _ext(name):
    name = name.lower()
    if name.endswith(".env.example"):
        return ".env.example"
    return os.path.splitext(name)[1]


def _classify(path):
    """Return None when `path` may be read, else the reason it may not.

    Name, mode and content must all agree. The extension is a hint anyone can
    change, so it is never enough on its own.
    """
    try:
        real = Path(path).resolve(strict=True)
        st = real.stat()
    except (OSError, RuntimeError) as exc:
        return f"cannot stat {path}: {exc.__class__.__name__}"
    if not stat.S_ISREG(st.st_mode):
        return f"{path} is not a regular file"
    if st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return f"{path} has the executable bit set; executables are not readable"
    # Both the name asked for and the file it resolves to (a symlink named
    # notes.md pointing at deploy.sh is the script, not the note).
    for name in {Path(path).name, real.name}:
        ext = _ext(name)
        if name.lower() in SCRIPT_NAMES:
            return f"{name} is a script; scripts are not readable"
        if ext in SCRIPT_FORMATS:
            return f"{name} is a script ({ext}); scripts are not readable"
        if ext and ext not in READABLE:
            return f"{name}: format {ext} is not on the readable list"
    if st.st_size > MAX_BYTES:
        return f"{path} is larger than {MAX_BYTES} bytes"
    try:
        with open(real, "rb") as fh:
            head = fh.read(8192)
    except OSError as exc:
        return f"cannot open {path}: {exc.__class__.__name__}"
    if head.startswith(b"#!"):
        return f"{path} starts with #!; it is a script whatever it is called"
    if b"\x00" in head:
        return f"{path} is binary"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 4:  # a multibyte char cut at the 8 KiB edge is fine
            return f"{path} is not UTF-8 text"
    return None


def _int(value, default, lo, hi):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer, got {value!r}")
    return max(lo, min(hi, value))


def _open(args):
    path = os.path.expanduser(str(args.get("path") or ""))
    if not path:
        return _refuse("path is required")
    why = _classify(path)
    if why:
        return _refuse(why)
    offset = _int(args.get("offset"), 1, 1, 10**9)
    limit = _int(args.get("limit"), 500, 1, MAX_LINES)
    with open(Path(path).resolve(), encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    body = "\n".join(f"{offset + i}|{line}" for i, line in enumerate(chunk))
    return json.dumps({"path": path, "content": body, "total_lines": len(lines),
                       "truncated": offset - 1 + limit < len(lines)})


def _walk(root):
    root = Path(root)
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _list(args):
    path = os.path.expanduser(str(args.get("path") or "."))
    if not os.path.isdir(path):
        return _refuse(f"{path} is not a directory")
    pattern = args.get("pattern") or "*"
    limit = _int(args.get("limit"), MAX_RESULTS, 1, MAX_RESULTS)
    # Names only. A script's name is not its program; its content stays closed.
    files = []
    for p in _walk(path):
        if fnmatch.fnmatch(p.name, str(pattern)):
            files.append(str(p))
            if len(files) >= limit:
                break
    return json.dumps({"path": path, "files": files, "truncated": len(files) >= limit})


def _search(args):
    path = os.path.expanduser(str(args.get("path") or "."))
    if not os.path.exists(path):
        return _refuse(f"{path} does not exist")
    try:
        rx = re.compile(str(args.get("pattern") or ""))
    except re.error as exc:
        return _refuse(f"bad regex: {exc}")
    if not rx.pattern:
        return _refuse("pattern is required")
    limit = _int(args.get("limit"), MAX_RESULTS, 1, MAX_RESULTS)
    matches, skipped = [], 0
    for p in _walk(path):
        if _classify(str(p)):
            skipped += 1  # never searched: same rule as open
            continue
        with open(p.resolve(), encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if rx.search(line):
                    matches.append(f"{p}:{n}:{line.rstrip()}")
                    if len(matches) >= limit:
                        return json.dumps({"matches": matches, "truncated": True,
                                           "unreadable_skipped": skipped})
    return json.dumps({"matches": matches, "truncated": False,
                       "unreadable_skipped": skipped})


_HANDLERS = {"open": _open, "list": _list, "search": _search}


def _handle(args, **_kw):
    op = args.get("op") if isinstance(args, dict) else None
    # Exact match only: no case folding, no trimming. An op this tool cannot
    # classify is refused, never guessed at.
    if not isinstance(op, str) or op not in _HANDLERS:
        return _refuse(f"op {op!r} is not one of {', '.join(OPS)}; this tool only reads")
    try:
        return _HANDLERS[op](args)
    except Exception as exc:
        return _refuse(f"{exc.__class__.__name__}: {exc}")


def register(ctx):
    ctx.register_tool(
        name=TOOL,
        toolset=TOOLSET,
        schema=SCHEMA,
        handler=_handle,
        description=SCHEMA["description"],
        emoji="📖",
    )
