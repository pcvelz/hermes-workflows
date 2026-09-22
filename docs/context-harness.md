# The harness: circumstantial context

<!-- Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/docs/context-harness.md -->

One of the three channels an agent learns from. The other two are
[the card](context-card.md) and [the system prompt](context-prompt.md).

## What belongs here

What is true right now. Which agents are running and on what. How often this card
has already failed, and how. What was refused on it, and why. Which files the
previous worker touched.

None of this can be written into a card or a prompt, because it is stale by the
time either is read. It has to arrive at the moment the agent starts.

## The harness both refuses and pushes

Two jobs, and they are one job. It refuses a move the table forbids, naming what
would have been allowed. It puts in front of an agent, at the start of its turn,
the facts that decide what the agent should do next.

Refusal without pushing makes an agent learn the rules by hitting them. Pushing
without refusal is advice, and advice is ignored. A harness does both.

What it pushes, and why each one changes an agent's behaviour:

- **The files the previous run touched.** The verifier reads them instead of
  rediscovering the work. This is the direct payoff of the `Files` line that
  [the hand-over](handover.md) already forces and the harness already computes.
- **This card's history.** How many runs, how each ended, what each consumed. A
  worker about to repeat a failure sees the failure first.
- **What has been refused on this card.** An agent that has already been blocked
  once does not spend its budget finding the same wall.
- **Which agents are running, and on what.** The planner cuts against a real
  board, not an imagined one.

Delivery is the plugin's `pre_llm_call` hook, which returns context for the turn.
The mechanism is upstream and available.

**Built as of 2026-09-22: the refusing half, and the computation the pushing half
needs. Not yet built: the injection itself.** That is a dated gap in this
implementation, not a softening of the rule. An agent reading this should expect
these facts to arrive in its turn, and a deployment that does not yet deliver
them is incomplete rather than compliant.

## Keep it small and true

Two limits, both learned from the other channels:

It competes for the same attention as everything else, so it must be short — a
dozen lines, not a dump. A worker given its whole board is no better off than one
given none.

It must be computed, never recalled. A worker's memory of what it touched at the
end of a long task is unreliable; the harness's record of the tool calls is not.
The value of this channel is precisely that it does not depend on an agent's
account of itself.

## What it must never become

A channel that pushes context is one refactor away from a channel that pushes
instructions. It carries facts: what exists, what happened, what was refused.
Guidance belongs in [the system prompt](context-prompt.md), obligations belong on
[the card](context-card.md), and a fact that has to be argued for is not a fact.
