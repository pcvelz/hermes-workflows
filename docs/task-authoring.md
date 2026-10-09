# Task Authoring

> How to put a task on the board — the CLI contract for creating kanban tasks that
> this repo's other docs assume already exists.

[docs/architecture/data-flow.md](architecture/data-flow.md) documents what happens to a
task once it exists: the `todo → ready → in_progress → done` state machine, the
dispatcher tick, the reaper. This page documents the piece that comes *before* all of
that — how you actually create a task in the first place, using the upstream
hermes-agent CLI, and the handful of authoring mistakes that make an otherwise
well-written ticket silently misbehave.

---

## The one rule that trips up every first-time adopter

> **A task with no `--assignee` is created successfully, sits in `ready`, and is never
> claimed — not by any profile, ever. This is by design, not a bug.**

The dispatcher's per-tick claim step only looks at tasks that are **both** `ready`
**and** already assigned to a specific profile. An unassigned ready task is invisible to
it — there is deliberately no "pick anyone idle" fallback at the CLI-creation layer.
**Always pass `--assignee <profile>`.**

The base config template also exposes a `kanban.default_assignee` key (see
[`config/config.yaml.example`](../config/config.yaml.example)), and the orchestrator
profile overlay sets it to `coding`. Do not treat that key as a substitute for
`--assignee` on the CLI — the verified behavior is that a task created via
`hermes kanban create` with no `--assignee` sits un-dispatched regardless. Confirm the
exact interaction with `default_assignee` against your own hermes-agent version before
relying on it for anything unattended.

---

## Creating a task

```bash
hermes kanban create "<title>" [flags]
```

`<title>` is the only required positional argument. Everything else is a flag — the two
you will use on almost every real ticket are `--body` and `--assignee`.

> Flags below match the upstream hermes-agent CLI as verified against the reference
> install (hermes-agent v0.14.0 — see [docs/patches.md](patches.md) for the version this
> scaffold tracks). Run `hermes kanban create --help` to confirm the exact set your
> installed version ships; flags can change across releases.

