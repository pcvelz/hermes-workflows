# Splitting work, and who fixes a card that is too big

<!-- Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/docs/splitting.md -->

A card cut too large does not fail cleanly. It burns its budget, retries, and
often succeeds on a later attempt — which hides the mistake behind a run that
looks busy. Nothing measures the cut, so nothing corrects it.

## The measured example

One card asked for thirty-three data rows and five criteria gradings against a
sixty-turn budget, where the mechanics alone — fetch, scroll, read, per item —
cost about twenty-one turns before any thinking. It took fifteen runs across ten
hours: three budget exhaustions, two protocol-violation crashes, nine hand-offs.
It was retried quietly each time.

Splitting it by item would not have helped. Three items of the same comparison is
one kind of judgement; the load is the number of DECISIONS, not the volume.

## Retry once, then block

On a board with a deployed spec:

- a run that exhausts its budget, or crashes for a reason that is not waiting,
  **blocks its card on the first occurrence**;
- a wait-class failure — the model busy or absent — is a queue, never a failure,
  and never counts;
- the block creates one card for the planner, carrying the blocked card, its run
  history, and its last hand-over **verbatim**;
- where the failed run produced no hand-over at all, the planner's card says so
  plainly and tells it not to invent a cut from the title.

One page reaches the person, and it names the cut rather than the failure.

## What the planner may do

It creates CARDS. It never spawns an agent — that distinction has to be written
explicitly, or "no fan-out" is read as "cannot split", which is the opposite of
the intent. It may comment on a running card and ask for a worker to be stopped,
for one reason only: the card is cut wrong and continuing wastes the budget. Any
such stop records its reason.

It never sets a card done. Its own card goes to the user like anyone else's.

## The planner is scored

After a cut, its children are watched: did each one finish inside its budget? A
child that exhausts its budget is the cut's failure, not the worker's, and it
returns with that history attached.

Without this, a planner that splits badly does so forever and the overload has
simply moved up a level.

## Keep the cards at real size

The temptation, when a chain is being demonstrated, is to shrink a card so the
run completes. That buys a clean result to report and destroys the exercise: the
point is a cut that survives the work as it actually is. Watch for it in
yourself — the urge is strongest when a tidy outcome would be easy to show.

## Status

The rows, the block and the planner card are built and live. **The planner has
not yet cut a real card.** Everything above the "what the planner may do" heading
is observed behaviour; the planner's own conduct is designed and unproven.
