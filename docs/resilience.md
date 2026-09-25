# Resilience: waiting, counting, and telling a human

> A card died like this. The model backend was held by another caller for ten
> hours and aborted every worker stream. The worker retried three times, gave
> up, and exited cleanly without completing. The dispatcher recorded `crashed`
> and incremented the failure counter **on the card**. At the limit the card
> auto-blocked with a `gave_up` event, and the promoter refused to auto-recover
> a card at its failure limit — so it left the board permanently. **Nobody was
> told.** A human noticed hours later and unblocked it by hand.
>
> Three things were wrong, and raising the retry count fixes none of them.

| What was wrong | The fix |
|---|---|
| A local-queue abort was indistinguishable from a genuine stall | [The taxonomy](#1-the-taxonomy-wait-class-versus-real) — a `backend_busy` reason, and a wait-class/real split |
| Patience was a retry count | [The wait budget](#2-the-wait-budget) — `agent.model_wait_budget`, a duration |
| A machine failure cost the card a life | [The counting policy](#3-failure-counting-is-a-policy) — `kanban.count_toward_breaker` |
| Nobody was told | [The escalation channel](#4-the-escalation-channel) — a human gets a message, always |

A later dry run against a live board added a fourth lesson, and it is the one
that governs whether any of the rest survives contact: **a channel that pages
falsely gets muted, and a muted channel is how a board goes silent a second
time.** So "stranded" has to mean *nobody can run this* rather than *nobody
has run it yet*, and the deadlock that actually bites — a card whose worker is
alive and idle — has to be detected on its own signal. Both are covered under
[the escalation channel](#4-the-escalation-channel).

Everything below lives in [`scripts/resilience/`](../scripts/resilience/README.md)
and is covered by `bash scripts/test.sh smoke`.

---

## 1. The taxonomy: WAIT-CLASS versus REAL

The upstream agent classifier (`agent/error_classifier.py`) returns a
`FailoverReason` for every API failure. The taxonomy splits in two:

### WAIT-CLASS — the machine could not serve the request

| Reason | Means |
|---|---|
| `overloaded` | Provider returned 503/529 |
| `server_error` | Provider returned 500/502 |
| `timeout` | Connection or read timeout — a genuine stall |
| `rate_limit` | 429 or a quota window |
| `backend_busy` | **New.** A *local* queue had no capacity |

A wait-class failure is **retried against a time budget, never against a
count**, and **never counted against a card**. Nothing about the card caused
it, and nothing about the card can fix it.

### REAL — the request or the account is wrong

`auth_permanent` · `billing` · `context_overflow` · `payload_too_large` ·
`model_not_found` · `provider_policy_blocked`

Retrying cannot help. These keep the classic `agent.api_max_retries` count
limit, unchanged.

Anything else — `unknown`, a transient `auth`, a reason a future agent version
introduces — is treated as **REAL**. That is deliberate and it is the fail-safe
direction: an error nobody recognises should surface to a human, not quietly
consume a twelve-hour budget.

### Why `backend_busy` had to exist

Two very different things landed in `timeout`:

1. **A mid-stream disconnect from the local proxy.** The queue killed the
   stream because the slot went to another caller. Error text:
   `upstream disconnected mid-stream`.
2. **A request parked behind a model swap.** The router was loading a different
   model. Error text: `no router for requested model`, `model swap in progress`.

Neither is a stall. Both mean *no capacity right now*, and both were mixed in
with genuine stalls where the model really did hang — so they inherited the
stall's retry-count treatment and the stall's cost to the card.

`backend_busy` is derived by refining the upstream classifier's answer rather
than replacing it. The refinement fires **only** when both conditions hold:

* the error text carries a local-queue abort signature, **and**
* the backend is local (`127.0.0.1`, `localhost`, `::1`, `*.local`).

A remote provider dropping a stream stays `timeout`, with exactly its previous
behaviour. This module widens the taxonomy; it does not second-guess the
classifier.

```python
from error_policy import refine

refine("timeout", "upstream disconnected mid-stream",
       base_url="http://127.0.0.1:<PORT>").reason
# 'backend_busy'   (wait_class=True, source='refined')

refine("timeout", "upstream disconnected mid-stream",
       base_url="https://api.example-provider.com").reason
# 'timeout'        (wait_class=True, source='upstream')
```

---

## 2. The wait budget

**A worker that cannot get model capacity WAITS. Patience is a duration, not a
retry count.**

```yaml
agent:
  model_wait_budget: 6h      # shipped default — the user changes it
  api_max_retries: 3         # unchanged — now governs REAL failures only
```

Accepts `30m` / `6h` / `12h` / `1800`; a bare number is seconds. This is the one
answer to "how long may a request wait before it fails". Six hours is the
default because a local model that is busy is a queue: a request parked behind
other callers can wait hours and still be served, and someone chatting with the
agent over Mattermost or Telegram must not see an error, a retry notice or a
gateway status line in the meantime. `12h` is what makes a ten-hour outage
survivable.

When no budget is configured and a card's `max_runtime` is shorter than the
default, the default yields: it takes 90 % of the card cap, so an unconfigured
deploy never trips the invariant below. An explicit budget is never adjusted.

### A queued request is silent — keep the silence bound at the budget

A local proxy that queues (llama-swap parks a request while every slot is busy)
sends a parked request nothing but keep-alive pings, and the Anthropic SDK drops
ping events before the agent sees them. To the agent a parked request is
silent. With `stream_silence_limit_s` at its 300 s default the request is ended
after five minutes and re-sent, which puts it at the **back** of the queue; on a
box that stays busy it is never served, and after `api_max_retries` of that the
user gets an error. On such a backend set `stream_silence_limit_s` to the wait
budget (`21600` for `6h`); the card still shows the wait, and the escalator
still pages on its own five-minute rule.

`WaitBudget.should_retry()` is the drop-in replacement for
`retry_count < max_retries`:

* **wait-class** → keep going while the budget holds. `retry_count` is ignored
  entirely; it stays in the log line as bookkeeping, not as a limit.
* **real** → the classic count limit, untouched.

The clock starts at the **first wait-class failure**, not at the start of the
call, and **resets on success** — so a long run that hits a busy patch every
hour gets a fresh budget each time, exactly like `retry_count = 0` after a
successful call.

### The invariant

> **`model_wait_budget` MUST be strictly less than the card's `max_runtime`.**

The card runtime cap is the **outer bound** — it is what a human reasoned about
when they said "this card gets four hours". A budget at or above it lets the
worker burn the entire card allowance sitting in backoff and then be reaped as
`timed_out`: the same silent death, with a different label.

It is asserted in `validate_budget()` and again in the `WaitBudget`
constructor. A violation is a **startup error**, not a warning:

```python
WaitBudget("12h", max_runtime_seconds="4h")
# WaitBudgetInvariantError: agent.model_wait_budget must be STRICTLY LESS
# than the card's max_runtime: budget=12h >= max_runtime=4h. ...
```

### Staying alive while waiting

The worker keeps its session in flight for the whole wait. Nothing extra is
needed: the existing backoff loop already touches activity every ~30 s, which
keeps the heartbeat fresh and the claim extended. `WaitBudget.cap_sleep()`
clamps an individual backoff sleep to the remaining budget, so the moment of
giving up is the moment the budget actually ran out — the escalation message
never lies about when the worker stopped waiting.

---

## 3. Failure counting is a policy

Every non-success outcome funnels through `_record_task_failure` in
`hermes_cli/kanban_db.py` and increments one counter, `consecutive_failures`,
**on the card**. At the limit the card auto-blocks with `gave_up`, and
`recompute_ready` refuses to auto-recover a card at its failure limit.

That design is right — it is what stops an infinite block/recover/respawn
cycle. The *input* was wrong: a card that never got a single token out of the
model has not failed. The machine has.

So which outcomes count became configurable:

```yaml
kanban:
  count_toward_breaker:
    crashed: true
    spawn_failed: true
    timed_out: true
    backend_busy: true
    protocol_violation: true
```

**Shipped defaults are conservative — everything counts, exactly as today.**
A deploy whose workers share one local model turns the machine-caused ones off,
because there they are the common case rather than the exception:

```yaml
kanban:
  count_toward_breaker:
    crashed: true
    spawn_failed: false
    timed_out: false
    backend_busy: false
```

An outcome nobody configured counts. Fail safe: the breaker keeps working for
outcomes nobody has thought about yet.

> **Turn an entry off only together with a working escalation channel.** A card
> that stops counting can retry indefinitely; what makes that safe is that a
> human is told every time.

### How it takes effect: a reconciler, not a fork

The counting rule lives inside `_record_task_failure`, upstream code in an
~8000-line file that churns between agent releases. Replacing that file to add
a policy lookup means carrying a fork and re-deriving it after every upgrade.

Instead, `scripts/resilience/escalator.py` reconciles from outside. On each
tick, for every card sitting at `blocked` with a tripped counter, it reads the
`gave_up` event that put it there, refines the trigger outcome (so a card
killed by a local-queue abort is recognised as `backend_busy` even though the
dispatcher labelled it `crashed`), and — when that outcome does not count —
clears the counter and returns the card to `ready`.

Both halves are required and happen together. `recompute_ready` refuses to
auto-recover a card whose counter is at the limit, so resetting the status
without resetting the counter would put the card straight back into the same
trap. The reconciler writes an `unblocked` event, the same kind a human
`hermes kanban unblock` produces, so the board ends in an identical state
either way.

**Trade-off:** the correction is eventually-consistent — the card is blocked
for up to one tick before it is put back — rather than atomic. That is
acceptable because a human is being told in the same tick regardless, and it
buys a system with no fork to maintain.

---

## 4. The escalation channel

**No card may ever sit silently blocked or given up.**

Whenever a card would be blocked, has given up, or is stranded with nobody able
to run it, a human gets a message carrying six things:

1. card id and title
2. which board
3. what happened, **in plain words**
4. the last error
5. the **exact command** to resume it
6. a pointer to the worker log

```
✖ CARD-42 -- Port the notifier to the new board layout
board: project-a

The card hit its consecutive-failure limit and was taken off the board.
It will NOT come back on its own.

last error: `RemoteProtocolError: upstream disconnected mid-stream`
detail: effective_limit=3, failures=3, trigger_outcome=crashed

resume it with:
`hermes kanban unblock CARD-42`

worker log: `~/.hermes/kanban/boards/project-a/logs/CARD-42.log`
```

### What triggers one

| Trigger | Why |
|---|---|
| `gave_up` | The breaker tripped; the card is off the board |
| `blocked` | The card is waiting for a human |
| `spawn_auto_blocked` | The dispatcher could never start a worker |
| `budget_exhausted` | The worker waited out its whole `model_wait_budget` |
| *stranded* | `ready`, unclaimed, and **nobody can run it** |
| *stalled* | `running`, heartbeating, and **going nowhere** |

Bare `crashed` and `timed_out` deliberately do **not** escalate: those drop the
card back to `ready` and it gets picked up again. Escalating them is how a
channel earns a mute, and a muted channel is how a board goes silent a second
time.

### "Stranded" means nobody *can* run it

Age plus `ready` is not enough. A dry run of an earlier version against a live
board produced two false pages, both from that naive rule — which scores one
card without ever looking at the board. Three suppressions fix it:

1. **The human lane.** An assignee that maps to no profile (no directory under
   `HERMES_HOME/profiles/`) is a review gate parked on a person's name. No
   dispatcher will ever claim it; that is the point. One such card had been
   `ready` for five months and was perfectly healthy.
2. **Queued behind capacity.** The board is at `kanban.max_in_progress` **and**
   this card's assignee already has a worker running elsewhere. The card is
   next in line, not stuck. Both halves are required — a full board with *this*
   assignee idle means the card really is being passed over, which is worth a
   page.
3. **An explicit ignore rule** — `escalation.ignore.card_ids` / `.assignees` /
   `.title_patterns` (shell globs, case-insensitive).

Suppressed cards are **reported, not hidden**: each tick's report lists them
with the rule that matched, so a wrongly-quiet card is still visible.

### "Stalled" — the deadlock nothing else looks for

> **A heartbeat means the process lives, not that the work moves. Progress
> needs its own signal.**

The worst case on a real board is not a card that failed. It is a card that is
`running`, with a healthy claim, heartbeating every minute, while the headless
session it dispatched has been hung for hours with zero model traffic. The
board looks perfect. Nothing moves. Nothing reaps it until the `max_runtime`
cap, which can be most of a day.

Two board-side signals, both required:

* `last_heartbeat_at` is **fresh** (within `heartbeat_fresh_within_seconds`).
  A card whose heartbeats *stopped* is a crash, and the dispatcher's own
  `detect_crashed_workers` already owns that — claiming it here would
  double-page.
* **No task event other than `heartbeat`** for longer than
  `stalled_after_seconds` (default 45 m). Heartbeats are excluded precisely
  because they are the signal that lies; every other event kind means
  something moved.

There is an optional third signal. The backend proxy records an
`X-Caller-Purpose` of `hermes:<profile>:<board>/<task_id>` on every request,
which makes *"is this card's worker actually using the box?"* answerable from
outside the agent — fresh heartbeats **and** no board progress **and** no model
traffic is a deadlock rather than a long think. Pass a callable as
`find_stalled(..., activity_probe=...)`; it receives the tag from
`caller_purpose_tag()` and returns True when the backend has seen recent
traffic. It is **injected, not implemented here**: the proxy has its own owner
and a monitoring job has no business dialling it. A probe that raises never
suppresses a stall.

The resume command for a stalled card is `hermes kanban reclaim <id>`, not
`unblock` — the card is running with a live claim, and releasing the claim is
what puts it back in play. The worker pid is in the message so the operator can
kill the hung process.

### Channels

```yaml
escalation:
  channel: mattermost      # mattermost | telegram | ntfy | none
```

There is **no default**. An unset channel is an error, because silence is
precisely the failure mode this block exists to remove. `none` is a valid,
explicit choice — messages are rendered and logged, never sent.

### Secrets

No new secret is invented and no token is hardcoded. Every channel resolves
credentials in the same order, and an unresolvable credential raises an error
naming **every source it tried**:

1. an explicit value in the escalation config
2. an environment variable
3. the OS keychain — the precedent set by the existing kanban ntfy notifier,
   chosen because a launchd job has no TTY and would hang forever on a vault
   unlock prompt
4. *(Mattermost only)* `MATTERMOST_URL` / `MATTERMOST_TOKEN` in the gateway
   profile's env file, read at send time and never logged

Mattermost reuses the bot that already posts in your workspace. ntfy reuses the
same server, topic and keychain-resolved bearer token as the existing notifier.
Telegram reuses an existing bot's token and the owner's chat id. See
[`scripts/resilience/resilience.yaml.example`](../scripts/resilience/resilience.yaml.example)
for the exact keys and the one-time keychain seeding commands.

### One push per stuck card. Never a stream.

> A push must mean **"this is stuck and nothing else will come through"**.

`gave_up` and `blocked` are *events* — one row each, so the event cursor pages
them once. `stranded` and `stalled` are **not events**: they are recomputed
from the card's current state on every tick, and nothing in the cursor stops
them. Measured on a live box before this was fixed: **60 consecutive ticks
produced 60 pushes for one card.** That is how a channel gets muted, and a
muted channel is the second silence.

So every page goes through a **page log** — a small JSON file beside the board
DB (`.escalator_pages.json`, override with `--state`). A card pages once per
`(card_id, kind)`, and again only when:

* `escalation.repeat_after_seconds` has elapsed (default **6 h** — long enough
  that a card stuck overnight is one message rather than six hundred, short
  enough that a still-broken card resurfaces within a working day), **or**
* the card's **state fingerprint changed**. A new worker pid on a stalled card
  is a new hang and deserves a new page.

Four properties that matter more than the mechanism:

| Property | Why |
|---|---|
| Only a **delivered** page is recorded | An undelivered one is retried next tick — the cooldown must never swallow a page nobody received |
| A **dry run** records nothing | Inspecting a tick must not silence the next real page |
| A **corrupt log fails open** | At worst one duplicate message; never a swallowed one |
| Suppressions are **reported** | Each tick lists what stayed quiet and why |

### Narrowing what pushes

```yaml
escalation:
  notify_on: [gave_up, blocked, budget_exhausted, stalled]
```

Unset means all six kinds. Kinds left out are still **reported** in the tick
output — visible, just not pushed.

### Closing the loop

```yaml
escalation:
  send_resolution: true     # default
```

When a card that was paged starts moving again, one closing line is sent and
the log entry is dropped, so the card can page cleanly if it gets stuck later.
Without it a page never ends and the reader has to go and look.

A state-derived page closes as soon as the card is no longer in that condition.
An event-derived page closes when the card's status has both *changed* since
the page and become one a human would call moving (`ready`, `running`, `done`,
`archived`). The change requirement matters: `budget_exhausted` fires on a card
that is already `ready`, and without it every such page would close itself one
tick later for nothing.

### Delivery failures

A failed send is **reported, not raised** — one unreachable channel must not
stop the rest of a tick. The event cursor is left *before* the undelivered
event, so the next tick retries it: a card nobody was told about must not be
forgotten because the channel was down for a minute.

---

## What is not wired yet

The taxonomy and the wait budget are libraries with full test coverage, and the
counting policy and escalation run today through the reconciler. Making the
wait budget take effect **inside the agent's retry loop** needs a small delta to
two upstream files, applied with the pattern in [patches.md](patches.md):

| File | Delta |
|---|---|
| `agent/conversation_loop.py` | Build one `WaitBudget` per API-call block from `agent.model_wait_budget` and the claimed card's `max_runtime_seconds`. Replace the loop condition `while retry_count < max_retries` and the exhaustion guard `if retry_count >= max_retries:` with `budget.should_retry(verdict, retry_count, max_retries)`. Clamp the backoff sleep with `budget.cap_sleep(wait_time)`. Call `budget.reset()` after a successful call. |
| `agent/error_classifier.py` | Optional. The refinement works from outside via `error_policy.refine()`; folding `backend_busy` into `FailoverReason` itself is the cleaner long-term shape and the right thing to propose upstream. |
| `cli.py` | **OPEN DEFECT against the policy "an absent local model is a queue, not a failure" — not an accepted gap. Found in the first real outage (the model box powered off).** When a kanban worker gives up on the backend, it exits `1`; only `failure_reason` `rate_limit` / `billing` get exit `75` (`KANBAN_RATE_LIMIT_EXIT_CODE`), the upstream sentinel that makes the dispatcher requeue the card **without counting a failure**. A dead or busy backend classifies as `timeout`, so it exits `1`, the dispatcher records `crashed` with the text *"pid N exited with code 1"*, and the card loses a life. The reconciler cannot undo it: the backend error never reaches the board — only the exit code does. The fix is one tuple: widen `in ("rate_limit", "billing")` to include the wait-class reasons (`timeout`, `overloaded`, `server_error`, `backend_busy`). The outage is then requeued at the source, atomically, with the respawn guard deferring the retry. |
| `agent/agent_init.py` (interim) | Until the loop consults `WaitBudget`, a one-function patch gets the same outcome for the wait: it sets the loop's retry allowance from `agent.model_wait_budget` — the smallest count whose zero-jitter backoff sum (2 s doubling, 60 s cap) still covers the budget, so no error can surface before the budget ends, and with full jitter it ends by about 1.5×. An unset budget means 6 h on a local backend (`127.0.0.1`, `localhost`, `::1`, `host.docker.internal`, `*.local`) and nothing extra on a remote one; a larger explicit `api_max_retries` wins. It does not tell wait-class from real failures — the non-retryable ones already leave the loop before the count is consulted. |
| heartbeat note | Also from that outage: every kanban heartbeat carries `note: null`, and the worker's retry status is buffered until it exits, so a worker waiting on a dead backend looks exactly like a hung one, and the `stalled` rule cannot tell them apart. Upstream's `heartbeat_task` already accepts a note; passing the retry loop's activity note ("error retry backoff 12/30") would let the escalator say "waiting on the backend" — once per outage, not once per card. |

Both are **temporary local patches** by the rules in [patches.md](patches.md):
derive them against the exact installed agent version, record a `.orig.bak`,
and prefer contributing the fix to
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) over
carrying them.

---

## What a chat user sees while the model is busy: nothing

A gateway profile (Mattermost, Telegram) waits exactly like a worker, and the
chat stays clean while it does. The wait itself is covered above; these keys
keep the gateway's own machinery out of the conversation:

```yaml
agent:
  model_wait_budget: 6h          # no error before this
  stream_silence_limit_s: 21600  # a parked request is not cut and re-queued
display:
  busy_input_mode: queue         # a second message waits its turn instead of
                                 # interrupting a request already in the queue
  busy_ack_enabled: false        # no "queued" / "interrupting" notices
platforms:
  mattermost:
    gateway_restart_notification: false   # no "Gateway shutting down" posts
```

Upstream sends one more lifecycle line no key reaches: a message that arrives
while the gateway drains for a restart gets *"Gateway is restarting and is not
accepting another turn right now"*. The deployment this scaffold came from
patches `gateway/run.py` so that notice follows `busy_ack_enabled` too.

Only when the budget is spent does the user hear anything, once: the provider
failure reply.

---

## See also

- [`scripts/resilience/README.md`](../scripts/resilience/README.md) — the
  mini-app: files, install, verify
- [docs/kanban-harness.md](kanban-harness.md) — the transition matrix the
  escalated cards move through
- [docs/patches.md](patches.md) — the patch pattern and its risks
