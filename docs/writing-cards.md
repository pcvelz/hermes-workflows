# Writing a card a small model can actually finish

For anything with tickets and acceptance criteria: tests, migrations, log triage, code
review, back end or front end.

## The frame

None of this is training. No weights change. Every example, rule and template in a card
is paid for again on every call, and competes with the task for the same attention. So
the question is never "what else could we tell it". It is "what earns its place".

## What shapes behaviour, strongest first

This order comes from what we have seen on our own boards, not from theory.

### 1. An output template

A fixed shape to fill turns judgement into filling in slots. A model that cannot reason
about a rule can still fill a shape. A shape is also the only thing a script can check.

Put the template where it is used. In a long card, repeat it: a template declared once
at the top is forgotten by the end.

```
criterion: <id>
outcome:   met | not met | could not check
evidence:  <path or command + output>
if could not check: <what was tried, why it gave no reading>
```

### 2. One to three worked input -> output pairs

One near-miss example beats five abstract rules, because the model matches on the
difference. Choose examples that are *nearly* right, not obviously wrong.

> **Input:** `pytest -q` printed `no tests ran in 0.01s`.
> **Wrong (nearly right):** `outcome: met, all tests pass`
> **Right:** `outcome: could not check. The run collected 0 tests; the test path was wrong.`

### 3. An anti-pattern paired with its correct version

Never a bare list of prohibitions. A checker that only listed prohibitions rejected 122
of its own author's messages. The output written against it came out sterile: it avoided
every rule and aimed at nothing. Put a positive target beside every "don't".

| Instead of | Write |
|---|---|
| "no errors found" (after grepping a log) | "0 matches for `ERROR` in `app.log` (41 203 lines, last entry 14:02)" |
| "migration is safe" | "migration applied to a copy of prod (12 tables); row counts before/after: …" |
| "LGTM" on a review | "checked: the null path in `parse()`; not checked: the retry branch" |

### 4. Abstract rules

These are the weakest per token. "Be careful about X" is "don't make mistakes" in a new
coat: a model agrees with it and breaks it in the next sentence. Where you have a rule
that matters, turn it into a template slot, an example or a paired anti-pattern.

## The ceiling (a hypothesis, not a measurement)

The limit doesn't seem to be the token count. It seems to be the number of **distinct
decisions**, and how far each one sits from where its instruction was written. Our
evidence, measured: one card asking for eight kinds of judgement exhausted a
60-iteration budget three times in a row (about 70 minutes of model time) before a fourth
attempt completed. A card over the ceiling doesn't fail cleanly. It burns its budget
repeatedly and then succeeds by luck on a later attempt, which costs far more than a
card that was split in the first place.

Defaults until measured otherwise:

- **one** primary kind of judgement per card;
- at most **two** output artefacts;
- at most **three** examples.

A card that needs more gets split. Where two criteria share an expensive setup, the
setup becomes its own card, and that card produces an artefact the others read.

## How to check any of this

Advice is not a check. These are:

- **Mechanical validators on output shape.** Gates that fail, never gates that advise.
  If the template has an `outcome:` slot, a script rejects a hand-off without one.
- **A regression corpus.** Keep the outputs that were accepted. Re-run the checker over
  them after every change to the instructions. A checker that suddenly rejects accepted
  work has changed meaning.
- **Golden cases per card template.** Two or three pairs of input and known correct
  output. Re-run them before and after any change to the brief, and name the model in
  the result. Then "this brief is too big for this model" becomes a measurement. One
  golden case every board can reuse: *a reading returned zero on both sides while the
  source plainly had content. The correct outcome is `could not check`, not "they
  agree"* ([evidence.md](evidence.md)).
- **A/B the same work.** Old instructions against new: same input, same model.

## Effort versus tier

People get this wrong in both directions:

- Raise **effort** for *judgement density*: many constraints to hold at once, or
  contradictions to notice.
- Raise the **tier** for *capability*: things the model does not know how to do.

Raising effort on a smaller model buys thinking time, never capability. For example, an
agent writing red-first tests against a settled contract runs at low effort and does it
well. An agent holding a dozen competing rules at once needs the headroom.
