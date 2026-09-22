# Board design note — implementing `config/board.yaml`

> `config/board.yaml` is the specification; this note records how the
> implementation answers to it, the decisions that had to be made, and why.
> Every open question it raised (D1–D10) is now **resolved** — the table at
> the end says how. The spec lints with **zero warnings**
> (`python3 scripts/resilience/board.py config/board.yaml`).

## The fixed ground

The nine status values in `hermes_cli/kanban_db.py` (`VALID_STATUSES`) are
validated on every write. The user's columns are therefore a **labelling and
policy layer** over them, never a schema change. Every column states its status
explicitly in `board.yaml`:

| Column        | Status      | Why this one |
|---|---|---|
| refinement    | `triage`    | the pre-work bucket; nothing dispatches from it |
| waiting       | `todo`      | upstream `recompute_ready` already promotes `todo → ready` when every parent finishes |
| to_do         | `ready`     | the **only** status the dispatcher claims from |
| in_progress   | `running`   | |
| question      | `blocked`   | a person must decide before work continues |
| user_review   | `scheduled` | finished work waiting for the person — see **user_review is a status, not a name** below |
| done          | `done`      | reachable only through the user's `accept` from user_review |
| archived      | `archived`  | |
| *(unused)*    | `review`    | the runtime's **agent** review lane — deliberately not used by this board |

## user_review is a status, not a name

The runtime already owns a status called `review`, and it is the opposite of
what this board means by "waiting for the user": the dispatcher claims every
`review` card assigned to a real profile and **spawns that agent as a
reviewer that may merge and set `done`**. Naming our column `user_review`
separates the two for people. Only the **status** separates them for the
runtime, because the dispatcher reads statuses, never labels.

`user_review` lives on `scheduled`, which upstream documents as *"intentionally
not dispatchable"*. Verified against the real `kanban_db`, not read off the
code:

| On a card in … | `scheduled` (user_review) | `review` (agent lane) |
|---|---|---|
| dispatcher, assignee is a **real profile** | spawns **nothing** | spawned `coder` as a reviewer |
| `complete_task` → done | **refused** | refused |
| `unblock_task` → ready | works | refused |
| `recompute_ready` | leaves it alone | leaves it alone |

Two consequences carry the design:

1. **`accept` is the only door to done — by construction.** The runtime has
   no move from `scheduled` to `done`, and no agent is granted `complete`, so
   the user's own `accept` (`scripts/resilience/board_cli.py`) is the single
   way in. A test pins upstream's refusal so an upgrade that changed it would
   fail loudly.
2. **"The user has no profile" is no longer load-bearing.** Even a card
   mis-assigned to a real profile cannot have an agent spawned for it while it
   waits in user_review.

The runtime's `review` status is left empty. The escalator pages at once if a
card ever sits there assigned to a real profile — our board never puts one
there, so a hit means something else did.

## Two files, composed at load

| | `board.yaml` | `harness.yaml` |
|---|---|---|
| Answers | **what moves exist** | **who may make them** |
| Reader | the user — their words, their columns | the operator — profiles, side-door regexes |
| Owns | columns, statuses, edges, `requires`, `requires_to_leave`, `auto_advance`, `archiving`, role semantics | role → profile binding, action grants, `always_allow`, `side_doors`, `mode` |

A move is legal only when **both** agree, and that is checked **at load and
at transition time**:

- *At load:* every role's `may_not` in `board.yaml` is checked against the
  harness grant table; human lanes must be human in both files; every hand-off
  must land on a column. Disagreement makes the harness refuse to load, and it
  then fails closed on every kanban mutation.
- *At each hand-off:* the move must be an **edge** on the board, the source
  column's `requires_to_leave` must hold, and the target column's `requires`
  must hold. Each refusal names the rule and teaches the move that satisfies it.

Not one file: that would put deny-regexes for `kanban.db` into the user's own
workflow specification. Not generated: a generated file silently diverges the
first time someone hand-edits it. Composition is **opt-in** — only a deployed
board (`KANBAN_BOARD_FILE` or `$HERMES_HOME/board.yaml`) is composed, never the
repository copy by fallback.

**Ruling — no agent reaches `done`, ever, not even for a child card.** The
harness once shipped a commented "sub-task gate" letting QA complete child
cards; `board.yaml` says `qa: may_not: [done]`, and the board wins. The gate is
gone from the shipped defaults, a test pins the template to grant no agent
`complete`, and with a board deployed any such row makes the harness refuse to
load. Children parking with the user is answered by making accept cheap:
`accept --children <parent>` takes every waiting child in one command.

## `requires` and `requires_to_leave`

