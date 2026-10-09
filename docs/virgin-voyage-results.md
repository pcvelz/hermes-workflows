# Virgin Voyage — First End-to-End Task Results

> The Definition-of-Done scorecard from this scaffold's first real task run through the full
> loop — `hermes kanban create` → dispatcher tick → worker spawn → local-model inference →
> (attempted) `kanban_complete` — plus the machinery defects that run surfaced, written up
> honestly rather than smoothed over.

Every other page in this repo describes the scaffold's intended shape. This page describes what
actually happened the first time a task was put through the whole pipeline for real. A shakedown
cruise's entire purpose is to find this class of problem while the cost of finding it is one test
task — not a production incident on real work. Treat everything below as exactly that: a
positive result, even where the verdict isn't PASS.

---

## Definition of Done — results

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Task created and picked up by the dispatcher | **PASS** | `hermes kanban create` with a real `--assignee` landed in `ready` and was claimed on a subsequent tick, per the promote/claim path in [docs/architecture/data-flow.md](architecture/data-flow.md). |
| 2 | Claim + worker spawn within one dispatcher tick | **PASS** | The worker process was spawned in the same tick that claimed the task — no extra tick of latency observed. |
| 3 | Requeue-after-timeout adapts | **PASS** | The run hit its iteration budget; the dispatcher reaped it and respawned a **fresh** worker that resumed from prior run history rather than starting blind. See [below](#3-requeue-after-timeout). |
| 4 | Local-model inference | **PASS** | The worker's turns were served end-to-end by the local `<model>` backend — no cloud fallback needed for the run to produce reasoning and tool calls. |
| 5 | Worker protocol ends in `kanban_complete` / `kanban_block` | **PARTIAL** | The *first* run exhausted its iteration budget without calling either terminal verb, and left no comments or artifacts behind it. See [below](#5-worker-protocol-ends-in-a-terminal-verb). |
| 6 | Output durability | **FAIL-as-designed** | The task's `scratch` workspace was deleted the moment the task completed, taking the deliverable with it. See [below](#6-output-durability). |

### 3. Requeue-after-timeout

The dispatcher's reap-and-respawn path (see [cron/README.md](../cron/README.md)'s "the tick
model") worked as intended even under a real failure: the reaped worker's replacement was able to
pick up prior run history rather than repeating work from zero. This is the mechanism doing its
job — a stalled run did not silently vanish, and the retry was not a blind restart.

### 5. Worker protocol ends in a terminal verb

The first attempt at the task ran its full iteration budget without ever calling
`kanban_complete(summary)` or `kanban_block(reason)` — see
[docs/task-authoring.md](task-authoring.md#what-happens-after-you-create-it--the-worker-lifecycle)
for why that matters: a worker that never calls either leaves the task with no terminal state
until the reaper notices. Because the run also hadn't written any early comments (Lesson 2 in
that same page), the respawned worker had run history to resume from, but no human-readable
breadcrumb describing what the first attempt had actually tried. The requeue (#3) recovered the
*task*; it could not recover the *narrative* of the first attempt.

### 6. Output durability

The task's `--workspace scratch` (the default) was torn down at the moment the task's completion
call succeeded, and the ticket's instruction to "write the output to a file in your workspace"
did not survive that teardown. This is not a bug in the reaping or dispatch logic — it is
`scratch`'s documented, by-design behavior once you know to look for it. It is exactly what is
now written up as Lesson 1 in [docs/task-authoring.md](task-authoring.md#1-scratch-workspaces-are-deleted-the-moment-the-task-completes),
**directly because of this run** — the fix is procedural (use `--workspace dir:<path>`, or have
the worker paste the full deliverable into the `kanban_complete` summary instead of a
workspace-relative pointer), not a code change to the scaffold.

---

## Shakedown findings

The rest of what the virgin voyage surfaced isn't captured by the six DoD checkboxes above — it's
a set of machinery defects and rough edges in the scaffold itself. Listed here, generalized, and
framed for what they are: **exactly what a shakedown cruise is for.** A scaffold that had never
run a real task would look cleaner in the docs and be far more dangerous in production — every
adopter would discover these the hard way, mid-task, with no name for what was happening. Now
each one has a name.

1. **Silent `DEFAULT_CONFIG` fallback on a corrupt profile config.** A profile whose
   `config.yaml` fails to parse, or is missing an expected key, does not hard-fail at gateway
   startup — it silently falls back to a built-in default configuration and keeps running. The
   worker then executes with the *wrong* model, toolset, or approval mode, and nothing in the
   visible logs names which keys actually fell back.
   **Fix direction:** validate the merged config at load time and fail loudly, or at minimum log
   every key that resolved to a default by name, instead of running silently on a degraded
   config.

2. **Approval-gate firing inside an unattended worker, and being terminal-bypassable.** A kanban
   worker is meant to run fully unattended (`--accept-hooks` — see
   [docs/task-authoring.md](task-authoring.md#what-happens-after-you-create-it--the-worker-lifecycle)),
   but the approval flow can still surface a prompt that then blocks indefinitely, waiting on a
   human who isn't watching an autonomous run. Separately, the same gate can be routed around
   from inside the `terminal` tool in a way that undercuts the boundary it exists to enforce
   (e.g. the `manual` approval posture `coding` is meant to keep in
   [`config/config.yaml.example`](../config/config.yaml.example), or `qa-tester`'s
   read-only-never-edits-source contract).
   **Fix direction:** audit every tool path for whether it genuinely honors
   `--accept-hooks`/`approvals.cron_mode`, and treat "blocks on human input inside an unattended
   kanban run" as a defect against the unattended-worker contract, not a one-off to route around.

3. **Workers misreading GLOBAL run-ids as their own retry history.** When a worker resumes after
   being respawned, it appears to consult run/session history scoped more broadly than its own
   task — broad enough that a worker can draw "here is what I already tried" conclusions from a
   **different** task's run history.
   **Fix direction:** verify resume/retry history lookups are scoped strictly by task id before
   depending on resume behavior anywhere cross-task bleed would be actively harmful, not just
   unhelpful.

4. **`timed_out` vs. iteration-budget outcome mislabeling.** A run that stopped because it
   exhausted its turn/iteration budget (`agent.max_turns`, or `--goal-max-turns` in judge-loop
   mode — see [Lesson 3](task-authoring.md#3-the-turn-budget-is-real-and-its-per-run)) was
   recorded under the same generic `timed_out` outcome as a run that hit its wall-clock
   `--max-runtime` cap. These need different fixes on the next attempt — raising
   `--max-runtime` does nothing for a turn-budget exhaustion, and vice versa — but the recorded
   outcome doesn't tell you which one happened.
   **Fix direction:** give the two conditions distinguishable outcome codes so whoever (or
   whatever) triages a failed task knows which knob to turn.

5. **PID-alive claim extension masking a wedged worker, with no heartbeat-staleness alarm.** The
   built-in dispatcher's claim-TTL is refreshed by heartbeats and backed by a PID-liveness check
   (see [cron/README.md](../cron/README.md)'s "the tick model") — but "the OS process is still
   alive" and "the worker is still making progress" are different facts, and today only the
   first one gates claim renewal. A worker wedged on a call that will never return can sit in
   `in_progress` indefinitely, its claim quietly renewed forever, with no alarm.
   **Fix direction:** track heartbeat *staleness* (time since the last heartbeat that carried
   genuinely new information) as a signal independent of PID liveness, and alarm on staleness
   even while the process is technically still alive.

6. **Hallucinated-reference detector lacking cross-board awareness and a task-body whitelist.**
   [Lesson 4](task-authoring.md#4-dont-reference-another-boards-task-ids-by-their-bare-id)
   already documents the false-positive shape this produces — citing another board's bare task
   ID gets flagged as fabricated. The voyage surfaced the two specific gaps behind it: the
   detector has no cross-board awareness (it cannot distinguish "not on my board" from
   "invented"), and it has no whitelist for IDs that were already present in the task's own
   body/brief — i.e. IDs the human or orchestrator legitimately handed the worker, which the
   worker did not invent.
   **Fix direction:** whitelist any ID that appears verbatim in the task body before flagging it
   as hallucinated; treat an unresolvable ID that is **also absent from the task body** as the
   real signal.

7. **Toolset advertising vision/web-extract capabilities the local backend can't actually
   serve.** The platform toolset list includes tools whose descriptions imply capabilities (an
   image/vision read, a full-page content-extraction call) that the local `<model>` backend cannot
   fulfill. The tool is present and callable — it just silently degrades or errors when invoked —
   rather than being absent or clearly marked unsupported for this backend.
   **Fix direction:** gate toolset advertisement by backend capability where possible, or at
   minimum document per-backend tool support, so neither the worker nor whoever writes its task
   body plans around a capability the configured model can't actually serve.

---

## Second voyage — a harder task surfaces four more

A follow-up run put a genuinely large browser task (a price-comparison sweep over a ~12-cell
date/duration matrix, each cell reached through a stateful search form, real price only on the
per-listing detail page) through the same pipeline. It surfaced four more rough edges — all
worth expecting before you hit them:

8. **The gateway's turn budget silently overrides every worker's own.** A worker profile can set
   `agent.max_turns`, but the dispatcher-hosting gateway loads *its* profile's `max_turns` and
   exports it as `HERMES_MAX_ITERATIONS` into its process environment; the spawn path copies that
   environment to every worker, and the env var wins over the worker's config. Symptom: raising a
   worker profile's `max_turns` changes nothing — workers keep hitting the gateway's number.
   Fix direction: don't propagate `HERMES_MAX_ITERATIONS` from gateway to a differently-profiled
   worker (strip it at spawn so the worker's own config authority holds), or raise the gateway
   profile's budget knowingly.

9. **A shared browser endpoint carries state between worker runs.** When every worker attaches to
   one long-lived headless browser (one CDP endpoint), whatever page the previous run left behind
   — or a login/consent interstitial — is what the next run wakes up on. A run that assumes it
   starts on the target home page mis-orients. Fix direction: navigate to the target URL fresh at
   the start of every run rather than trusting the attached page's current location; persist the
   *procedural* knowledge (how to drive the site) alongside the data ledger so a requeue resumes
   navigation instead of re-discovering it.

10. **First-record-fast beats analyze-first.** A worker that reaches the first detail page and then
    spends its whole budget dissecting one listing's price tiers — before writing a single ledger
    row — produces nothing and forces a from-scratch requeue. Breadth over depth: capture the one
    headline number the task asked for, write the row, move on. A committed approximate record
    beats a perfect uncommitted one.

11. **A local model can be the ceiling even when the machinery isn't.** Every mechanism above can
    be sound and a smaller local model still can't reliably drive a complex, stateful, multi-page
    task to completion — it misreads pages, over-analyzes, burns turns. That is a *model*-capacity
    finding, not a machinery one; the honest response is to escalate the worker's backend for that
    task, decompose the work into per-cell units small enough for the local model, or accept a
    proven-capability partial. Note the credit shape of whatever you escalate to: a metered
    "extra-usage" pool for tool-using cloud calls can be empty (`HTTP 400: out of extra usage`),
    which stops a tool-heavy worker cold regardless of how well it would otherwise drive.

---

## Framing: this is what a shakedown cruise is for

None of the above should read as "the scaffold is broken." Read it the other way: **Lesson 1 in
[docs/task-authoring.md](task-authoring.md#1-scratch-workspaces-are-deleted-the-moment-the-task-completes)
already exists, in writing, before a single adopter hit it, directly because of this run.** That
is the entire value of running one. The seven findings above are now documented, expected rough
edges instead of silent traps — every one of them has a name and a fix direction before you find
it mid-task with no idea what just happened.

If you hit one of these seven in your own deploy, you now have prior art for the fix direction.
Closing any of them is exactly the kind of scaffold-fleshing-out contribution
[CONTRIBUTING.md](../CONTRIBUTING.md) is asking for.

---

## See also

- [docs/task-authoring.md](task-authoring.md) — the CLI contract and the hard-won lessons this run fed directly into
- [cron/README.md](../cron/README.md) — the dispatcher tick model, claim-TTL, and PID-liveness reaping referenced above
- [docs/roles.md](roles.md) — how the role↔profile mapping held up (and changed) after this run
- [docs/browser-capability.md](browser-capability.md) — the browser-toolset decision this run's `qa-tester` work drove
- [README.md](../README.md) — Status section, for where this fits in the scaffold's overall maturity
