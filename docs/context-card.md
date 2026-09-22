# The card: long-haul memory

<!-- Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/docs/context-card.md -->

One of the three channels an agent learns from. The other two are
[the system prompt](context-prompt.md) and [the harness](context-harness.md).
Using the wrong channel for a piece of knowledge is most of what goes wrong.

## What belongs here

What the work is, what must be true when it is done, and what earlier attempts
learned. The card outlives every worker that touches it and every compaction.

## Write the contract at birth

A card's comments are append-only, and `edit` sets only its result. A card's body
cannot be corrected after creation. So everything a worker needs to know about
its obligations has to be on the card the moment it is made:

- the ticket or source its acceptance criteria come from, by path;
- the criteria this card owns, by id;
- the form its hand-over must take.

A card that names no source sends its worker looking, and a worker that cannot
find its criteria works without them. In one observed run, three workers in a row
read the brief and never the ticket, because the card named only the brief. All
three graded themselves against criteria they had never opened.

## One kind of judgement per card

Not one file, one page or one item: one kind of DECISION. Reading ten inputs is
one kind. Deciding whether ten readings agree is another. Grading those decisions
against criteria is a third. That is three cards, even when it is one ticket.

The unit that matters is the worker's turn budget. Count the mechanical cost
first — every fetch, read and command before any thinking — and if the mechanics
alone consume most of the budget, the card is too big however simple the
judgement looks. State the estimate on the card, in turns. A cut nobody can
justify in turns is a guess.

## What the card is not

It is not a place for facts about right now. Which agents are running, how often
this card has failed, what was refused on it: all of that is stale by the time a
worker reads it, and belongs to [the harness](context-harness.md).

It is also not a place to put nuance that should bind every ticket. A rule
written into one card binds one card. Rules about how work is done belong in
[the system prompt](context-prompt.md) and in the contract, so they hold for a
ticket that says nothing about them — which is the only kind of ticket you can
count on receiving.
