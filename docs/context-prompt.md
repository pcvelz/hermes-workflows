# The system prompt: knowledge context

<!-- Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/docs/context-prompt.md -->

One of the three channels an agent learns from. The other two are
[the card](context-card.md) and [the harness](context-harness.md).

## What belongs here

Who this agent is, what it may decide, what it must never do, and how it exits.
Rules that must hold for every ticket, including the ticket that says nothing
about them.

## It outranks a brief

A brief is a document the agent is asked to read. The prompt is what it is. When
the two disagree, the prompt wins, because it arrived first and shaped everything
after it.

Observed: a QA profile's prompt told it to hand its ticket to another session
rather than verify anything itself. It obeyed, faithfully, for ten hours — while
the brief that defined QA, with its criteria and its required independent method,
never reached the session doing the work. The brief was not ignored. It was never
in the room.

So: if a rule matters, it goes in the prompt. A rule that lives only in a
document is a rule with an extra step, and the extra step is where it is lost.

## It must agree with the gate, word for word

The gate is an invisible hand: an agent meets it only when refused. That makes
the prompt the only place an agent learns the rules in advance, and any
disagreement between the two is paid for by the agent.

Observed: two profiles' prompts, and the dispatch skill they follow, instructed
workers to call `kanban_complete` or `kanban_block` on a verdict. Both are
refused for every agent role on a board with a deployed spec. Two runs ended as
`crashed — worker exited cleanly without calling kanban_complete or kanban_block
— protocol violation`. The prompt sent them at a door held shut.

The rule that follows: for every basic obligation, state it in the prompt AND
enforce it in the harness, in the same words. If the prompt says "when you are
done, write your result in this form", the harness checks that a result in that
form exists. A rule in only one of the two is a rule that will drift.

When a refusal does happen, it should name the missing thing, point at the form,
and quote the line of the agent's own prompt that says the same. That refusal is
the one moment where a person can see the prompt and the gate agreeing — or not.

## Aim it at the work

A prompt is paid for on every call and competes with the task for the same
attention. Domain guidance for an unrelated project, a persona restated three
ways, a tool preference that contradicts the work: each one costs turns the
worker needs.

Observed: worker prompts of nineteen lines, of which the kanban section was one,
opening with guidance for a different product entirely, and a tooling rule that
forbade the browser automation the work required. The budget we ration was being
spent on none of the work.

## What it is not

It is not the place for facts about right now, which belong to
[the harness](context-harness.md), nor for the obligations of one specific piece
of work, which belong to [the card](context-card.md).