`requires` is checked to **enter** a column; `requires_to_leave` to **leave**
one. A card is born in refinement, so its acceptance-criteria rule is a
`requires_to_leave`; everywhere else the condition guards the way in.

| Condition | Holds when |
|---|---|
| `comment` | the move carries a non-empty summary |
| `assignee: <role>` | the card lands on that role's assignee (the role is resolved through the harness binding) |
| `references_card` | the card has a real dependency link (a parent) |
| `acceptance_criteria` | the card body has a heading or line starting `Acceptance criteria` |

An unknown condition **fails closed** and says so.

The gate sees **agent** moves only (it is a `pre_tool_call` hook). A human at
a terminal is the board's owner using their own tool; the rule exists to stop
agents closing their own work. So for humans the answer is detection, not
prevention: the escalator pages when a card reaches `done` without an
`accept`. That was a deliberate architectural choice — prevention there would
need a fork of the agent.

## `references_card` is a real dependency link

Not a mention in prose — a prose mention also appears in "unlike t_42, this
one…", and a false link is a card that never leaves `waiting`. `waiting` is
`todo` underneath, which upstream already promotes when every parent finishes.
The reconciler closes the gaps upstream leaves:

- **`user_review` arrival.** Upstream promotes only on `done`/`archived`; the
  spec also waits for `user_review`, so the reconciler moves those itself.
- **The comment.** Upstream writes a bare `promoted` event; the reconciler
  always writes the explaining comment, and backfills it when upstream's own
  promoter wins the race on `done`.
- **All parents.** A card waiting on two things leaves only when both arrived.

A card in the runtime's agent `review` lane has **not** arrived: an agent
reviewer still working on it is not the user having it.

## Archiving

```yaml
archiving:
  clock: {archives: done_only, after: 30d}
  human_may_archive: from_any_column_listing_archived
```

The clock archives `done` cards and nothing else, and never invents a
`done → archived` edge the board lacks. A person may archive from any column
that lists `archived` as an exit: throwing away a bad idea in refinement, or a
question that turned out moot, is a decision, not a sweep. Values this build
cannot carry out are load errors, not silent no-ops.

## Resolutions

| # | Question | Resolved as |
|---|---|---|
| D1 | `requires` had no direction | `requires` = to enter, `requires_to_leave` = to leave |
| D2 | `never_archive_unless_done` vs `archived` exits | the clock archives done only; humans may archive from any column listing it |
| D3 | the `todo` key collided with the `todo` status | key renamed `to_do`; every column states its `status:` |
| D4 | upstream auto-splits `triage` cards | documented beside `kanban.auto_decompose` in `config.yaml.example`: turn it off when you deploy this board, or refinement cards are split before anyone writes their criteria. The scaffold default is unchanged, since boards without refinement are unaffected |
| D5 | the gate cannot see a human's moves | enforce for agents; detect for humans (`done` without `accept` pages) |
| D6 | "acceptance criteria present" undefined | a heading or line starting `Acceptance criteria` |
| D7 | JSON cannot carry an origin header | JSON skipped by `check-origin-headers.sh`, with the reason |
| D8 | shebang must stay on line 1 | header goes on line 2 of shell templates |
| D9 | the user could not leave `review`; upstream's review lane spawns agents | `user_review` on `scheduled`; `accept` / `rework` / `accept --children` shipped; alarm on a `review` card assigned to a real profile |
| D10 | origin headers in `SOUL.md` / `MEMORY.md` reach the model | placed inside each file's existing comment style; small, known cost |

## What is built

| Part | State |
|---|---|
| Loader, column → status map, validator (zero warnings on the real spec) | built |
| Composition of `board.yaml` with `harness.yaml`, at load and at each hand-off | built |
| `requires` / `requires_to_leave` on agent hand-offs, with teaching refusals | built |
| QA's hand-off lands in user_review (`scheduled`); hand-off guard widened to `ready \| blocked \| scheduled` and no further | built |
| `accept` (only door to done), `accept --children`, `rework` (reason required, back to whoever did it) | built |
| Agents denied the accept/rework command as a side door, and the command refuses to run inside a worker | built |
| `auto_advance` out of waiting; clock archiving of done cards — opt-in (`escalator.py --board-file`) | built |
| Escalator alarms: `done` without `accept`; a `review` card on a real profile — opt-in with the board | built |
| Origin headers on 32 of 33 non-JSON templates | `scripts/memory/vault-hindsight-sync.example.sh` still needs its two lines |

Nothing in this build touches `~/.hermes`, the live profiles, or a live board.
Tests run against the real `kanban_db` in a scratch `HERMES_HOME`.
