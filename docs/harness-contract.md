<!-- @user-gated -->
# The harness contract

> **@user-gated.** This file, the code that enforces it and the tests that prove it
> change only by the user's hand, in the user's own session. No test in it is ever
> skipped, marked xfail, narrowed, renamed into something softer, or "temporarily"
> disabled. A test that is inconvenient is the test doing its job.

The kanban harness is a **state machine with default deny**. Every card move is a
triple `(from, to, actor)`. The table below lists the permitted triples; **anything not
listed is refused**. `done` is only the most expensive cell in that table.

**Baseline in code, configuration only stricter.** A config file, an environment
variable or a per-board entry may add a role, a requirement or a deny pattern, or narrow
a grant. It can never remove a refusal. There is no mode, flag, env var, per-board entry
or profile setting that turns a refusal into a log line, exempts a board or a profile, or
switches the harness off.

## Who counts as whom

- **Agent**: any process with `HERMES_KANBAN_TASK` set, or `HERMES_PROFILE` set, or
  `HERMES_HOME` set to anything other than `~/.hermes`, or stdin not a TTY. That covers
  orchestrator and chat sessions: a profile with a model behind it is an agent. When the
  caller can't be positively identified as the user's hand, it is an agent.
- **User's hand**: none of the above, and no Hermes process (worker, dispatcher,
  gateway, agent) among the caller's ancestors. The cost is deliberate: **`accept`,
  `rework` and `reopen` can't be scripted or piped.** That isn't a bug.

