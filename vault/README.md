# Knowledge Vault

An Obsidian-compatible, git-tracked markdown vault holding the workflow's durable long-form knowledge — Layer 2 of the three-layer memory architecture (see [docs/memory.md](../docs/memory.md)). Plain markdown, greppable, and editable by both humans and the agent.

---

## Layout

```
vault/
├── Architecture/          — how systems are built (components, data flows, ADRs)
├── Operations/            — runbooks, deploy/restart, incident playbooks, cron inventory
├── Research/              — investigations, comparisons, findings
├── Project/               — per-project working knowledge (OWN git repo, nightly backup)
├── Meta/
│   └── hermes-learnings/  — the agent's reflections on itself & the workflow
├── Security/              — security posture & policy (NEVER secrets)
└── Strategy/              — roadmaps, priorities, long-horizon goals
```

Directories seeded by this scaffold: `Architecture/`, `Operations/`, `Research/`, `Project/`, `Meta/hermes-learnings/`. `Security/` and `Strategy/` seed READMEs are provided separately.

Each directory contains a `README.md` explaining what belongs there.

---

## What belongs where

| Content type | Where |
|---|---|
| Behavioral rules the agent obeys every turn | Per-profile `MEMORY.md` (NOT here) |
| Durable long-form knowledge, runbooks, findings | This vault, in the appropriate section |
| Actual secrets, credentials, tokens | Never here — use the secrets manager |
| Ephemeral scratch | Research/ is acceptable, but prune it |

**Durable behavioral RULES do NOT go here.** If a rule must be obeyed on most turns, distill it into the relevant profile's `MEMORY.md` (keeping that file under ~2200 chars). The vault is for knowledge that is too long, too rare, or too structured for the always-injected MEMORY.md.

---

## The Project/ subtree

`Project/` is **its own nested git repository**, intentionally separate from the outer vault repo. Project knowledge often has a different sharing and retention boundary than general workflow knowledge, and it warrants an independent nightly backup.

The outer vault's `.gitignore` already excludes `Project/` so it is never double-tracked.

**First-run setup:**
```bash
cd vault/Project && git init
# or: git clone <your-project-knowledge-repo> .
```

See `vault/Project/README.md` for layout conventions and the backup requirement.

---

## Hindsight sync

This vault is the feedstock for **Hindsight** (Layer 3 — semantic memory). A cron job runs every 30 minutes, mtime-incremental with a ~90-second wall-budget, syncing changed vault files into a pgvector store for semantic recall. Changed files only; cold syncs don't re-ingest the entire vault on every tick.

- Config template: [`../config/hindsight/hindsight.example.yaml`](../config/hindsight/hindsight.example.yaml)
- Sync script skeleton: [`../scripts/memory/vault-hindsight-sync.example.sh`](../scripts/memory/vault-hindsight-sync.example.sh)
- Cron schedule: [`../cron/README.md`](../cron/README.md)

Excluded from sync by default: `Project/` (separate boundary), `Security/private/` (sensitive). Everything else in the included top-level dirs is embedded.

---

## Obsidian

Point Obsidian at this `vault/` directory and it opens directly. Wiki-links, tags, and the graph view all work. Agents and humans share the same files — there is no separate agent copy.
