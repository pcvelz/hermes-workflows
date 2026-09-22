# resilience — wait instead of failing, and always tell a human

A worker that cannot get model capacity should **wait**, not burn three retries
and die. A card killed by the machine should not lose a life. And no card
should ever sit silently blocked or given up.

This mini-app is five stdlib-only modules and one launchd job. Three of them
are libraries the agent's retry loop consults; two of them run on the host and
make the policy real today without forking any upstream file.

The full rationale — the incident, the taxonomy, the invariant, the trade-offs
— is in [`../../docs/resilience.md`](../../docs/resilience.md). This file is
the operator's view: what the files are, how to install them, how to verify.

---

## Files

| File | Purpose |
|------|---------|
| `error_policy.py` | The taxonomy. Splits `FailoverReason` into WAIT-CLASS and REAL, and adds `backend_busy` for local-queue aborts (mid-stream disconnect, request parked behind a model swap) that used to be indistinguishable from a genuine stall. Refines the real agent classifier's answer; never replaces it. |
| `wait_budget.py` | Patience as a duration. `WaitBudget.should_retry()` replaces `retry_count < max_retries` for wait-class failures, and asserts the invariant `model_wait_budget < max_runtime`. |
| `failure_policy.py` | Which outcomes increment the card's failure counter. Conservative defaults (everything counts); `kanban.count_toward_breaker` overrides. |
| `escalation.py` | The communication agent. Renders one message per stuck card and delivers it over `mattermost` \| `telegram` \| `ntfy` \| `none`. Resolves every credential at send time; carries none. |
| `escalator.py` | The host-side reconciler + CLI. Each tick: give back the lives the machine took, then tell a human about everything still stuck. Read-only unless `--apply`. Owns the detection rules — the human-lane and at-capacity suppressions that keep `stranded` meaning "nobody *can* run this", and the `stalled` rule that catches a running card whose worker is alive but idle. |
| `board.py` | Loads and lints `config/board.yaml`: maps each of the user's columns to the database status it lives on, and reports contradictions at load. `python3 board.py config/board.yaml` prints the map and any warnings. |
| `board_cli.py` | The user's two moves out of user_review: `accept` (the **only** path to done), `accept --children <parent>`, and `rework --comment` (back to whoever did the work, with the reason). Refuses to run inside a kanban worker; agents are refused it as a side door. |
| `resilience.yaml.example` | Config template. No secrets — every credential resolves from the environment or the OS keychain. |

Tests: [`../../tests/smoke/test_resilience.py`](../../tests/smoke/test_resilience.py),
run by `bash scripts/test.sh smoke`. They drive the **real** hermes-agent
`kanban_db` and the **real** error classifier in a scratch `HERMES_HOME`, with
the sender stubbed — no test opens a socket.

---

## Install

```bash
# 1. Config.
cp scripts/resilience/resilience.yaml.example "$HERMES_HOME/resilience.yaml"
$EDITOR "$HERMES_HOME/resilience.yaml"     # pick a channel, set channel_id/topic

# 2. Credentials — one of these, matching your channel. Never in the config.
security add-generic-password -s hermes-escalation -a mattermost-token -w '<token>' -U
# or: export HERMES_ESCALATION_TELEGRAM_BOT_TOKEN / _CHAT_ID
# or: reuse the ntfy token your kanban notifier already uses

# 3. Dry run against your board FIRST. Reads only; sends nothing.
python3 scripts/resilience/escalator.py \
  --db "$HERMES_HOME/kanban.db" --board default \
  --config "$HERMES_HOME/resilience.yaml" --json

# 4. Once the report looks right, schedule it.
```

### launchd (macOS)

Use [`../../launchd/com.hermes-workflows.escalator.plist.example`](../../launchd/com.hermes-workflows.escalator.plist.example).

Two rules that are not optional on macOS Tahoe and later:

* **The payload must live under `HERMES_HOME`, never under a documents
  folder.** TCC denies a launchd-context process even *reading* a script there,
  and the job dies with `Operation not permitted` — often invisibly, because a
  manual run works fine. Keep this directory canonical and **copy** it:

  ```bash
  mkdir -p "$HERMES_HOME/launchd-bin"
  cp scripts/resilience/*.py "$HERMES_HOME/launchd-bin/"
  ```
  Re-copy after every edit. Symlinks do not help — following one back re-triggers
  the block.

* **Run it with an absolute, non-shim python.** The agent venv's interpreter
  (`$HERMES_HOME/hermes-agent/venv/bin/python3`) is the safe choice.

Verify the **launchd path itself**, not a manual run:

```bash
launchctl kickstart -k "gui/$(id -u)/com.hermes-workflows.escalator"
tail -20 ~/.hermes/logs/escalator.log     # must show a board=... line
```

---

## Safety

The reconciler writes to a live board, so it is deliberately hard to do by
accident:

| Guard | Effect |
|---|---|
| default read-only | Opens the DB `mode=ro`. Writes only with `--apply`. |
| `--db` required | No "guess the live board" default. |
| dry run is silent | Without `--apply` or `--notify` it uses the null sender: messages are rendered and reported, nothing leaves the box. |
| `HERMES_ESCALATOR_READONLY_BOARDS` | Comma-separated board slugs the reconciler refuses to `--apply` to, even when asked. Use it for a board with a supervised run in flight. |
| cursor held back on failure | An undelivered escalation leaves the cursor before its event, so the next tick retries rather than losing the card. |
| page log | One push per stuck card, never a stream. `stranded`/`stalled` are recomputed each tick, so a JSON page log beside the board DB caps them at one per `(card, kind)` until `repeat_after_seconds` (6h) or a real state change. Only delivered pages are recorded; dry runs record nothing; a corrupt log fails open. |

---

## Verify

```bash
# The suite (real kanban_db, real classifier, scratch HERMES_HOME, stubbed sender)
bash scripts/test.sh smoke

# One escalation end to end, on a scratch board, over your real channel:
python3 scripts/resilience/escalator.py \
  --db /tmp/scratch-board/kanban.db --board default \
  --config "$HERMES_HOME/resilience.yaml" --notify
```

A real message arriving on your phone, carrying a resume command you can paste,
is the proof. A green test run is not.
