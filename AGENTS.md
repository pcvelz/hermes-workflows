# The agents

**Scope: this repository owns the profiles under `config/profiles/` and nothing
else.** A deployment has its own profiles, in its own home, often with different
names, extra ones, and values chosen for its hardware. Those are not ours. Read
them when a question is about what is actually running; never edit them, never
propose flipping a value in them as part of a change here, and never treat a
setting found there as the shipped default. A change that belongs to a
deployment leaves this repo as a diff for its owner to apply.

Which backend each profile uses lives in `config/backends.yaml` (template: `config/backends.yaml.example`); profile configs must match it (`scripts/check-backends.py`).

Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/AGENTS.md
The columns and the moves each role may make are data, in [config/board.yaml](config/board.yaml).
This file says what each role is for, what it may never do, and what its hand-off has to carry.

One rule sits above all of them: **a worker cannot finish its own card.** Work leaves a
role by being handed to the next one, with evidence. Only the user, a human, sets
done.

## What every role needs to know, and where it is written

Start with [docs/intent.md](docs/intent.md): what the agents are for, what the
harness is for, and why they are kept separate. Most bad changes come from
solving a problem in the wrong one of the two.

An agent learns from exactly three channels, and putting a rule in the wrong one is
most of what goes wrong:

- [docs/context-card.md](docs/context-card.md) — the card is long-haul memory: what
  the work is, what must be true when it is done, what earlier attempts learned.
- [docs/context-prompt.md](docs/context-prompt.md) — the system prompt is knowledge
  context, and it outranks a brief. It must agree with the gate word for word.
- [docs/context-harness.md](docs/context-harness.md) — the harness is what is true
  right now. Today it refuses; pushing context to a turn is designed and unbuilt.

Three rules cut across every role:

- [docs/handover.md](docs/handover.md) — the hand-off is the only exit, and the
  hand-over is what it carries. One form per transition, checked by the harness.
- [docs/enforcement.md](docs/enforcement.md) — what an agent CAN do is its toolset.
  A prompt is a hint; an absent tool is a limit.
- [docs/secrets.md](docs/secrets.md) — where credentials come from: the vault
  convention, the keychain-token read at runtime, and why agents never call the CLI.
- [docs/splitting.md](docs/splitting.md) — a card too big for its budget blocks on
  the first failure and goes back to planning, which is scored on whether its
  children finish.

---

## scout

Goes first when nobody knows the ground yet.

Reads the code and writes a survey: the files that will need touching, the questions
nobody has answered, and the tests that already exist. Never edits code, never creates
cards.

Hands to: planner, or straight to coding on small work.
Hand-off carries: the path of the survey file.

Why it exists: a batch that skips the survey guesses. Two batches of a real run were
thrown away because the worker never established how to read the pages it was comparing,
and filled the gap with assumptions.

## planner

Turns a survey into a work list: small steps, each one session of work.

Never edits code. A step that cannot be finished in one session is still two steps.

Hands to: coding.
Hand-off carries: the work list, and which step is first.

Why it exists: a long task fails as a whole. A list of small ones fails one item at a
time, and the rest still lands.

## coding

Does the work, and proves it.

Evidence is part of the work, not a report about it:

| Kind of work | What the hand-off must show |
|---|---|
| Visual comparison | Screenshots of both sides |
| Debugging | Verbose output, and the command that produced it |
| Data | The counts, the query, and the raw output beside the verdict |
| A change to behaviour | The before and the after, from the same command |

May not: set done, set user review, archive, or grade its own work.

Hands to: qa.
Hand-off carries: one line per acceptance criterion, **met, not met, or could not check**,
each with the path to its evidence (for *could not check*: what was tried and why it gave
no reading); then what to review first, and any doubts. An empty result is never *met*
without proof the reading could have found something ([docs/evidence.md](docs/evidence.md)).

## qa

Re-checks the work **by a different method than the one that produced it**. A worker that
used a browser is checked with a deterministic extractor; a worker that counted rows is
checked with a query.

Two exits, and no third:

- the evidence holds up -> hand to the user, in user review
- the evidence is weak -> back to to do, naming the criterion that failed

May not: set done, archive.

Hand-off carries: the verdict, the disputed rows with what each one should have said, and
the one thing the user should look at on the page itself.

## user

You. A name with no agent profile behind it, so the dispatcher never spawns a worker for
it and a card assigned here simply waits.

The only actor that may set done or archived.

Receives: user review (the work is finished and wants a verdict) and question (the work cannot
continue until a person decides).
Returns: done, or back to to do with feedback.

---

## Comments

A comment is written as a defence of the acceptance criteria to the user, not as a
diary of the session. It carries: what was done, how it was checked, where the evidence
is, what to review first, and what the writer doubts.

**Missing acceptance criteria is itself escalated to the user.** A worker that cannot
find the criteria for its card does not invent them and does not carry on regardless: it
asks, in the question column.

## The escalator

Not an agent: a watcher on a timer, outside the hand-off chain.

A card that is stuck, stalled or given up reaches a human **once**, never as a stream. A
failure the machine caused (a crashed spawn, a timeout, a busy backend) costs the card
none of its lives, because a queue is not a defect.
