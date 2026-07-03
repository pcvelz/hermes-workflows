# Operating Hermes — How You Actually Run, Watch, and Steer It

This is the day-to-day walkthrough: **how do I run it, how do I look into what it's
doing, how do I tell whether it found real problems, and how do I interrupt or
redirect it.** It answers those questions in order, with the real `hermes` commands
inline. Every command below was verified against the installed `hermes-agent` CLI
(`hermes <cmd> --help`); none are invented.

> **First time?** Install + backend config + smoke test live in
> [getting-started.md](getting-started.md). Sandbox / secure-run options live in
> [security-hardening.md](security-hardening.md). This document assumes you already
> have a working `hermes` on your PATH and a populated `HERMES_HOME` (default `~/.hermes`).

> **One honest caveat up front (read this before §3).** Two things people expect — a
> *live cockpit GUI* and a *synchronous control endpoint you can script* — are **not in
> this repository**. They come from a separate **Hermes Desktop** app. With only this repo
> + the `hermes-agent` CLI, your cockpit is **logs + sessions + the kanban board**, and
> that is genuinely enough to run, watch, vet, and stop the agent. Desktop is an optional
> upgrade, covered last (§7).

---

## 1. Start it — gateway vs one-shot

There are two distinct ways to "run Hermes", and which one you want depends on whether
you want it to *keep running on its own* or just *answer one thing*.

### 1a. One-shot or interactive chat — "answer this, then stop"

If you just want to ask it something — including running a skill like the HA
health-check — you do **not** need the gateway at all. A single invocation starts a
fresh session, runs one turn, persists the transcript to `state.db`, and exits:

```bash
# One-shot, programmatic (quiet — only the final answer + session id are printed)
hermes chat -q "Summarise today's errors.log and tell me what's actionable" -Q

# One-shot shorthand (same idea, top-level flag)
hermes -z "Summarise today's errors.log and tell me what's actionable"

# Interactive — babysit it turn by turn in a terminal UI
hermes chat                 # TUI (default)
hermes chat --cli           # plain-text, no TUI (good for tmux / piping)
```

`-q "<query>"` is the single-query (non-interactive) flag; `-Q` is *quiet* mode for
scripts (suppresses the banner, spinner, and tool previews — you get the final response
and the session id only). This is exactly the mode the private reference HA-healthcheck
dog-food run used: a clean one-shot against a local model, one API call,
`finish_reason=stop`, then exit — **no gateway, no background daemon, no kanban activity**.
(That run record, `dogfood-obs.json`, is an artifact of the private reference deployment
and does not ship in this repo — its HA workflow is a design target here; see the §3
callout.)

Conversations are **discrete, resumable sessions** — not one eternal chat. Continue one
instead of starting fresh:

```bash
hermes chat --resume <session-id>     # -r — thread back into a specific session
hermes chat --continue                # -c — the most-recent session
hermes chat --continue <name>         # a named session
hermes sessions browse                # interactive picker — search + resume
```

### 1b. The gateway — "keep running and work on its own"

The gateway is the **always-on autonomous process**. Start it only when you want Hermes
to keep working without you sitting in front of it. It does three things:

- listens on any configured chat platforms (Telegram / Mattermost / Matrix) and routes
  incoming messages to the right profile's agent loop;
- runs the **kanban dispatcher** (default every 60 s) — claiming *ready* tasks off the
  board and executing each in an isolated workspace;
- runs the **cron scheduler**.

```bash
# Foreground (dev, containers, debugging — you watch it in this terminal)
hermes gateway run
hermes gateway run --replace          # take over a stale instance
hermes gateway run -v                 # verbose; -vv = very verbose

# Background service (the normal way to leave it running)
hermes gateway install                # registers launchd (macOS) / systemd (Linux)
hermes gateway start
hermes gateway stop
hermes gateway restart
```

> **One gateway per profile.** `gateway.pid` / `gateway.lock` enforce a single instance
> per `HERMES_HOME`. Starting a second gateway against the same home corrupts shared
> state — use `--replace` / `--force` to take over a stale lock, never start a second
> one alongside the first.