**What this check establishes, and what it does not.** It establishes that the caller
is not a Hermes agent by any ordinary route: no task or profile variable, not a
profile's home, not spawned by a worker or the dispatcher, and not a pipe or a tool
call. It does **not** prove a human is at the keyboard. A terminal can be allocated by
automation, and a process can be detached from its parents; a caller that sets out to
get round the check can. "TTY-gated" means "not an agent by accident or by habit", not
"only a human can do this". If this ever guards something that matters more than a
kanban column, the stronger option is a one-time confirmation the user types, which
the caller cannot predict. That isn't built: it would cost every accept a step.
- **Machine**: `dispatcher` (upstream claim, spawn, requeue, breaker) and `reconciler`
  (this repo's escalator: `auto_advance`, the archive clock).
- The harness **prevents** agent moves. Machine and human moves are outside its reach,
  so they're **audited**: every status change in the event log is checked against the
  table, and a triple not in it pages once.

## The table (the shipped default)

Statuses: refinement=`triage` · waiting=`todo` · to do=`ready` · in progress=`running` ·
question=`blocked` · user review=`scheduled` · done · archived. The runtime's `review`
status is used by nothing on this board.

| from | to | actor | condition |
|---|---|---|---|
| refinement | waiting | user | card has a parent link |
| refinement | to do | user | acceptance criteria present |
| refinement | archived | user | reason comment |
| waiting | to do | reconciler | every parent reached user review or done; comment written |
| waiting | to do | user | |
| to do | in progress | dispatcher | claim + spawn the assignee |
| in progress | to do | coding | hand-off to qa, summary required |
| in progress | to do | qa | rework to coding, reason required |
| in progress | to do | dispatcher | crash / timeout / reclaim requeue |
| in progress | question | coding | comment required; assignee → user |
| in progress | question | qa | comment required; assignee → user |
| in progress | question | dispatcher | breaker auto-block; the escalator pages |
| in progress | question | reconciler | the run exhausted its budget or crashed for a reason that was not waiting; retry limit 1, so on the first occurrence; a wait-class failure is a queue and never counts |
| refinement | to do | reconciler | split card born from that block; assignee planner; carries the blocked card as parent, its run history and its last hand-over verbatim |
| refinement | to do | planner | a child of its cut; acceptance criteria, parent link, budget estimate in turns and hand-over form present; assignee a lane |
| in progress | user review | planner | cut handed off; summary names the children and what made the original too big; assignee → user |
| in progress | question | planner | it lacks what it needs to cut; comment required; assignee → user |
| in progress | user review | qa | summary required; assignee → user |
| question | to do | user | |
| question | waiting | user | parent link required |
| question | archived | user | reason comment |
| user review | done | user | **accept only**; writes the trace |
| user review | to do | user | rework; reason required; back to whoever did the work |
| done | archived | user | |
| done | archived | reconciler | done ≥ 30 days; the clock archives done cards only |
| done | user review | user | reopen; **only when there is no accept trace**; reason required |
| archived | user review | user | reopen; **only when there is no accept trace**; reason required |

Other agent rules:
- **No standalone assignee change.** `assign` / `reassign` are refused for every agent.
  The assignee changes only as part of a hand-off row above.
- **`link`**: an agent may link only its own card and cards it created in its own
  fan-out. **`unlink` is refused** for agents.
- `scout` has **no moves** until its contract is built. `planner` has exactly the rows
  the table gives it. A role may be granted moves only if `board.yaml` declares it under
  `roles:`, and the harness refuses to load a grant for an undeclared role (T39).

**What the table records but cannot check, said plainly:**
- A card born directly in `to do` (the planner card the reconciler cuts from a blocked
  card) has no earlier status, so the audit cannot check its birth row. That row records
  intent, not enforcement.
- Retry limit 1 holds only as tightly as the reconciler's tick allows. The dispatcher
  requeues a failed card at once and may claim it again before the next tick blocks it,
  so a card can get one extra attempt in that window. A run count of 2 on a blocked card
  is that window, not the rule failing.

**Source of truth:** this table lives in `config/board.yaml` as `moves:`
(`{from, to, by, when}` rows), read by the harness, the audit and the T2 generator. The
table above is its readable copy, and a test fails when the two disagree (T31).
`board.yaml` carries `@user-gated`. A `board.yaml` that is missing, unreadable or fails
to parse means **refuse**. There is no fallback to a default table.

*Signed off by the spec owner, all 22 rows and the agent rules, 2026-09-21.*

## The accept trace

Every `accept` writes **two** records. Upstream's `gc_events()` deletes the events of old
done and archived cards, so the event alone erodes on exactly the cards it protects:

1. an event: `kind=completed`, payload `{"by": "accept", "actor": "user"}`;
2. one line in the board's **accept ledger**, `<board kanban.db>.accepts.jsonl`, append
   only, written only by `board_cli.py`:
   `{"card": "t_…", "board": "…", "at": 1790000000, "by": "user", "uid": 501}`.
   The file name matches the built-in `kanban\.db` deny pattern, so no agent can reach it.

`reopen` and the escalator read **the event or the ledger line, never the comment**. A
missing ledger on a board with a deployed spec is a **fault that pages**. It never reads
as "no accepts".

## Tests

Every test runs in one scratch `HOME` + `HERMES_HOME`, against the real `kanban_db` and
the real `pre_tool_call` pipeline, and in **one process** with every other suite (T23).

### Fixtures

| Fixture | How it's built |
|---|---|
| `Worker(card, profile)` | env `HERMES_KANBAN_TASK=card`, `HERMES_PROFILE=profile`, `HERMES_HOME=<scratch>/profiles/<profile>` |
| worker tool call | `get_pre_tool_call_block_message(tool, payload)` + `model_tools.handle_function_call(tool, payload)` |
| worker shell | the same, with tool `terminal` and `{"command": "<literal>"}` |
| user's hand | `board_cli.py` in a subprocess with **no** Hermes env, stdin a real pty (`os.openpty`) |
| not the user's hand | the same subprocess with stdin `/dev/null`, or with one Hermes variable set |
| board state | `kb.get_task(conn, id).status` read from a **fresh** connection after the call |
| refusal on disk | `<scratch HOME>/.hermes/logs/kanban-harness.log`, only the bytes written by this call |
| GC run | `kb.gc_events(conn, …)` with a cutoff past the card's events |
| harness not loaded | a scratch profile whose config lists `kanban-harness` but has no `plugins/kanban-harness` link |
| permissive table | a hand-written YAML granting `complete`, with `mode`, `enabled`, `side_doors.enabled: false`, `unknown_profile: allow`, a mutating verb in `always_allow` |

"Refused" always means all three: the call returns a refusal naming the role, the
attempted move and the permitted move; the board state is **unchanged**; and a `[BLOCKED]`
line for that tool is on disk.

### The list

| # | Call and environment | Expected |
|---|---|---|
| T1 ✅ | `Worker(t, coder)`: `kanban_complete {task_id: t}`, with the template config, **and again with the live config as of 17:24** (`mode: warn`) | refused; `t` still `running`; `[BLOCKED] tool=kanban_complete` logged. **Red** at HEAD (the 17:24 config let it through); green now |
| T2 ✅ | **Generated.** For every status `f` in the 9, every status `t ≠ f`, every agent role (coding, qa, and an unknown profile): put a card in `f`, then try each agent route that lands in `t` (`kanban_complete`→done, `kanban_block`→blocked, `kanban_unblock`→ready, `kanban_handoff`→its row's status, shell `hermes kanban schedule/promote/archive/unblock/block/complete`) | refused unless `(f, t, role)` is a table row **and** the route is that row's route. Permitted set = table rows with an agent actor; everything else is subtracted from the full product. A new status or role enters the product automatically |
| T3 ✅ | permissive table as `KANBAN_HARNESS_FILE`; `Worker`: `kanban_complete`, shell `hermes kanban complete t`, `terminal: sqlite3 …/kanban.db` | the config fails to load; every call is refused; board unchanged |
| T4 ✅ | one config at a time, each adding exactly one of: `mode`, `enabled`, `side_doors.enabled: false`, `unknown_profile: allow`; plus env `KANBAN_HARNESS_MODE=warn` | load fails naming the key; `kanban_complete` refused. The env var has no effect |
| T5 ✅ | board `unlisted-board` absent from `boards:`; `Worker`: `kanban_complete` on it | refused (every board is harnessed) |
| T6 ✅ (green with no new code) | `KANBAN_HARNESS_FILE` and `KANBAN_BOARD_FILE` both pointed at the permissive files | refused |
| T7 ✅ (green with no new code: only one config source is ever read) | home config strict, profile config permissive (and the reverse) | refused both ways: the strictest wins |
| T8 ✅ | `always_allow.shell_verbs: [complete]` | load fails: a mutating verb in `always_allow` |
| T9 ✅ | a config whose `side_doors.deny_patterns` omits `kanban\.db`; shell `sqlite3 ~/.hermes/kanban.db "update tasks set status='done'"` | refused: the built-in patterns can't be removed |
| T10 ✅ (green with no new code) | done routes for an agent, each separately: tool `kanban_complete`; shell `hermes kanban complete t`; shell `python3 …/board_cli.py accept t`; shell `curl -X POST http://127.0.0.1:9119/api/plugins/kanban/…`; shell `sqlite3 …kanban.db …` | each refused; `t` not `done` |
| T11 ✅ (green with no new code) | card in runtime `review`, assignee a real profile; the spawned reviewer calls `kanban_complete` and `hermes kanban complete` | refused; the card never reaches done; the escalator pages the card once (`agent_review_lane`). The **spawn itself** is upstream-internal and listed UNPREVENTED below |
| T12 ✅ (green with no new code) | as an agent, try to produce an accept trace: `kanban_complete` with `{"by": "accept"}` in the summary or metadata; shell `hermes kanban edit t …`; append to the ledger path | refused; no `completed` event with `by=accept`; ledger unchanged |
| T13 ✅ | user `accept t`; then `gc_events` past the window | ledger line survives; `reopen t` refused; the escalator doesn't page `t` |
| T14 ✅ | user's hand `reopen t --reason "…"` on done-without-accept | `t` → `scheduled`, assignee `user`; same id; earlier comments and events kept; one `[reopen]` comment with the reason |
| T15 ✅ | the same on archived-without-accept | → `scheduled` |
| T16 ✅ | `reopen` on an accepted card | refused; the message carries the accept's timestamp |
| T17 ✅ | `reopen` with stdin `/dev/null`; with `HERMES_PROFILE=coding`; with `HERMES_HOME=<profile dir>`; from a worker shell | each refused, board unchanged |
| T18 ✅ | `reopen t` twice | second run exits 0, no second comment, one status change |
| T19 ✅ | fail-closed install check over the "harness not loaded" profile | reports the fault and the escalator pages once. It checks the plugin is **loaded** (resolved and importable), not just listed |
| T20 ✅ (the list was built from the first scan, so it did not go red first; from here a new or vanished route fails) | **Route inventory.** (The "vanished route fails" half is deliberate: on an upgrade someone decides whether a route moved or was removed. That decision is the review this test forces.) AST scan of the installed agent (`hermes_cli/`, `tools/`, `gateway/`, `cron/`) for every function containing SQL that sets `status` to `'done'` or `'archived'`, and every call to `complete_task` / `archive_task` | the found set of `module:function` equals `tests/fixtures/terminal-routes.yaml` exactly, where each entry carries a one-line reason. A **new** route fails; a **vanished** route fails too, so the list stays true |
| T21 ✅ (compares snapshots per tick: a move and its reversal inside one tick is not seen) | the event log holds a status change not in the table (e.g. `running → done` by `complete`) | the audit pages once |
| T22 ✅ (test_board.py TestBoardAlarms) | the user's bare `hermes kanban complete t` (no accept) | pages once, exactly as for an agent's |
| T23 ✅ | every suite in one pytest process, in both orders | all green; the real `~/.hermes` untouched |
| T24 ✅ (green with no new code) | worker shell, each separately: `hermes kanban schedule t`, `block t`, `unblock t`, `promote t`, `assign t x`, `reassign t x`, `unlink a b`, `archive t` | refused, board unchanged. Note: the runtime's own `unblock` accepts `scheduled`, so without this a worker could pull a card out of user review |
| T25 ✅ (green with no new code) | worker shell `hermes kanban complete t_ok t_forbidden` | the **whole** batch refused; neither card moves |
| T26 ✅ | any refusal | the message names the role, the attempted move and the permitted move, and says the refusal is final: do not retry. No "try again", "temporary" or "later". **Not retryable in shape**: the tool result classifies as a permanent failure, never as a wait-class reason (`timeout`, `overloaded`, `server_error`, `backend_busy`, `rate_limit`) that the worker's retry logic would wait out |
| T27 ✅ | the loaded harness is **the** gated one: its resolved `__init__.py` path is the canonical `hermes-standalone/plugins/kanban-harness`, and its file starts with `@user-gated` | a copy dropped into a profile fails the install check and pages |
| T28 ✅ | the audit over 60 ticks with one illegal triple, plus the reconciler's own legal moves | one page for the illegal triple; none for the reconciler |
| T29 ✅ | `kanban_create` with an idempotency key that already belongs to a done or archived card | **refused**, naming the terminal card. No card is created and none is handed back |
| T30 ✅ | delete the accept ledger on a board that has accepted cards; run the escalator tick; `reopen` any card on that board | the escalator pages a **fault**; `reopen` refuses every card on the board until the ledger is restored |
| T31 ✅ | parse `moves:` from `config/board.yaml` and the table in this file | identical sets of `(from, to, actor)`; a drift fails |
| T33 | the installed agent source | both agent patches are present (terminal writes refuse agents; the user's own moves -- terminal, web dashboard -- go through and a non-accept done pages; kanban mutation tools refuse without the harness registered). An upgrade that drops either one fails |
| T36 ✅ | `reopen` from a real terminal whose parent is a Hermes process (an interpreter at `…/hermes-agent/venv/bin/hermes`), with a control launch from a non-Hermes parent | refused, board unchanged, refusal on disk; the control succeeds. A terminal does not change who spawned you. (Not identity: see "What this check establishes") |
| T35 ✅ | the real `scripts/test.sh` over fake layers, one of which calls `exit 0` part-way | the gate fails with "gate incomplete". Any green reported by `test.sh smoke` before this fix is **void**: a sourced layer's `exit` ended the run early with status 0 |
| T34 ✅ | the escalator has seen N ledger lines on a board; the ledger is emptied or truncated (as after GC plus a deletion and re-creation by hand) | the escalator pages `ledger_shrank` as a fault. The high-water mark lives in the escalator's own state, only ever rises, and is out of reach of the ledger's deletion |
| T32 ✅ (green with no new code: composition already failed closed) | `KANBAN_BOARD_FILE` pointed at a missing file, an unreadable file, and invalid YAML; `Worker`: `kanban_handoff` | refused each time; no default table is used |
| T40 ✅ | generated: every form that hides a shell command's program word (bare and braced variable, `eval`, `exec $H`, backticks, `$(…)`, `sh -c`/`bash -c` bodies, `env A=1 $H`, `xargs $H`) × the verb literal and split (`${v}${w}`), plus a literal `hermes` given an unreadable board or verb, or `xargs hermes` | refused, board unchanged, on disk, and the refusal says "write the command literally". No keyword: a hidden program word is refused whatever it runs. Literal wrappers (`env`, `timeout`, `nohup`, `xargs grep`, `bash -c 'pytest -q'`) and variables in ordinary arguments are resolved, not refused. Observed before the fix on a scratch board: 11 forms moved a card from running to done |
| T41 ✅ | `bash x.sh` where the script runs `hermes kanban complete` | **not** refused: pinned, so the day it changes the contract must change with it. See "Unprevented routes" |
| T42 ✅ | `execute_code` running `subprocess.run([h, "kanban", "complete", id])` | **not** refused: pinned, as T41. What the code route does catch (`kanban_db` import, the database file) stays refused, and the shell rule never parses Python |
| T43 ✅ | `kanban.auto_decompose` in `$HERMES_HOME/config.yaml` true, absent, or the file missing; and every shipped `config.yaml.example` | a config load error, so the harness locks down; only an explicit `false` loads. Every example says `false` |
| T44 ✅ | a board whose `may_not`, `hands_off_to`, `rework_to`, `owns` or `human` forbids a move the harness grants; a role key outside the closed list | the load fails naming the key: the stricter file wins, and a key nothing reads cannot sit in `board.yaml` looking like a rule |
| T46 ✅ | `execute_code` importing `hermes_cli.kanban_swarm`, `hermes_cli.kanban` or `tools.kanban_tools`, or an assembled name via `importlib` | **not** refused: pinned, and expected to fail the day the hole is closed. See "Unprevented routes", In-process Python |
| T47 ✅ | a hand-off whose `Files:` line names a missing file, an empty file, a five-byte `hello`, a markdown file of only headings, a table with no rows, an unreadable or non-UTF-8 markdown file, a directory, prose, `/etc/hosts`, a symlink out of the tree, or any path with no workspace; and a real findings file, a small binary, `Files: none` with no writes, a bulleted list, a file the run deleted | the first group refused, final, board unchanged, on disk, naming the path and saying what would satisfy it; the second passes. Red first: every refusal went through. T38's "extra listed paths are fine" was inverted by this row on purpose. See "Placeholder deliverables" |
| T45 ✅ (tests/smoke/test_visible_wait.py, test_resilience.py TestBackendNotServing) | the generated visible-wait patch: a worker's request receives nothing; ticks of the wait loop; a retry; the first bytes; a request silent past the bound; three workers on one endpoint and model | the card's heartbeat does not move and the claim is extended; one note on the card, updated in place, names endpoint, model, attempt and duration and opens with a sentence; bytes close it; the silent request ends with its reason on the card while status, failure count and runs stay as they were; the escalator pages `backend_not_serving` once per endpoint and model, closes it when the waits end, and never pages a waiting worker as stalled. Red first: a waiting worker was paged as `stalled` |

## A wait is visible

A heartbeat means progress, not existence. The card's `last_heartbeat_at` moves
only when the worker receives something from the model or does work between
requests. While a request has received nothing, the worker is waiting, and the
card says so in one comment, updated in place, that a person can act on:
`Waiting on the model backend: 127.0.0.1:<port> (model <name>) has sent nothing
for 14m (attempt 9 of 30). This is a wait, not a failure: …`. The first bytes
close it. The card's title never changes.

A wait is not a failure. The worker's claim is extended on every tick, so the
dispatcher does not reclaim it; the worker keeps retrying in place; the card's
status, failure count and retry budget are untouched. The worker is requeued
only when its process is gone, which is the dispatcher's own rule.

A worker that receives nothing for longer than the silence bound ends that
request and records the reason on its card; a wait never consumes the retry
budget. The bound is `stream_silence_limit_s`: a **per-request silence bound,
not a task timeout**. Default 300 s, local endpoints included. Set it with
`agent.stream_silence_limit_s` in `$HERMES_HOME/config.yaml` or
`HERMES_STREAM_SILENCE_LIMIT_S`. No value means unbounded: zero, negative,
infinite or unreadable falls back to 300.

The escalator pages `backend_not_serving` once per endpoint and model when a
worker has waited on it for five minutes, whatever the number of workers, and
closes the page when the waits end. A waiting worker is never paged as stalled.

This lives in the agent, so it ships as a generated patch
(`scripts/harness-patches/build.py`, marker `kanban-harness-patch: visible-wait`,
over `agent/chat_completion_helpers.py` and `tools/kanban_tools.py`) and lands
with the other agent patches. It covers the streaming request path; the
non-streaming path keeps upstream's behaviour. Tested by T45.

The patch is applied; the install's `reapply-hermes-patches.sh --status` names
its marker. The escalator pages only the boards it is configured to read: a
board outside its `--board` list gets the note on the card but no page.

## A hard gate blocks. It does not report.

Every state transition is forced. An agent that attempts a move the table doesn't
allow gets **`command blocked`** back in its own tool result, at the moment of the
call, and the board never moves. A test is green only on **three assertions**:

1. the agent received the refusal, with the table's reason and the one move that is
   allowed from here;
2. the board is unchanged;
3. the refusal is on disk at `~/.hermes/logs/kanban-harness.log`.

The harness is not a reporting layer. These are all the same defect: a mode that logs
instead of refusing; a route where the agent isn't blocked; a page instead of a
refusal; a repair the user performs afterwards; and any word that means "the move
happens and we write it down". Detection covers the escapes nobody foresaw. It is
never the answer for one we did. `reopen` exists for the unforeseen, not as a workflow.

## Every route to a terminal state, and whether it's prevented

| Route | Status | Owner | Waiting on |
|---|---|---|---|
| An agent's tool call or shell verb (`complete`, `block`, `unblock`, `schedule`, `promote`, `archive`, `assign`, `unlink`, `board_cli`, `kanban.db`, dashboard API) | **PREVENTED** by the harness (T2–T12, T24, T25) | hermes-standalone | — |
| The user's own `complete`, `unblock` or dashboard | **Not a gate case**: the user is the gate. It pages as loudly as an agent escape (T22), with no special case for "it was the user" | — | — |
| The dispatcher spawning a reviewer for a card in the runtime's `review` status on a real profile | **UNPREVENTED** (the spawned reviewer still cannot reach done: T11; the escalator pages it) | hermes (agent patch) | `/hermes-e2e` |
| Upstream internals and crons that write `done` or `archived` without an actor | **UNPREVENTED** | hermes (agent patch) | `/hermes-e2e`: a patch makes the terminal writes refuse agents; the user's own moves (terminal, web dashboard) go through and a non-accept done pages. Queued with the wait-class exit patch |
| A profile where the harness isn't loaded | **PREVENTED** by the fail-closed agent patch: every kanban mutation tool refuses while the harness hasn't registered (T33b). The patch is applied; the install's `reapply-hermes-patches.sh --status` shows it (T33c). Covered by test; not observed live | hermes (agent patch) | — |

Until the two UNPREVENTED rows land, each escape through them pages as a **fault**, every
time, never in the routine stream. Once they land, a test asserts the patch is
**installed** (T33), so an upgrade that drops it fails the suite.

Every page above depends on an escalator reading the board. An escalator job
reads exactly one board (its `--board`). A board with no escalator job of its
own is never paged: not stranded, not stalled, not `backend_not_serving`, not
a fault. The cards on it still carry their notes; nobody is told.

## Placeholder deliverables

Recorded 2026-09-22 (T47). A card was completed over a findings file of five
bytes: `hello`. The form had a `Files:` field and nothing had ever looked inside
the files it named.

`Files:` now takes paths and nothing else: one per line or comma separated, list
bullets and backticks stripped, or the single word `none`. Each path must resolve,
symlinks followed, inside the run's workspace or its git top, and hold the work.
Refused: a path that does not exist (unless the run deleted a file tracked at
HEAD), a directory, a path outside the tree, a file that cannot be read, a
markdown file that is not UTF-8, a file under **64 bytes** once whitespace is
stripped, a markdown file of only headings, a markdown table with a header and no
rows. Fail closed throughout. With no workspace there is no tree, so no path can
be trusted and only `Files: none` passes; do not "fix" that by defaulting the
workspace to the current directory, which hands back `Files: /etc/hosts`.

The threshold is a byte count, and a byte count is crude. 64 catches a
placeholder (`hello`, `TODO`, `wip`, a lone heading) and a legitimately tiny file
too: an empty `__init__.py` is refused, and the refusal says a docstring
satisfies it. There is no allowlist: an allowlist is a backdoor with a friendly
name. It does not detect bad work. A 65-byte lie passes, and so does any
full-length, well-formed file that is wrong. The structural checks read markdown
only and say nothing about whether a row is true.

## Unprevented routes

Recorded 2026-09-22. These routes move a card past the table and the harness
cannot refuse them. They are written here in plain words so that nobody reads
the table above as a promise it cannot keep. T41, T42 and T46 pin each one: the day a
route becomes prevented, its test fails and this section changes with it.

- **A script.** `bash x.sh`, where the script runs `hermes kanban complete`. The
  gate inspects the command it is handed; it does not read files that command
  will go on to run. Observed 2026-09-22: a scratch card went from running to
  done.
- **Code execution.** `execute_code` running
  `subprocess.run([h, "kanban", "complete", id])` with `h` from a variable.
  Python has no program word to resolve, and a check for the quoted string
  `"kanban"` is a keyword filter that `"kan" + "ban"` defeats. Observed
  2026-09-22: running to done. The literal string `kanban_db`, and the database
  path, are refused wherever they appear in a command (T42). That is a text
  match: it catches those spellings and nothing else. It says nothing about
  reaching the same code under another name — see In-process Python below.
- **In-process Python.** Recorded 2026-09-22 (T46). Code execution runs
  arbitrary Python inside the worker's process, and the board is a Python API
  plus a file. Proven to pass the gate: `from hermes_cli import kanban_swarm`,
  `import hermes_cli.kanban as k; k.main(['create', ...])`, and
  `from tools.kanban_tools import ...`. These are a class, not a list:
  `__import__`, `importlib.import_module("hermes" + "_cli.kanban")`, or a module
  that imports another reach the same code, so any in-process Python reaches
  the board. The enforcement point is therefore never the command text, and
  never another deny-list entry. It is one of two: the profile does not hold
  `code_execution` at all (the planner holds none, which is why it is outside
  this hole), or the board refuses the write at the API or database layer, on
  the caller's identity, where an import cannot route around it.
- **A plausible lie on the Files line.** Recorded 2026-09-22 (T47). A findings
  file whose table contradicts its own appended JSON passes every check here:
  it is full-length and well-formed. See "Placeholder deliverables".
- **Anything reachable from a shell we grant.** The shell rule (T40) refuses a
  command it cannot read. It does not and cannot know what every readable
  program does once it runs.

These are one defect seen from four sides, and they end at the same sentence
(`docs/enforcement.md`):
**an agent that must not move cards should not hold a tool that can.** The fix is to withhold the tool, never another check on its
text. Today the shipped `coding` role holds both a terminal and code execution.
Narrowing that is the user's decision, because those workers run scripts as part
of the work. The trade-off is set out for the user; this file has not
decided it.
