# Strategy

**Longer-horizon goals, roadmaps, and priorities** for this workflow and the products it operates on.

---

## What belongs here

- **Goals and objectives** — what the workflow is trying to achieve over the next quarter, half-year, or year
- **Roadmaps** — planned features, migrations, integrations, and their rough sequencing
- **Priorities** — what is most important right now and why; what is intentionally deferred
- **Strategic decisions** — choices about direction that shape many downstream architecture and operations choices (link to `Architecture/ADR-NNN-*.md` for the structural implementation)
- **Open strategic questions** — unresolved higher-order choices awaiting information or decision

---

## What does NOT belong here

| Content type | Where it goes |
|---|---|
| Tactical runbooks and procedures | `Operations/` |
| Architecture decisions (how, not why in the strategic sense) | `Architecture/` |
| Behavioral rules for the agent | Per-profile `MEMORY.md` |
| Secrets or credentials | Never in the vault |

---

## Suggested conventions

- **Date your roadmap snapshots** — `YYYY-MM-DD-roadmap.md` so you can see how priorities evolved
- **One decision per file** for significant strategic choices — keeps each one findable by Hindsight recall
- **Link downward** — a strategy document should link to the Architecture ADRs and Operations runbooks that implement it

---

Strategy without execution is noise. Link everything to concrete next steps in Operations/ or the active task queue.
