# Kanban workflow harness

A regular workflow (a skill, a SOUL, the worker prompt) *asks* an agent to follow the
board's process. An agent can still ignore it. One real case: a coding worker marked its
batch done with a confident summary while 4 of the 10 rows in its findings file were
wrong, and nothing on the board could stop that. The **harness** is enforcement. Moves
the matrix forbids are refused in code before the tool runs, so an agent cannot make
them, whatever its prompt says.

It ships as the [`kanban-harness`](../plugins/kanban-harness/) governance plugin. The
plugin has **no workflow built in**. It interprets a transition table in
`plugins/kanban-harness/harness.yaml`, per board and per role. Every move an agent may
make is a row in that table; anything without a row is refused.

## The default matrix

The shipped `harness.yaml.example` encodes this:

| From | To | Who | How |
|---|---|---|---|
| first work column | `ready` | dispatcher | automatic promotion (deps resolved) |
| `ready` | `running` | dispatcher | spawns the assignee's profile; only roles with a profile are ever spawned |
| `running` (coding) | `ready`, assignee = QA | coding agent | `kanban_handoff(summary)` |
| `running` (QA) | `blocked`, assignee = architect | QA agent | `kanban_handoff(summary)` |
| architect lane | `done` | the human | `hermes kanban complete <id>` or the dashboard |
| architect lane | back to coding | the human | `hermes kanban comment <id> "…"`, `reassign <id> <coder>`, `unblock <id>` |

Refused for every agent (because no row grants it):

- `kanban_complete`, `kanban_block`, `kanban_unblock`
- hand-offs other than the ones in the table. A worker cannot pick another target,
  hand off someone else's task, or hand off with an empty summary.
- **Side doors** in shell, code and file tools (`terminal`, `execute_code`, `process`,
  `write_file`, `patch`, …):
  - any `hermes kanban <verb>` not granted via `shell` or in `always_allow.shell_verbs`
  - the configured `deny_patterns`: the kanban DB file, the `kanban_db` module, and the
    dashboard API
  - `deny_path_patterns` for file writes

  The dashboard session token is also stripped from agent shells by hermes-agent itself.

A refusal comes back to the worker as an ordinary tool error. It lists the moves the
worker's role *may* make and names `kanban_handoff`, so the worker carries on instead
of crashing.

The architect is a **name with no Hermes profile**, e.g. your own name. The dispatcher
never spawns a worker for it.

## The transition table

```yaml
roles:
  coding:    {profiles: [coder]}
  qa:        {profiles: [qa-tester]}
  architect: {human: true, assignee: <your-name>}   # must NOT be a Hermes profile
  # planner: {}          # no `profiles`: a single profile named as its own role

transitions:
  - {from_role: coding, action: handoff, to_role: qa,        status: ready}
  - {from_role: qa,     action: handoff, to_role: architect, status: blocked}
  - {from_role: coding, action: create}                     # fan-out
  - {from_role: coding, action: link}
  # - {from_role: qa, action: complete, when: non_root}     # sub-task gate
  # - {from_role: coding, action: create, via: [tool, shell]}

always_allow:            # open to every profile: reading and talking on the board
  tools: [kanban_show, kanban_list, kanban_heartbeat, kanban_comment]
  shell_verbs: [list, show, tail, …, "boards list"]
unknown_profile: deny    # profiles in no role
root_marker: '(?mi)^\s*Level:\s*0\s*$'
side_doors:
  enabled: true
  deny_patterns: [{pattern: 'kanban\.db', label: the kanban database file}, …]
  deny_path_patterns: ['kanban\.db', '/kanban/boards(/|$)']
```

Row fields:

| Field | Meaning |
|---|---|
| `from_role` | the role making the move |
| `action` | the kanban tool name without `kanban_` (`complete`, `block`, `unblock`, `create`, `link`, …), or `handoff`. With `via: shell`, the `hermes kanban <action>` verb, so any CLI verb (`comment`, `assign`, `promote`, …) can be granted to a role. |
| `via` | `tool` (default), `shell`, or both |
| `when` | `any` (default), `root` or `non_root`. A task is **level 0 (root)** when it has no parent task, or its body matches `root_marker`. |
| `to_role`, `status` | `handoff` only: the target role, and the task's status afterwards. `ready` makes the dispatcher spawn the target, and records a `promoted` event so age-based checks count from the hand-off. `blocked` parks the task (sticky) until a human acts. |
| `requires` | Optional evidence the move needs: `{files: <glob>, matches: <regex>}`, relative to the worker's workspace (`HERMES_KANBAN_WORKSPACE`, `**` allowed). The move is refused until some file matches the glob and, if `matches` is given, its content matches the regex. Use it so a hand-off with no evidence can't pass, e.g. a findings file with a `Verified: yes` line. |

### The configuration options

1. **Per-board matrices.** Top-level keys are the default matrix. Under `boards:` a
   mapping lets a board override any of `roles`, `transitions`, `always_allow`,
   `unknown_profile`, `root_marker` and `side_doors`; each key is replaced as a whole.
   `"*"` covers every board not listed. Without `"*"`, unlisted boards are not
   harnessed. A plain list of slugs applies the default matrix to those boards.
