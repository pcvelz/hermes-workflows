# What this is for

<!-- Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/docs/intent.md -->

Two things live here and they are often confused. The agents do the work. The
harness decides what they are allowed to do. Read this before changing either,
because most bad changes come from solving a problem in the wrong one.

## The agents

A Hermes agent is a worker with a role, a budget and exactly one exit.

**A role, not a personality.** `coding` does work and proves it. `qa` re-checks
by a different method than the one that produced the result. `planner` cuts work
into pieces a worker can finish. `scout` surveys ground nobody has walked. The
role decides what the agent may do; the persona only decides how it reads.

**A budget, in turns.** Every agent is finite, and the limit is reached long
before the work feels finished. This is the constraint everything else is shaped
around: cards are cut to fit it, prompts are kept short because they spend it,
and a card that exceeds it is a planning defect rather than a worker's failure.

**One exit.** An agent hands its work to the next role, with evidence. It cannot
finish its own card and it cannot grade its own work. Only a person sets done.
That is not ceremony: an agent asked to judge its own output will pass it, every
time, and the whole chain exists to put a different pair of eyes on each step.

## The harness

The harness is the part that does not trust the agents. Not because they lie, but
because an instruction is a suggestion to something that generates text, and a
suggestion is not a control.

**It is a state machine with default deny.** Every card move is a triple of from,
to and actor. The permitted triples are listed; everything else is refused. A
refusal reaches the agent in its own tool result, at the moment of the call, and
the board does not move. No mode turns that into a log line.

**Configuration may only narrow it.** A deployment can add a role, a requirement
or a deny pattern. Nothing in a config file, an environment variable or a profile
can turn a refusal into a warning or exempt anyone from the table.

**It is the only honest record.** An agent's account of what it did is a memory
at the end of a long task. The harness's record of the calls it made is not. When
the two disagree, the harness is right, and anything that matters should be
computed from it rather than asked of the agent.

## Why they are separate

The agents are replaceable. Models change, prompts get rewritten, a role gets
split in two. The harness is what makes that safe: if the rules live in the
prompts, every rewrite is a chance to lose one, and nobody finds out until a card
is quietly marked done by the thing that was supposed to be checked.

So the division is: anything that must hold regardless of which model reads it
belongs in the harness. Anything about how to do the work well belongs in the
prompt. A rule in the prompt alone will drift. A rule in the harness alone will
be discovered by being refused, which is why the two must say the same thing in
the same words ([the system prompt](context-prompt.md)).

## What this is not

It is not a framework for making agents autonomous. It is a framework for making
them accountable — for keeping work moving without anyone having to trust that a
worker did what it said. Autonomy is what you get once the accounting is real.

It is also not a pipeline. A card can go back as easily as forward: a verifier
returns work, a planner re-cuts a card that was too big, a person reopens
something that was accepted. The columns are a state machine, not a conveyor.
