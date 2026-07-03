# Example: the autonomous kanban loop — board → worker → verdict

> **Status: demonstrated end-to-end on a local backend.** Unlike the other
> examples (design targets), the loop walked through here was actually run
> unsupervised: a task placed on the board was **autonomously claimed, executed,
> and completed by a spawned worker process** with no human in the loop. The
> worker created its proof artifact, self-recovered from a guard block, verified
> its own output, wrote a verdict back to the board, and marked the task `done`.
> Every command below is **verbatim what was run** — nothing here is invented.
>
> The demonstration ran against a **local backend at `http://127.0.0.1:8001`**.
> To reproduce it, supply your own Anthropic-Messages (or OpenAI-compatible)
> backend: set `HERMES_LOOP_BASE_URL` / `HERMES_LOOP_MODEL` (or edit
> `model.base_url` / `model.default` in the seed config). The loop shape is
> identical; only the backend changes.

This is the most important thing the scaffold can show you: a real, native
`board → worker → verdict` cycle. You put a task on the kanban board, the
dispatcher claims it and spawns a worker, the worker does the job in an isolated
workspace and writes its verdict back, and you read the result off the board.

The loop runs on the **built-in hermes-agent dispatcher** — no custom code, just
config keys. (`hooks/per-profile-dispatcher/handler.py` is an *optional* custom
dispatcher for when you outgrow the native one; see
[§ When to reach for the custom dispatcher](#when-to-reach-for-the-custom-dispatcher).)

---

## What you need running

- **A backend.** The proof used a local model served at `http://127.0.0.1:8001`
  (llama-swap speaking the Anthropic Messages protocol — free/offline). Any
  Anthropic-Messages- or OpenAI-compatible backend works; set `HERMES_LOOP_BASE_URL`
  and `HERMES_LOOP_MODEL` (or `model.base_url` / `model.default` in the config).
  See [docs/backend.md](../../docs/backend.md).
- **The `hermes` CLI** on your PATH (or reference a venv binary by absolute path,
  as the proof did — see the `$H` variable below). Install per
  [docs/getting-started.md](../../docs/getting-started.md).
- **Nothing else.** For a *single-tick* proof you do **not** need a running
  gateway — `hermes kanban dispatch` is the manual one-pass equivalent and spawns
  the worker directly. A *continuous* unsupervised loop uses `hermes gateway run`
  (or `start`) with the embedded dispatcher (see [§ From one tick to a continuous
  loop](#from-one-tick-to-a-continuous-loop)).

> **`hermes kanban daemon` is DEPRECATED** — the dispatcher now lives in the
> gateway. Use `hermes kanban dispatch` for a one-shot tick, or the gateway for a
> continuous loop.

---

## Files in this example

| File | What it is |
|------|------------|
| `run-loop-proof.sh` | **Entrypoint.** Runs the exact proven loop in an isolated throwaway `HERMES_HOME` (never touches your live `~/.hermes`): seed config → init board → add the `loop-proof` task → dispatch → show the verdict → clean up. Start here. |
| `config.yaml.example` | The minimal seed config the proof used — local backend, `approvals.mode: never`, manual single-pass dispatch. Copy + edit `base_url`/`default` for your backend. |
| `jobs.json.example` | A **real-shaped** task template (cron/seed shape): "tail CI pipeline `<ID>` to a terminal state and write a SUCCESS/FAILED verdict with root cause." Generic — bring your own CI host/IDs. This is the shape you grow into once the trivial proof works. |

---

## The trivial proof — run it yourself

This is the **same loop that was proven**. It is self-contained and safe: it
builds an isolated `HERMES_HOME` with `mktemp -d`, runs entirely there, and
removes it at the end. Your live install at `~/.hermes` is never touched.

`HERMES_LOOP_MODEL` and `HERMES_LOOP_BASE_URL` are **required** — the script
exits with a friendly guide if they are not set:

```bash
HERMES_LOOP_MODEL=your-dotless-alias \
HERMES_LOOP_BASE_URL=http://your-backend:port \
  bash examples/autonomous-loop/run-loop-proof.sh
```

> **Model id must be DOTLESS.** hermes-agent normalises `.` → `-` in model ids
> before sending the request (e.g. `Foo3.6-Bar` → `Foo3-6-Bar` on the wire).
> Proxies (llama-swap, etc.) return HTTP 404 if the normalised form has no alias.
> Use a dotless id your backend actually exposes. See [docs/backend.md](../../docs/backend.md).

What it does, step by step (these are the verbatim commands that were run):

```bash
# 0. Isolated, safe HERMES_HOME (never touches your live ~/.hermes)
TMP_HOME=$(mktemp -d "${TMPDIR:-/tmp}/hermes-loop.XXXXXX")
export HERMES_HOME="$TMP_HOME"
H=hermes                                  # or an absolute venv path: /path/to/venv/bin/hermes

# 1. Seed a minimal config pointed at your backend (see config.yaml.example)
cp examples/autonomous-loop/config.yaml.example "$TMP_HOME/config.yaml"
#    edit model.base_url / model.default if your backend differs

# 2. Sanity one-shot (proves model + isolated home work). Prints: OK
"$H" -m "<your-model>" --ignore-user-config -z "say OK"

# 3. Initialize the board
"$H" kanban init

# 4. Add the task (assigned tasks land directly in `ready`)
WS="$TMP_HOME/proof-workspace"; mkdir -p "$WS"
"$H" kanban create "Create loop-proof file" \
  --body "Create a file named loop-proof.txt in your current working directory containing exactly the text LOOP_OK and nothing else. Then mark this task done." \
  --assignee default \
  --workspace "dir:$WS"
# (If a task lands in `todo` instead of `ready`, promote it: hermes kanban promote <task_id>)

# 5. Run the dispatcher — ONE pass: reclaims stale, promotes ready, SPAWNS the worker.
#    This is the autonomous tick. It detaches the worker and returns immediately.
"$H" kanban dispatch                      # use --dry-run first to preview spawns

# 6. Watch the transition + read the verdict
"$H" kanban list                          # ready -> running -> done
"$H" kanban show <task_id>                # status, events, run history, worker summary
"$H" kanban log <task_id>                 # full agentic trace of what the worker did

# 7. Cleanup
rm -rf "$TMP_HOME"
```

### What "it worked" looks like

**State transitions** (from `hermes kanban show`):

```
[created]   {assignee: default, status: ready}
[run 1] claimed   {lock: <host>:<pid>, run_id: 1}
[run 1] spawned   {pid: <worker_pid>}          <- worker process actually spawned
[run 1] heartbeat
[run 1] completed {summary: "Created loop-proof.txt ... exactly \"LOOP_OK\" ..."}
status: done
```

**The artifact** (`od -c "$WS/loop-proof.txt"`) — proves a worker did real file I/O:

```
0000000    L   O   O   P   _   O   K
0000007                                  # 7 bytes, no trailing newline — exact
```

**The worker's own verdict**, written back to the board:

> "Created loop-proof.txt in the proof-workspace directory containing exactly
> "LOOP_OK" with no trailing newline or extra content."

**It is genuinely agentic, not a scripted happy path.** The proven
`hermes kanban log` trace showed the worker *self-recover*:

1. `mcp__kanban_show` — worker read its own task.
2. `mcp__write_file` → **blocked** by a guard ("Refusing to write to sensitive
   system path").
3. Worker reasoned: *"The write was blocked by a guard. Let me use the terminal
   directly"* → `printf 'LOOP_OK' > .../loop-proof.txt`.
4. Self-verified: `xxd ... && wc -c ...`.
5. `mcp__kanban_complete` — marked itself done.

### Timing / cold-start

One dispatch tick → worker spawn → file write → self-verify → complete took
**~65 s wall** on a local 35B MoE. Budget more for heavier tasks; a hosted API
is faster on cold start but the loop shape is identical.

---

## From one tick to a continuous loop

`hermes kanban dispatch` is **one tick**, by hand. For an unsupervised loop that
keeps claiming `ready` tasks on its own, run the gateway with the **embedded
dispatcher** — these are the proven config keys (see `config.yaml.example` and
[docs/operating.md § Running the autonomous loop](../../docs/operating.md#running-the-autonomous-loop)):

```yaml
kanban:
  dispatch_in_gateway: true          # gateway runs the dispatcher itself
  dispatch_interval_seconds: 60      # tick cadence (default 60s)
approvals:
  mode: never                        # autonomous: no approval prompts
```

```bash
hermes gateway run                   # foreground; ticks every dispatch_interval_seconds
# or as a background service:
hermes gateway install && hermes gateway start
```

> For the single-tick proof above, `config.yaml.example` ships with
> `dispatch_in_gateway: false` so `hermes kanban dispatch` performs the manual
> one-pass tick without a gateway. Flip it to `true` (and start the gateway) for
> the continuous loop.

> **Safety:** `approvals.mode: never` is what makes the worker run without
> prompts — appropriate for a trivial sandboxed proof. For real autonomous work,
> read [docs/security-hardening.md](../../docs/security-hardening.md) and prefer
> `approvals.mode: manual` + `cron_mode: deny` until you trust the loop.

---

## The real-shaped task: tail a CI pipeline to a verdict

The `loop-proof` task is deliberately trivial. `jobs.json.example` shows the
shape you actually grow into: **tail a CI pipeline to a terminal state and write
a SUCCESS/FAILED verdict with a root cause.** It is generic — bring your own CI
host and pipeline ID:

```bash
# Seed the same kind of task onto the board (substitute your own values):
WS="$TMP_HOME/ci-tail-workspace"; mkdir -p "$WS"
hermes kanban create "Tail CI pipeline ${PIPELINE_ID} to verdict" \
  --body "$(jq -r '.jobs[0].prompt' examples/autonomous-loop/jobs.json.example)" \
  --assignee default \
  --workspace "dir:$WS"
hermes kanban dispatch
hermes kanban show <task_id>          # read the SUCCESS/FAILED verdict + root cause
```

> **Offline / no-CI note.** Tailing a *live* pipeline needs a reachable CI host
> and a valid pipeline ID + token — none of which ship in this repo. With no CI
> available, the loop mechanics (claim → spawn → verdict → done) are still fully
> exercised by `run-loop-proof.sh` above; the CI task is what you point at real
> infrastructure once the trivial loop works on your machine. Inject the CI token
> via your secrets layer (see [docs/secrets.md](../../docs/secrets.md)) — never
> inline it in the task body.

---

## When to reach for the custom dispatcher

The native dispatcher (above) is the **primary, proven path** and is enough for
most setups: promote → claim → spawn → reap, driven entirely by config.

The optional `hooks/per-profile-dispatcher/handler.py` is for when you **outgrow**
the native one and need custom orchestration logic — per-profile cooldowns,
bespoke fast-retry / failure-escalation windows, a custom "notify only on state
change" channel, or dispatch rules the native dispatcher does not express. It is
the external-dispatcher pattern: you bind its adapter functions
(`fetch_tasks`, `dispatch_task`, …) to the same `hermes kanban` commands proven
here. Enable **either** the native dispatcher **or** the custom one — never both
against the same `HERMES_HOME`. See that file's header for the adapter contract.

---

## See also

- [docs/operating.md § Running the autonomous loop](../../docs/operating.md#running-the-autonomous-loop) — the verified commands in the operations guide.
- [docs/backend.md](../../docs/backend.md) — point the loop at your backend (local or hosted).
- [docs/security-hardening.md](../../docs/security-hardening.md) — approval policy before letting it run unattended.
- [hooks/per-profile-dispatcher/handler.py](../../hooks/per-profile-dispatcher/handler.py) — the optional custom dispatcher.
