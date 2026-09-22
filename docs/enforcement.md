# Enforcement is tools, never words

<!-- Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/docs/enforcement.md -->

What an agent CAN do is decided by the tools it holds. What its prompt says is a
hint, and a hint is something the agent can break in its first call.

## The rule

A limit that matters is expressed as an absent tool, a refused verb or a failed
load — never as an instruction. Configuration may narrow what an agent holds; it
can never widen it.

The test of any proposed limit: could the agent break it by deciding to? If yes,
it is not a limit. `READ ONLY` at the top of a prompt is the clearest example: it
asks an agent to refrain, and an agent that reasons its way past it has broken
nothing but a promise.

## Worked example: a role that plans and cannot work

The planner exists to cut work into cards a worker can finish. It must not do the
work, and it must not hand the work to something else that does it.

Both are enforced by its toolset, not by its prompt:

- `file_read`, never `file`; no terminal, no code execution — it can read
  tickets, surveys, run history and code, and cannot edit anything
  (see [profiles](profiles.md#reading-without-writing-file_read));
- no dispatch skill — it cannot route its ticket to a headless session, which is
  what another profile does today instead of doing its own job;
- no delegation — it cannot spawn;
- kanban tools yes: it reads cards, comments, creates children and hands off.

Its prompt says the same things in words, because an agent should know its limits
rather than discover them by refusal (see [the system prompt](context-prompt.md)).
But if the prompt were deleted tomorrow, the limits would hold.

## Where a fan-out fits

A fan-out may be allowed. What the fanned-out agent can do is the question, and
the answer is its toolset: an investigation dispatch gets no edit tools, so
"read only" is true by construction. A prompt prefix on top of that is a helpful
label, never the control.

## Where this rule runs out, stated honestly

Some limits cannot be expressed as tools today, and pretending otherwise would be
worse than naming them:

- **Per-tool narrowing.** A toolset is granted whole. A profile that needs one
  kanban verb gets all of them, and the refusal has to come from the transition
  table instead.
- **Shell indirection.** A shell check that looks for a literal command name does
  not see `H=hermes; $H kanban complete`. Anything reachable from a shell is
  bounded by the shell the agent has, not by the words it types.
- **Scripts.** A gate that inspects commands cannot see inside a script file. The
  real answer is the same rule one level up: an agent that must not move cards
  should not hold a shell that can.

Each of these is a reason to remove a tool rather than to add a check.