**Is it actually up and healthy?** Two commands, plus one file:

```bash
hermes gateway status                 # running state per profile
hermes status --deep                  # full component health: provider, model, gateway
cat "$HERMES_HOME/gateway_state.json" # raw truth: pid, argv, "gateway_state": "running"
```

A healthy gateway shows `running` in `gateway status`, your model/provider marked OK in
`status --deep`, and `gateway_state.json` carries a live `pid`, the launch `argv`, and
`"gateway_state": "running"`.

> **Illustrative, not reproducible here.** The concrete values quoted below — and the
> Z-Wave / recorder / HACS findings in §3 — come from a **private reference deployment**,
> shown so you know what real output looks like. They are **not** a measured run of this
> scaffold (its HA workflow is a design target — see
> [`examples/home-assistant-healthcheck/README.md`](../examples/home-assistant-healthcheck/README.md)).
> Your own pids, argv, and findings will differ.

On that reference install the file reads pid `1041`, argv `... gateway run --replace`,
state `running`.

> **"No messaging platforms enabled" is NOT a fault.** A gateway started on the `default`
> profile logs exactly that line, because **platforms are configured per-profile, not on
> `default`**. If you expect a Telegram/Mattermost thread and the gateway is silent, check
> which profile is active before assuming it's broken — see the profile note in §3.

---

## 2. Watch it think — and catch errors live

Once something is running (a one-shot turn, an interactive TUI, or the autonomous
gateway), the **logs** are where you watch it reason and where errors surface first.
`hermes logs` is a unified viewer/tailer over `~/.hermes/logs/` (`agent.log`,
`gateway.log`, `errors.log`, …):

```bash
hermes logs -f                        # follow agent.log — the everyday "watch it think" view
hermes logs gateway -f                # follow gateway.log (lifecycle, dispatcher, cron fires)
hermes logs -f --component agent      # only agent-loop lines
hermes logs -f --component cron       # only cron-scheduler lines
hermes logs -f --component gateway    # only gateway lines
hermes logs --session <id>            # everything for one session id
hermes logs --since 1h                # last hour only
hermes logs --since 30m -f            # follow, starting 30 min back
```

`--component` accepts exactly one of `gateway`, `agent`, `tools`, `cli`, `cron`, `gui`
(verified in `hermes logs --help`). In `agent.log` you'll see the lines that tell you it
is alive and working — e.g. `agent.turn_context: ... model=… provider=… platform=cli`,
`agent.conversation_loop: API call #1: ... latency=…`, and the turn's end:
`Turn ended: reason=text_response(finish_reason=stop) api_calls=1/60`.

**To catch errors specifically, filter by level — this is your error tripwire:**

```bash
hermes logs --level WARNING           # warnings and above
hermes logs --level ERROR             # errors only
hermes logs errors                    # the dedicated errors.log
```

If `errors.log` is empty and `--level WARNING` is quiet, the run hit no internal faults.
(Note: an empty `errors.log` means *Hermes* hit no errors — it says nothing about
whether the agent's *analysis* found problems in whatever it was inspecting. That's §3.)

> **Local-LLM self-report caveat.** If you run the agent on a local model in a sandboxed
> HOME, it can mis-report its own environment (wrong `~` paths). Treat anything the agent
> *says about its own state* as unreliable and verify against the real files / logs above
> rather than asking it. (Background: observed in the private reference deployment's run
> record, `dogfood-obs.json → artifactOmissions §5` — that artifact is not shipped here.)

---

## 3. Read what it did — and vet whether it found real problems

This is the question people keep asking: *the agent ran — did it find actual errors, did
it stay safe, or did it hallucinate?* The answer lives in the **session transcript**.
Every turn (one-shot, TUI, platform, Desktop) is a row in `state.db` with full-text
search. The workflow is: list → pick the id → read the transcript → judge it.

