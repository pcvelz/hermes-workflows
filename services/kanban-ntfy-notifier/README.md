# kanban-ntfy-notifier

Pushes a phone notification (ntfy) whenever any task on the kanban board
reaches a terminal state: `completed`, `blocked`, `gave_up`, `crashed`,
`timed_out` or `protocol_violation`. A host-side poller, decoupled from every
chat gateway, so it needs no bot token and covers every task automatically.

## Architecture

launchd runs `notifier.py` every 60 seconds and at load. It opens the board DB
read-only, sends one ntfy POST per new terminal event, and advances a cursor
file so each event fires exactly once. A failed send rewinds the cursor and
retries on the next tick. The first run seeds the cursor at the current tip, so
history is not replayed.

The ntfy server requires a Bearer token (deny-by-default). The token is read
from the macOS login Keychain, whose item is named by two key files. Keychain
rather than a vault, because launchd has no TTY to answer an unlock prompt.

## Key files

| Key file | Holds |
|----------|-------|
| `keys/kanban-ntfy-notifier/server` | ntfy server base URL |
| `keys/kanban-ntfy-notifier/topic` | the unguessable topic name (the only identity) |
| `keys/kanban-ntfy-notifier/keychain-account` | account name of the Keychain item holding the token |
| `keys/kanban-ntfy-notifier/keychain-service` | service name of the Keychain item holding the token |

Create them with `printf '%s' '<value>' > keys/kanban-ntfy-notifier/<name>`.
Each file is read at runtime and whitespace-stripped. A missing key fails with
a message naming its path. Values are never logged.

Seed the token once:

```bash
/usr/bin/security add-generic-password -a "$(cat keys/kanban-ntfy-notifier/keychain-account)" \
  -s "$(cat keys/kanban-ntfy-notifier/keychain-service)" -w '<token>' -U
```

## Files

| File | Role |
|------|------|
| `notifier.py` | Stdlib poller. Reads the board DB read-only, sends ntfy POSTs, keeps the cursor. |
| `README.md` | This file. |

Environment: `HERMES_HOME` (default `~/.hermes`) locates `kanban.db` and the
cursor. `KANBAN_DB` and `NOTIFIER_CURSOR` override them.

## Install

Through the shared installer (see `docs/services.md`). It copies `notifier.py`
and the key files into `$HERMES_HOME`, then loads the job:

```bash
python3 scripts/install-services.py --only kanban-ntfy-notifier
```

## Verify

```bash
# One-shot run with the real keys (uses the same cursor):
HERMES_HOME="$HOME/.hermes" python3 services/kanban-ntfy-notifier/notifier.py
```

Verify the launchd path, not only the manual run:
`launchctl kickstart -k gui/$(id -u)/<label-prefix>.kanban-ntfy-notifier`, then
read the log under `$HERMES_HOME/logs/`. It must show `sent N notification(s)`
or nothing at all, and never a `403` or `Operation not permitted`.

To re-fire a past event for testing, write `<rowid-1>` into the cursor file and
run once.
