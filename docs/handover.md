# The hand-over

<!-- Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/docs/handover.md -->

A hand-off is the only exit an agent has. The hand-over is what it carries.

A hand-over is not a status line. It is everything the next agent needs so it
does not rediscover the work, and everything a reviewer needs to tell what was
established from what was assumed.

## One form, one section per transition

The form ships as `config/HANDOVER.md.example`. A deployment copies it, adapts
the wording if it likes, and points its board at it. The common fields are:

```
Done: <what you did, one line>
Method: <how you established it: the tool, script or command>
Files: <every file you wrote or changed: paths only, inside the workspace, or "none">
Result: <what you found, in numbers where there are numbers>
Could not check: <what you were unable to establish, and why>
Review first: <the single thing you are least sure of>
Doubts: <anything that failed, was retried, or looked wrong, or "none">
```

Each transition adds two or three lines of its own: work to a verifier names the
method NOT used, so the check does not repeat the mistake; a verdict names the
exact failing thing rather than a paraphrase; a question asks one question; a
card going back to planning says why the CARD is the problem and where the work
divides; a cut names, per child, the one judgement it owns and why it fits the
budget.

## Why these fields

**Method** travels because the next agent must not repeat it. A check that
re-runs the original method reproduces its mistakes: a failed reading, re-read
the same way, returns the same empty answer, and two agents then agree about
nothing.

**Files** travels because it is the only reliable index of what was touched, and
because a worker's memory of it after a long task is not reliable. It is also the
seam for [circumstantial context](context-harness.md): once the harness knows
what a run changed, it can put that in front of the next agent.

**Could not check** is a first-class outcome, never an apology. Silence there is
read as "everything was checked", which is how an unexamined gap becomes a
finding nobody made.

## The harness enforces the form

On a board whose spec carries a `handoff:` block, a hand-off that does not meet
the form is refused, and the refusal names the missing field. Three checks:

1. **Structural** — every field present; `none` is allowed, absence is not.
2. **Computed** — the `Files` line must match what the run actually touched,
   taken from the harness's own record of file-writing tool calls and from the
   repository state diffed between claim and hand-off. Only paths this run
   changed are required; another session's dirty files are not the worker's
   fault, and a refusal for someone else's mess teaches nothing.
3. **Consistency** — `Could not check: none` is refused on a run that recorded a
   failure it never recovered from.

A board whose spec has no `handoff:` block has no form, so nothing is checked.
That absence is reported once by the install check, so switching the form off by
deleting it is a visible act rather than a quiet one.

## Read it at the moment of use

Both worker prompts tell an agent to open the form when it hands off, not at the
start of its task. A rule read at the beginning of a long task is half-remembered
or compacted away by the end. The form is short precisely so it can be read then.