```bash
# 1. Find the run
hermes sessions list                          # newest first
hermes sessions list --limit 5                # just the last few
hermes sessions list --source cli             # filter by origin (cli, telegram, …)

# 2. Read the full transcript of the run you care about (JSONL; '-' = stdout)
hermes sessions export --session-id <session-id> -

# 3. Cross-reference the log side of the same run
hermes logs --session <session-id>
```

> **Correct export syntax:** `hermes sessions export` takes an **output path** (use `-`
> for stdout) and the id via `--session-id` — i.e. `hermes sessions export --session-id
> <id> -`, *not* `sessions export <id>`. Verified in `hermes sessions export --help`.

### Is this a good run or a bad run? A concrete rubric

Read the transcript and check, in order. (The concrete Z-Wave / recorder / HACS findings
quoted below are **illustrative output from a private reference deployment**, included to
show what a good transcript looks like — **not** a run reproducible against this scaffold,
whose HA workflow is a design target.)

1. **Did it actually find the problems, with evidence?** A good run cites the raw signal —
   specific log lines, entity ids, counts — not vague claims. In the reference HA dog-food
   run the agent correctly root-caused a Z-Wave JS disconnect at 09:14 as the cause of
   three stuck Z-Wave entities (P1), correlated a recorder DB backlog with a
   `boiler_pressure` template warning (P2), and flagged a HACS GitHub 403 (P2) — each
   anchored to a line in the error log it was given.
2. **Did it suppress known noise instead of crying wolf?** The same run *correctly
   suppressed* two known-noisy items (a kitchen-speaker warning and a zigbee2mqtt
   unsubscribed-topic warning) per the memory's suppression list. A run that re-flags
   known noise as new problems is over-reporting.
3. **Did it stay within bounds?** For a read-only job, the transcript should show it
   *recommended* fixes and *performed none* — GET-only. If you see it attempting writes /
   deploys on a read-only task, that's the failure mode to catch.
