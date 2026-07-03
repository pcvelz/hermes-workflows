# Research

**Investigations, comparisons, experiments, and findings** that aren't yet — or won't become — formal architecture or operations docs.

---

## What belongs here

- **Tool, model, and library comparisons** — "We evaluated X vs Y for Z use-case; here's what we found"
- **Spike notes** — exploratory work, proof-of-concepts, one-off experiments
- **Benchmark results** — latency measurements, throughput numbers, cost comparisons
- **"I tried X and learned Y" write-ups** — informal but durable findings
- **Open questions** — unresolved issues, pending decisions, hypotheses to test

---

## Suggested conventions

- **Date-prefix exploratory notes:** `YYYY-MM-DD-topic.md` so staleness is visible at a glance
- **Promote and leave a pointer:** when a finding hardens into durable knowledge, move it to `Architecture/` or `Operations/` and replace the Research note with a one-liner like `_Promoted to Architecture/llm-backend.md on 2025-03-15._`
- **Keep scope narrow per file** — one investigation or question per file is easier to reason about and easier to recall semantically

---

## Pruning

Research/ is the one section where scratch is acceptable — but **prune dead notes periodically**. Stale investigations sitting in this tree pollute semantic recall (Hindsight will surface them as relevant when they aren't). A note that hasn't been touched in six months and was never promoted is probably safe to delete.

---

## What does NOT belong here

| Content type | Where it goes |
|---|---|
| Behavioral rules for the agent | Per-profile `MEMORY.md` |
| Finalized component documentation | `Architecture/` |
| Finalized runbooks | `Operations/` |

---

Leave a date. It makes the difference between a finding and a fossil.