| Flag | Repeatable | Purpose |
|---|---|---|
| `--body <text>` | no | The worker's brief — the actual task description injected into the worker's session. Without it the worker has only the title to go on. |
| `--assignee <profile>` | no | **Required for automatic dispatch.** See [above](#the-one-rule-that-trips-up-every-first-time-adopter). |
| `--parent <task_id>` | yes | Declares a dependency. The child is born in `todo` and is promoted to `ready` only once every declared parent is `done`. See [below](#--parent--the-dependency-graph-and-the-promote-leg). |
| `--skill <name>` | yes | Force-loads an extra skill's prompt fragment into this task's worker, on top of the built-in `kanban-worker` skill. See [below](#--skill--force-loading-extra-capability). |
| `--workspace scratch\|worktree\|dir:<path>` | no | Where the worker operates. Default is `scratch`. See [below](#--workspace--where-the-worker-operates) — this is where Lesson 1 lives. |
| `--goal <text>` | no | Switches the task into judge-loop mode for open-ended cards. See [below](#--goal--goal-max-turns--judge-loop-mode). |
| `--goal-max-turns <n>` | no | Turn ceiling for judge-loop mode. |
| `--max-runtime <seconds>` | no | Per-task wall-clock cap. On excess the dispatcher SIGTERMs the worker and requeues the task. |
| `--max-retries <n>` | no | Per-task override of the board's failure circuit breaker (real default `2` — see `kanban.failure_limit` in [`config/config.yaml.example`](../config/config.yaml.example)). |
| `--priority <n>` | no | Ordering hint among competing `ready` tasks for the same idle profile. See the caveat below if you run the external dispatcher hook instead of the built-in one. |
| `--triage` | no | Creates the task parked in a triage state instead of the normal `todo`/`ready` path — for a rough idea that needs a human (or a dedicated specifier pass) to refine the body, assignee, and acceptance criteria before it is fit to dispatch. |
| `--idempotency-key <key>` | no | Your own dedupe key. Re-running the same `create` with a key already on the board is a no-op instead of creating a duplicate — useful when `create` is called from a retried script or cron job. |

> **On `--max-runtime` and the board default:** leaving `--max-runtime` unset does not
> mean "no cap" — it means "inherit whatever `kanban.dispatch_stale_timeout_seconds` is
> set to on the board." Upstream ships that key **disabled (`0`)** by default; this
> scaffold's `config/config.yaml.example` sets an explicit recommended override of
> `14400` (4 hours) rather than relying on the (disabled) upstream default. Confirm which
> one is actually in effect on your board before assuming an unset `--max-runtime` will
> ever expire.

> **On `--priority` and the external dispatcher:** if you have enabled the OPTIONAL
> external dispatcher hook
> ([`hooks/per-profile-dispatcher/handler.py`](../hooks/per-profile-dispatcher/handler.py))
> instead of the built-in gateway dispatcher (see the mutual-exclusivity note in
> [cron/README.md](../cron/README.md)), be aware its reference `pick_task_for()`
> scheduling logic is FIFO-plus-assignee-match only — the `Task` model it works against
> carries no priority field at all. `--priority` is meaningful against the built-in
> gateway dispatcher; treat it as advisory-only (or wire it into `pick_task_for()`
> yourself) if you have switched to the external hook.

### `--parent` — the dependency graph and the promote leg

```bash
hermes kanban create "Wire the payment webhook" \
  --body "..." \
  --assignee coding \
  --parent task_1a2b3c \
  --parent task_4d5e6f
```

The child is born in `todo`. The dispatcher's promote step (see
[data-flow.md](architecture/data-flow.md) and `deps_resolved()` in
[`hooks/per-profile-dispatcher/handler.py`](../hooks/per-profile-dispatcher/handler.py))
only flips it to `ready` once every task named by `--parent` is `done`. A missing or
unknown parent id is treated the same as an unresolved dependency — the child stays in
`todo` forever rather than silently skipping the gate.

### `--skill` — force-loading extra capability

`--skill <name>` (repeatable) force-loads an additional skill's prompt fragment into the
worker's system prompt for this task only, appended on top of the built-in
`kanban-worker` skill every task worker already carries. Use it when a specific task
needs a capability its assignee's profile doesn't carry by default (see
[docs/skills-whitelist.md](skills-whitelist.md) for how the whitelist normally gates
this) — for example, forcing in a `browser` skill for a one-off scrape task assigned to
the `coding` profile, without permanently widening `coding`'s skill whitelist.

```bash
hermes kanban create "One-off scrape of the vendor pricing page" \
  --body "..." \
  --assignee coding \
  --skill browser \
  --skill web-fetch
```

### `--workspace` — where the worker operates

| Mode | What it is | Survives task completion? |
|---|---|---|
| `scratch` (default) | An ephemeral working directory the board allocates for this task only | **No — verified: deleted at the moment the task completes successfully.** See [Lesson 1](#1-scratch-workspaces-are-deleted-the-moment-the-task-completes). |
| `worktree` | A git worktree checkout | Not independently verified either way this session. If you rely on it for durable output, confirm your own hermes-agent version's cleanup behavior first, and commit/push before calling `kanban_complete` regardless. |
| `dir:<path>` | A path you own, outside the board's managed directories | Yes — nothing in the board's own lifecycle touches it. |

### `--goal` / `--goal-max-turns` — judge-loop mode

`--goal <text>` switches the task into **judge-loop mode**: rather than a single fixed
`--body` brief executed once, the worker iterates toward the open-ended goal you describe,
across multiple turns, until an internal judge considers the goal satisfied. Use it for
exploratory cards where a single fixed acceptance criterion would under-specify success
(e.g. "reduce p95 latency on the search endpoint" rather than a fixed step list).

`--goal-max-turns <n>` is the hard ceiling on how many judge-loop turns the task gets
before it is stopped regardless of whether the judge is satisfied. Pairs with
[Lesson 3](#3-the-turn-budget-is-real-and-its-per-run): this is a per-task budget layered
on top of — not a replacement for — the profile's own `agent.max_turns`.

> This repo has not independently verified the judge's exact pass/fail semantics — treat
> `--goal` as upstream hermes-agent functionality and consult
> `hermes kanban create --help` (or the upstream hermes-agent docs) for your installed
> version's exact behavior before depending on it for anything unattended.

---

## What happens after you create it — the worker lifecycle

Once a task is `ready` and has an `--assignee`, the dispatcher's tick loop (default
**60 s** — `kanban.dispatch_interval_seconds` in
[`config/config.yaml.example`](../config/config.yaml.example); see also
[cron/README.md](../cron/README.md)) claims it and spawns a one-shot worker:

```bash
hermes -p <assignee> --accept-hooks --skills kanban-worker chat -q "work kanban task <task_id>"
```

This is a single, non-interactive `hermes chat` invocation scoped to one task:
`-p <assignee>` selects the profile you named with `--assignee`, `--accept-hooks` lets it
run unattended (no interactive approval prompts — see the `approvals` block in
`config/config.yaml.example` for what this does and does not bypass), and
`--skills kanban-worker` loads the built-in worker skill (plus whatever you forced in via
`--skill`).

Every worker session must end by calling one of two terminal kanban tool verbs — there is
no implicit "done" state:

| Verb | Effect |
|---|---|
| `kanban_complete(summary)` | Marks the task `done`. `summary` is the durable record of what happened — see [Lesson 1](#1-scratch-workspaces-are-deleted-the-moment-the-task-completes) for why it may need to carry more than a one-line recap. |
| `kanban_block(reason)` | Marks the task `blocked` with `reason` recorded for a human (or the orchestrator profile) to unblock. |

A worker that never calls either — because it crashed, ran out of turns, or was reaped —
leaves the task in `in_progress` with no terminal state. The dispatcher's stale-worker
reaper (heartbeat / startup / max-runtime checks — see `worker_is_stale()` in
[`hooks/per-profile-dispatcher/handler.py`](../hooks/per-profile-dispatcher/handler.py)
and [cron/README.md](../cron/README.md)) is what eventually notices and returns it to
`ready`. **Heartbeating keeps a task from being reaped prematurely, but a heartbeat is
not a checkpoint** — it says "still alive," not "here is what I've produced so far." See
[Lesson 2](#2-a-dead-run-leaves-no-breadcrumbs-unless-you-write-early).

## Watching a task work

```bash
hermes kanban show <task_id>     # current state, assignee, body, comments
hermes kanban tail <task_id>     # follow the worker's live output
```

Or read the raw per-task log directly: `$HERMES_HOME/logs/<task_id>.log` (the board
root's `logs/` subdir — see [data-flow.md](architecture/data-flow.md)).

---

## Hard-won lessons — read before writing your first ticket

### 1. Scratch workspaces are deleted the moment the task completes

Verified this session: the default `scratch` workspace is torn down **at the moment
`kanban_complete` succeeds** — not at some later cleanup pass. If the ticket's `--body`
told the worker to "write the report to `report.md` in your workspace," that file is
gone the instant the task shows as `done`. There is no grace period and no separate GC
step to race.

**Fix — do one of:**
- Use `--workspace dir:<path>` pointed at a location outside the board's managed
  directories.
- Have the worker post the artifact as a kanban comment as it's produced.
- Word the ticket so the worker pastes the **full** deliverable content directly into the
  `kanban_complete(summary)` call, not just a pointer to a workspace-relative path.

A ticket that says "write your findings to `findings.md`" with no other instruction has
zero durable output once it completes — this is a *silent* failure mode: the task shows
`done`, looks successful, and produced nothing you can retrieve.

### 2. A dead run leaves no breadcrumbs unless you write early

A worker that times out, crashes, or gets reaped mid-task leaves nothing behind for
whoever (or whatever) picks the task up next — unless it had already written artifacts or
comments *before* it died. Heartbeating (see the worker-lifecycle section above) only
prevents premature reaping; it records no work product.

**Fix:** write tickets that demand incremental, early writes — a comment or a
`dir:<path>` artifact after each meaningful step — instead of a single
"write everything at the end" instruction. Treat "report only at the end" as an
anti-pattern for any task long enough to plausibly hit `--max-runtime` or the turn budget
(Lesson 3).

### 3. The turn budget is real, and it's per run

`agent.max_turns` (profile config, see
[`config/config.yaml.example`](../config/config.yaml.example)) is a hard ceiling on a
single worker run, and it is the binding constraint for exploratory or open-ended
tickets long before wall-clock or retry limits come into play. A ticket that says
"explore approaches A, B, and C, then pick the best one" can easily blow through the turn
budget on exploration alone, leaving zero turns for the actual work.

**Fix:** time-box candidate work explicitly inside the ticket body — e.g. "spend at most
a few turns on each of A/B/C, then commit to one and implement it" — rather than leaving
the split between exploration and execution to the worker's judgment. This applies
whether or not the task uses `--goal`/`--goal-max-turns` judge-loop mode; the underlying
profile's `agent.max_turns` is still in force on top of it.

### 4. Don't reference another board's task IDs by their bare ID

If a `kanban_complete(summary)` (or a comment) references a task ID that belongs to a
*different* board — another project's kanban, a ticket in an entirely separate tracker —
by its bare ID (e.g. "see task_9f8e7d" or "follow-up to PROJ-1234"), the completion
summary's hallucinated-reference check can flag it as a false positive: it looks up the
ID against its own board, doesn't find it, and treats it as a fabricated reference.

**Fix:** describe external tickets, don't cite their bare IDs — "the follow-up ticket
filed against the payments-service backlog for the webhook retry bug" instead of
"see task_9f8e7d". If a human still needs the ID to look it up, put it in prose that
makes clear it points off-board, e.g. "(tracked externally as PROJ-1234, not on this
board)".

---

## A worked example

Putting the assignee contract, workspace durability, and incremental-writes lessons
together:

```bash
hermes kanban create "Investigate p95 latency regression on /search" \
  --body "Profile the /search endpoint under the k6 load test in perf/. \
Time-box: spend at most 2-3 turns each on (a) DB query plan, (b) N+1 calls, \
(c) serialization cost, then commit to the most promising lead and fix it. \
Post a kanban comment after each of the three investigation legs with what \
you found, BEFORE starting the fix, so a successor can pick up your findings \
if this run gets interrupted. When done, paste the full before/after p95 \
numbers into your kanban_complete summary — the workspace will not survive \
past completion." \
  --assignee coding \
  --workspace dir:/absolute/path/you/own/search-latency-investigation \
  --max-runtime 5400 \
  --max-retries 2 \
  --priority 5
```

This bakes in: an explicit assignee (the one rule), a durable `dir:` workspace, an
explicit time-box for the exploratory phase (Lesson 3), a mandated incremental-comment
habit (Lesson 2), and an explicit instruction to inline the final numbers into the
completion summary rather than rely on the workspace surviving (Lesson 1).

---

## See also

- [`docs/architecture/data-flow.md`](architecture/data-flow.md) — the task state machine and dispatcher tick once a task exists
- [`docs/profiles.md`](profiles.md) — the profile names valid for `--assignee`
- [`docs/skills-whitelist.md`](skills-whitelist.md) — how the `kanban-worker` skill and `--skill` force-loads interact with the per-profile whitelist
- [`cron/README.md`](../cron/README.md) — dispatcher tick cadence, reaping mechanism, and the built-in-vs-external-dispatcher mutual exclusivity
- [`hooks/per-profile-dispatcher/handler.py`](../hooks/per-profile-dispatcher/handler.py) — the OPTIONAL external dispatcher reference implementation (promote/reap/dispatch pure logic)
- [`config/config.yaml.example`](../config/config.yaml.example) — the `kanban:` block (`dispatch_interval_seconds`, `failure_limit`, `dispatch_stale_timeout_seconds`, concurrency caps)
