#!/usr/bin/env python3
"""Kanban terminal-event -> ntfy push notifier.

Polls the kanban board DB (read-only) for terminal task events and POSTs a
one-line notification to an ntfy topic. Runs from launchd on an interval; a
cursor file makes each event fire exactly once. Fully decoupled from any chat
gateway: no platform adapter, no bot token.

Account-bound values come from key files (see services/_shared/keyfile.py):
  server            keys/kanban-ntfy-notifier/server
  topic             keys/kanban-ntfy-notifier/topic
  keychain account  keychain-account  (macOS login Keychain item)
  keychain service  keychain-service  (macOS login Keychain item)
Each can be redirected with KEY_<NAME> (KEY_SERVER, KEY_TOPIC, ...). The
installer sets those from services/services.yaml.

Privacy: the unguessable topic is the only identity; the payload carries the
task id, title and first line of the run summary only. Values are never logged.
"""
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "_shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from keyfile import KeyFileError, read_key_env  # noqa: E402

HERMES_HOME = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser("~/.hermes")
DB = os.environ.get("KANBAN_DB", os.path.join(HERMES_HOME, "kanban.db"))
CURSOR = os.environ.get(
    "NOTIFIER_CURSOR", os.path.join(HERMES_HOME, "kanban", ".ntfy_notifier_cursor")
)

TERMINAL = {"completed", "blocked", "gave_up", "crashed", "timed_out", "protocol_violation"}
EMOJI = {
    "completed": "✔",
    "blocked": "⏸",
    "gave_up": "✖",
    "crashed": "✖",
    "timed_out": "⏱",
    "protocol_violation": "⚠",
}


def read_cursor(conn):
    try:
        return int(open(CURSOR).read().strip())
    except (OSError, ValueError):
        # First run: start at the current tip so history is not replayed.
        tip = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM task_events").fetchone()[0]
        write_cursor(tip)
        return tip


def write_cursor(value):
    os.makedirs(os.path.dirname(CURSOR), exist_ok=True)
    tmp = CURSOR + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(value))
    os.replace(tmp, CURSOR)


def first_line(payload):
    try:
        data = json.loads(payload) if payload else {}
    except ValueError:
        return ""
    for key in ("summary", "error", "reason"):
        val = data.get(key)
        if val:
            return str(val).splitlines()[0][:200]
    return ""


def ntfy_token(account, service):
    """Access token from the login Keychain.

    The self-hosted server denies by default, so publishing without a token is
    a 403. The Keychain is used rather than a vault because launchd has no TTY
    and would hang on an unlock prompt. The item's name is the key file pair.
    """
    proc = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeError("ntfy token not found in the Keychain item named by the "
                           "keychain-account and keychain-service keys")
    return proc.stdout.strip()


def notify(server, topic, account, service, title, body):
    req = urllib.request.Request(
        f"{server}/{topic}",
        data=body.encode(),
        headers={
            "Title": title.encode("ascii", "ignore").decode(),
            "Authorization": f"Bearer {ntfy_token(account, service)}",
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def load_keys():
    """Read every account-bound value up front, so a missing key fails once, by name."""
    return {
        "server": read_key_env("KEY_SERVER", "keys/kanban-ntfy-notifier/server").rstrip("/"),
        "topic": read_key_env("KEY_TOPIC", "keys/kanban-ntfy-notifier/topic"),
        "account": read_key_env("KEY_KEYCHAIN_ACCOUNT", "keys/kanban-ntfy-notifier/keychain-account"),
        "service": read_key_env("KEY_KEYCHAIN_SERVICE", "keys/kanban-ntfy-notifier/keychain-service"),
    }


def main():
    try:
        keys = load_keys()
    except KeyFileError as exc:
        print(f"kanban-ntfy-notifier: {exc}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cursor = read_cursor(conn)
    rows = conn.execute(
        "SELECT e.rowid AS rid, e.task_id, e.kind, e.payload, t.title "
        "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
        "WHERE e.rowid > ? ORDER BY e.rowid",
        (cursor,),
    ).fetchall()
    sent = 0
    last = cursor
    for row in rows:
        last = row["rid"]
        if row["kind"] not in TERMINAL:
            continue
        mark = EMOJI.get(row["kind"], "")
        title = f"{mark} kanban {row['task_id']} {row['kind']}"
        body = (row["title"] or "(no title)")[:150]
        extra = first_line(row["payload"])
        if extra:
            body += f"\n{extra}"
        try:
            notify(keys["server"], keys["topic"], keys["account"], keys["service"], title, body)
            sent += 1
        except Exception as exc:  # leave the cursor before this event -> retry next tick
            # Exception type only: the message can carry the endpoint.
            print(f"send failed at rowid {row['rid']}: {type(exc).__name__}", file=sys.stderr)
            write_cursor(row["rid"] - 1)
            return 1
    write_cursor(last)
    if sent:
        print(f"sent {sent} notification(s), cursor -> {last}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