4. **Did it hallucinate?** Anything in the conclusion that isn't traceable to data in the
   transcript (an error it never saw, a device that wasn't in the input) is a
   hallucination — discount the whole run and re-verify against the real source.

You can also search across *all* past sessions when you don't know which run it was —
`state.db` carries FTS5 indexes:

```bash
sqlite3 -readonly "$HERMES_HOME/state.db" \
  "SELECT session_id, snippet(messages_fts,0,'>>>','<<<','…',20) \
   FROM messages_fts WHERE messages_fts MATCH 'zwave OR recorder' LIMIT 20;"
hermes sessions stats                         # totals, token counts, date ranges
```

> **Profile-aware caveat.** *What you can watch depends on the active profile.* Each
> profile is an isolated instance with its own `state.db`, board, and platform wiring
> (e.g. one profile streams to Telegram, another to Mattermost, `default` to no
> platform). If a session list or a platform thread looks empty, you may be looking at
> the wrong profile — check and switch first:
>
> ```bash
> hermes profile list                          # all profiles + which is active
> cat "$HERMES_HOME/active_profile"            # the sticky default (plain text)
> hermes profile use <name>                    # change the default
> ```

---

## 4. The kanban board — what is queued, in-flight, and done

The logs and sessions tell you about *individual turns*. The **kanban board**
(`kanban.db`) is how the autonomous side is *managed*: it's the durable backlog the
gateway dispatcher pulls from. Reading the board answers "what is it about to do, what is
it doing right now, and what did it finish?"

```bash
hermes kanban list                            # the whole board
hermes kanban list --status running           # what is in-flight right now
hermes kanban list --status ready             # queued, waiting for the dispatcher
hermes kanban list --status done              # finished
hermes kanban list --status blocked           # stuck / waiting on a human
hermes kanban show <task-id>                  # one task: comments + full event history
hermes kanban stats                           # counts per status
```

The `--status` values *are* the board columns:
`triage, todo, ready, running, review, blocked, scheduled, done, archived` (verified in
`hermes kanban list --help`). Ready tasks get auto-claimed and executed by the gateway
within `dispatch_interval_seconds` (default 60) when `kanban.dispatch_in_gateway: true`.

**To watch autonomous work happen live — and catch failures as they occur:**

```bash
hermes kanban watch                           # live task_events: claimed → running → completed
hermes kanban watch --kinds completed,blocked,gave_up,crashed,timed_out   # only outcomes/failures
hermes kanban tail <task-id>                  # follow one task's event stream
```

`kanban watch --kinds gave_up,crashed,timed_out` is your **failure tripwire for
autonomous work** — those event kinds are exactly how a runaway or broken task surfaces
on the board.

---

## 4a. Running the autonomous loop

This is the payoff: a task on the board gets **claimed, executed, and verdicted by a
spawned worker with no human in the loop** — `board → worker → verdict → done`. It was
demonstrated end-to-end against a local backend (the model at
`http://127.0.0.1:8001`, which the seed config hardcodes); the worker created its proof
artifact, self-recovered from a guard block, verified its own output, wrote a summary back
to the board, and marked the task `done`. **To reproduce, point
`HERMES_LOOP_BASE_URL` / `HERMES_LOOP_MODEL` (or `model.base_url` / `model.default`) at
your own Anthropic-Messages endpoint** — the loop shape is identical, only the backend
changes. The full walkthrough — with the verbatim demonstrated commands, a trivial example
you can run yourself, and a real-shaped CI-tail task — lives in
[`examples/autonomous-loop/`](../examples/autonomous-loop/README.md).

**The single-tick proof — no gateway needed.** `hermes kanban dispatch` is the manual
one-pass equivalent of the gateway's dispatcher: one tick that reclaims stale claims,
promotes ready tasks, and **spawns a worker** for each ready task, then returns. This is
the autonomous tick, by hand:

```bash
hermes kanban init                            # initialize the board
hermes kanban create "<title>" \
  --body "<the task — end it with: then mark this task done>" \
  --assignee default \
  --workspace "dir:<workspace>"               # assigned tasks land directly in `ready`
hermes kanban dispatch --dry-run              # preview which workers WOULD spawn
hermes kanban dispatch                        # ONE tick — spawns the worker, returns
hermes kanban list                            # watch ready -> running -> done
hermes kanban show <task-id>                  # status, events, run history, worker verdict
hermes kanban log <task-id>                   # full agentic trace of what the worker did
```

> `hermes kanban daemon` is **DEPRECATED** — the dispatcher now lives in the gateway. Use
> `hermes kanban dispatch` for one tick, or the gateway (below) for a continuous loop.

**The continuous, unsupervised loop — the gateway's embedded dispatcher.** Flip on the
in-gateway dispatcher and run the gateway; it then ticks on its own every
`dispatch_interval_seconds`, claiming `ready` tasks as they appear:

```yaml
kanban:
  dispatch_in_gateway: true          # gateway runs the dispatcher itself
  dispatch_interval_seconds: 60      # tick cadence (default 60s)
approvals:
  mode: never                        # autonomous: no approval prompts (proof setting)
```

```bash
hermes gateway run                            # foreground; or: gateway install && gateway start
```

**What must be running / honest caveats:**

- **A backend.** The proof used a local model at `http://127.0.0.1:8001` (free,
  offline). Any Anthropic-Messages- or OpenAI-compatible endpoint works — set
  `model.base_url` / `model.provider`. See [backend.md](backend.md).
- **Cold start.** One tick → spawn → file write → self-verify → complete took **~65 s**
  wall on a local 35B MoE. Budget more for heavier tasks.
- **`approvals.mode: never`** is what lets the worker run unattended — appropriate for a
  trivial sandboxed proof. For real autonomous work prefer `manual` + `cron_mode: deny`
  (§5) until you trust the loop, per [security-hardening.md](security-hardening.md).
- **Isolation.** `examples/autonomous-loop/run-loop-proof.sh` runs the whole thing in a
  throwaway `HERMES_HOME` (`mktemp -d`), so your live `~/.hermes` is never touched.

---

## 5. Talk to it — and the approval gate

You drive Hermes one of four ways. Pick by how much you want to babysit it:

| You want to… | Use |
|---|---|
| Get one quick answer from a script | `hermes chat -q "…" -Q` (or `hermes -z "…"`) |
| Sit with it and watch each turn | `hermes chat` (TUI) / `hermes chat --cli` |
| Have it message you on your phone | a chat platform — Telegram / Mattermost bridge (per profile) |
| Drive it synchronously from code | the Desktop control endpoint — **needs Hermes Desktop, §7** |

**Chat platforms (human-in-the-loop over messaging).** Platforms are enabled
*per profile*, in that profile's `config.yaml` under `platforms:`, with the bot token in
`.env`. In a channel the bot needs an @mention (`require_mention: true`); DMs are
free-response. A thread maps to a session — reply in the same thread to continue it, start
a new thread/DM for a fresh session. The agent streams its tool progress and results back
into the thread, so the chat itself becomes a live "watch it work" view. For a one-off
push with **no** agent loop:

```bash
hermes send "deploy finished"                 # fire a message to a platform from a script/cron
```

**Approvals — it asks before doing anything risky.** With `approvals.mode: manual` (the
recommended default) the agent **pauses** before a dangerous action, waits `timeout`
seconds, and expects you to approve or deny — inline in the TUI/CLI, in the chat thread,
or via Desktop. This is the human gate that lets you let it run without it acting
irreversibly behind your back:

```yaml
approvals:
  mode: manual        # manual | auto | deny
  timeout: 60         # seconds to wait for a human before auto-denying
  cron_mode: deny     # block dangerous ops triggered by cron (recommended)
  mcp_reload_confirm: true
  destructive_slash_confirm: true
```

Set `mode: manual` and `cron_mode: deny` **before** letting the gateway run autonomously.

---

## 6. Interrupt or redirect it

If a run goes sideways, you have a stop at every layer — from a keystroke to pulling a
task back off the board:

- **Interactive turn (TUI/CLI):** press **Ctrl-C** to interrupt the current turn.
- **A one-shot:** it's a single foreground process — **Ctrl-C** ends it; or constrain it
  in advance with `hermes chat --max-turns N` so it can't loop forever (default 90).
- **The whole autonomous gateway:** stop the daemon — no more tasks get claimed, no
  platform messages get processed:

  ```bash
  hermes gateway stop
  hermes gateway restart                       # stop + start cleanly
  ```

- **A single runaway autonomous task** — pull it back off the board instead of killing
  the whole gateway:

  ```bash
  hermes kanban reclaim <task-id>              # release the worker's claim on a running task
  hermes kanban block <task-id>                # mark it blocked so the dispatcher won't re-run it
  hermes kanban unblock <task-id>              # later: send it back to ready when you're ready
  ```

  (All three verified in `hermes kanban --help`: `reclaim`, `block`, `unblock`.)

- **Redirect** by resuming the session with corrected instructions rather than starting
  over: `hermes chat --resume <id>` (§1a) threads a new turn into the same conversation,
  so the agent keeps the context but takes your new direction.

The combination of `approvals.mode: manual` (it asks first), `--max-turns` (it can't run
away), Ctrl-C / `gateway stop` (hard stop), and `kanban reclaim`/`block` (surgical stop)
is the safety rail that lets a first-timer let it run without fear.

---

## 7. The optional cockpit — Hermes Desktop (NOT in this repo)

Everything above works with **only this repo + the `hermes-agent` CLI**. The one thing it
does *not* give you is a live GUI cockpit / a synchronous HTTP **control endpoint** you
can script against. Those come from a separate **Hermes Desktop** app that is **not part
of this repository** — without Desktop running there is simply no endpoint to call.

When Desktop *is* running it exposes a loopback, bearer-token HTTP control API (port +
token written to `~/.hermes-desktop/control.json`, mode 0600) for synchronous
observe/steer: `GET /health`, `GET /sessions`, `GET /sessions/<id>/transcript`,
`POST /chat`, `POST /sessions/<id>/message`. The blocking semantics (a `POST` returns only
when the turn finishes) make it the cleanest way to drive multi-minute runs from a script
without polling.

A **read-only reference client** ships in this repo at
[`scripts/observe/control-client.sh`](../scripts/observe/control-client.sh) — it reads
the port/token and does `GET /health`, `/sessions`, and a transcript tail. It **cannot
start Desktop**; if `~/.hermes-desktop/control.json` is absent it tells you Desktop isn't
running and exits.

```bash
bash scripts/observe/control-client.sh                 # health + sessions (needs Desktop)
bash scripts/observe/control-client.sh <session-id>    # + transcript tail
```

For the bind scope, token handling, and why this endpoint must stay loopback-only, see
[architecture/ports.md](architecture/ports.md) and
[security-hardening.md](security-hardening.md).

---

## Capability matrix — native vs external

Short version of what's reachable with **only this repo + the `hermes-agent` CLI** versus
what needs the separate Desktop app:

| Surface | How you reach it | Native or external? |
|---|---|---|
| **Live logs** | `hermes logs -f`, `--component`, `--session`, `--since`, `--level` | ✅ Native |
| **Session store / vet a run** | `hermes sessions list` / `browse` / `export --session-id` / `stats`; FTS over `state.db` | ✅ Native |
| **Kanban board** | `hermes kanban list` / `show` / `watch` / `tail` / `stats` | ✅ Native |
| **Interrupt / redirect** | Ctrl-C, `--max-turns`, `hermes gateway stop`, `hermes kanban reclaim` / `block` | ✅ Native |
| **Gateway health** | `hermes gateway status`, `hermes status --deep`, `gateway_state.json` | ✅ Native |
| **Raw state (read-only)** | `sqlite3 -readonly` on `state.db` / `kanban.db` | ✅ Native |
| **Web dashboard** | `hermes dashboard` → `http://127.0.0.1:9119` | ⚠️ via upstream hermes-agent CLI (not shipped in this repo) |
| **Chat / drive** | `hermes chat`, `hermes -z`, `hermes send`, Telegram/Mattermost bridge | ✅ Native |
| **Synchronous control endpoint / live cockpit GUI** | `POST /chat`, `/sessions/<id>/message`; `scripts/observe/control-client.sh` | ⚠️ **External — needs Hermes Desktop, NOT in this repo** |

---

## Quick-reference

| Goal | Command |
|---|---|
| One-shot answer | `hermes chat -q "…" -Q` / `hermes -z "…"` |
| Interactive | `hermes chat` (TUI) / `hermes chat --cli` |
| Resume a session | `hermes chat --resume <id>` |
| Start gateway (fg / service) | `hermes gateway run` / `hermes gateway install && hermes gateway start` |
| Is it alive? | `hermes gateway status`, `hermes status --deep` |
| Watch it think | `hermes logs -f` |
| Catch errors | `hermes logs --level WARNING` / `hermes logs errors` |
| Watch autonomous failures | `hermes kanban watch --kinds gave_up,crashed,timed_out` |
| Find a past run | `hermes sessions list` |
| Read / vet a run | `hermes sessions export --session-id <id> -` |
| What's queued/in-flight/done | `hermes kanban list --status running` / `ready` / `done` |
| Message yourself from a script | `hermes send "…"` |
| Stop everything | `hermes gateway stop` |
| Pull one task back | `hermes kanban reclaim <id>` / `hermes kanban block <id>` |
| Which profile am I on? | `hermes profile list`, `cat "$HERMES_HOME/active_profile"` |

---

## See also

- [getting-started.md](getting-started.md) — install, backend config, smoke test
- [profiles.md](profiles.md) — profile roles, per-profile platform topology
- [secrets.md](secrets.md) — API keys and platform tokens
- [security-hardening.md](security-hardening.md) — sandbox / secure-run, approval policy
- [architecture/ports.md](architecture/ports.md) — every port, bind scope, the Desktop control endpoint
- [backend.md § Known gotcha: Claude Code OAuth token override](backend.md#known-gotcha-claude-code-oauth-token-override) — if Hermes sends a `sk-ant-oat01…` token to a third-party backend and gets HTTP 401
