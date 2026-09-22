# Evidence: a null reading is never a finding

## The rule

**A reading that returned nothing must never become a positive finding.**

"I looked and there was nothing" and "I failed to look" produce the same empty result.
Only the worker that gathered the evidence can tell them apart, and it can only do so
while it is gathering. Once the same empty result is handed on as a verdict, the
difference is gone: every later reader, including an independent audit that re-runs the
same tool, sees the same nothing and agrees with it.

The shape is everywhere:

| What happened | What got reported |
|---|---|
| a test run that executed zero tests | "all tests pass" |
| a grep that matched nothing in a log it never opened (wrong path) | "no errors found" |
| a query that returned 0 rows because it hit an empty replica | "no orphaned records" |
| a file the reviewer could not open | "no issues in that file" |
| an endpoint that returned 404 on both sides | "the field is absent on both sides" |
| an extractor that failed to read a section on both sides | "the two sides agree" |

In every row, a tool failure passed for a fact about the world. Re-running the same tool
confirms the failure. It does not check the fact.

## The requirement: three outcomes per criterion

A hand-off reports every acceptance criterion as exactly one of:

- **met**, with the path to the evidence;
- **not met**, with the path to the evidence;
- **could not check**, with what was attempted and why it produced no reading.

*Could not check* is a first-class outcome, not a doubt in prose. The next worker and
the audit both see it by name, and a card with any criterion in *could not check* is not
finished work. It goes back, or to the user as a question.

Before reporting *met* on an empty result, the worker shows that the reading could have
been non-empty: the test count is above zero, the log file exists and has lines, the
query hits the table it names, the page section was actually read. An empty result
without that proof is *could not check*.

## Cards and criteria

- **One kind of judgement per card.** A card that both gathers evidence and judges it
  loses the difference between "nothing there" and "did not look".
- **Acceptance criteria assert about the artefact, not the procedure.** "Every row
  carries a verdict" is satisfied by a table of false verdicts. "Section X on page A
  shows the same values as section X on page B, and both values are quoted" is not.
- **The planner's unit of work is a criterion, not a task.** Where two criteria share an
  expensive setup, the setup becomes its own card, and that card produces an artefact the
  others read.

## Nuance belongs in the contract, not the ticket

A ticket says what must be true when the work is done. How a worker avoids fooling itself
on the way there is the job of the agent contract and the brief. That job has to hold
for a ticket that says nothing about it. The three outcomes and the null-reading rule
above are therefore **not** ticket criteria. They live where a worker reads them for
every ticket.

- **A criterion written after seeing the failure it describes proves nothing.** It
  passes because it was drawn around that defect. The next defect will have a
  different shape.
- **What a ticket legitimately gets is the removal of stale criteria.** A ticket that
  lists three verdict values while the work has used five for weeks should be
  corrected. Adding a sixth value tailored to last night's defect is writing the test
  to match the answer.

Real tickets are generic and will stay generic: the person writing one describes the
outcome they want, not the failure modes of whoever implements it. **A generic ticket
plus a disciplined contract must produce a trustworthy result, because a generic ticket
is the only kind we will ever reliably get.**

This document states a rule, not a pipeline. How a board enforces it (separate reading
and judging passes, a probe that proves a tool can see anything at all, a count check
beside every empty result) depends on the domain, and belongs to that board.