2. **Sub-task gate.** With the default table every task ends with the human, so the
   children of a fan-out park with the architect and their dependents never unlock. Add
   `{from_role: qa, action: complete, when: non_root}` to let QA finish level-1 tasks
   after checking them. Level-0 tasks (no parent, or `Level: 0` in the body) still need
   the human.
3. **Per-role shell verbs.** Add `shell` to a row's `via` to let that role run the
   matching `hermes kanban <verb>` from a shell, e.g. scripted fan-out with
   `create`/`link`. Verbs without a `shell` row stay refused, including
   complete/block/unblock/promote/assign/reassign/archive.
4. **Rollout mode.** `mode: warn` logs each would-be refusal (`[warn] [WOULD-BLOCK]`)
   and lets the call run. Use it to enable the harness beside a live run first.
   `mode: enforce` refuses and logs `[enforce] [BLOCKED]`. The log file is `log_file:`,
   default `$HERMES_HOME/logs/kanban-harness.log`.
5. **Human-lane noise.** `hermes_cli/kanban_diagnostics.py`'s `stranded_in_ready` rule
   escalates `ready` tasks by age only; it ignores `max_in_progress` and whether the
   assignee can be spawned. A human-lane hand-off with `status: blocked` (the default)
   parks the task instead: it gets a `blocked` event, so `recompute_ready` won't promote
   it and it never sits in `ready`. For rework, the human reassigns and then unblocks.
   With `status: ready` the task waits in `ready` (counted as `skipped_nonspawnable`)
   and will be flagged by that rule as it ages.

Tasks correctly queued behind `max_in_progress` still trigger `stranded_in_ready`. That
is an upstream diagnostics issue the harness does not change. The rule scores one task at
a time and never sees the dispatcher's capacity, so fixing it means patching every
diagnostics caller in hermes-agent.

**Review cards.** Under the harness, a scripted "review card" (a script that creates a
card and blocks it for the human) is replaced by a `handoff` row to the human role; the
hand-off parks the task itself for the human.

**Human completion in the run list.** When the human completes a parked task, the
hermes-agent run list shows a zero-length run under the human's name (e.g. `@peter 0s`).
That is the human's action, not a worker run. Labelling it would need a hermes-agent
display patch.

## How it works

- **`pre_tool_call` hook.** It fires before every tool call in every agent process where
  the plugin is enabled. It resolves the board (the tool's `board` argument, then
  `HERMES_KANBAN_BOARD`, then the current board) and the role (`HERMES_PROFILE`, set by
  the dispatcher), then looks for a granting row.
- **`kanban_handoff` tool.** Registered by the plugin and offered only to
  dispatcher-spawned workers whose role has a `handoff` row. It performs the row in one
  write transaction:
  1. ends the worker's run (outcome `handed_off`, summary stored on the run);
  2. clears the claim;
  3. assigns the target role's profile, or the human lane name;
  4. sets the row's `status`;
  5. appends a `handed_off` event, plus a sticky `blocked` event when parking.

  It also posts the summary as a comment.
- **The human is outside the harness.** Your own `hermes kanban` CLI and the dashboard
  never load agent plugins, so moves no row grants to an agent (such as done) are yours
  alone.
- **Fail-closed for the board.** If `harness.yaml` is unreadable or invalid (an unknown
  role, a hand-off without a status, …), or a check errors, every kanban tool call is
  refused. Tools that don't touch the board are unaffected. (The other governance
  plugins fail open; this one deliberately does not.)

## Enabling it

Copy `plugins/kanban-harness/harness.yaml.example` to `harness.yaml` (git-ignored) or
point `KANBAN_HARNESS_FILE` at your own file. Enable the plugin in **every worker
profile** on a harnessed board, and in any orchestrator profile that has the kanban
toolset (see [plugins/README.md](../plugins/README.md#enabling-a-plugin)):

```yaml
plugins:
  enabled:
    - kanban-harness
```

A profile without the plugin enabled is not harnessed. Treat enabling it as part of
adding a profile to a harnessed board. For an existing board, start with `mode: warn`,
read the log, then switch to `enforce`.

## Limits (read before relying on it)

- **Pattern refusal on a shell is not a sandbox.** Deliberately obfuscated code (building
  the path from pieces, base64, a script written to disk and then run) can get past
  text matching. The harness closes the direct and accidental routes; that is where the
  observed failures come from. The hard boundary is to run workers with the **docker
  terminal backend**, with the kanban directory *not* mounted into the container (see
  [security-sandboxing.md](security-sandboxing.md)).
- A stricter option, not shipped: patch `hermes_cli/kanban_db.py` (see
  [patches.md](patches.md)) so mutators refuse when `HERMES_KANBAN_TASK` is set.
- The upstream `review` status (same assignee, PR-oriented reviewer) is separate. The
  harness routes review to a *different* role by reassignment.

## Testing

`bash scripts/test.sh smoke` runs `tests/smoke/kanban-harness.sh`. It drives the real
hermes-agent `kanban_db` in a scratch `HERMES_HOME` and covers:

- every forbidden transition and side door, expecting a refusal;
- the allowed path end to end, plus rework;
- one refused and one allowed case per configuration option.

See [testing.md](testing.md).
