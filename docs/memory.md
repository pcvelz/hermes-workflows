# Memory & Knowledge Architecture

Hermes-style autonomous workflows need memory at three different timescales and granularities. A rule the agent must obey on every turn is fundamentally different from a deployment runbook, which is itself different from a half-remembered architecture decision that you want to surface by meaning rather than path. This document describes a three-layer model that fits each kind of knowledge into the right container.

Core principle: each layer is **ADDITIVE** on top of the layer below it — MEMORY.md is always present; the vault and Hindsight enrich on demand. You do not replace one layer with another; you promote knowledge up the stack deliberately.

---

## At a glance

| Layer | Storage | Scope | Lifecycle | Injected? |
|---|---|---|---|---|
| **MEMORY.md** | Per-profile markdown file (`~/.hermes/profiles/<profile>/memories/MEMORY.md` on the native setup) | One profile | Edited rarely, hand-curated | YES, every turn |
| **Vault** | Git-tracked Obsidian-style markdown tree (`vault/`) | Shared across profiles | Grows continuously, browsable | NO — linked/quoted on demand |
| **Hindsight** | pgvector semantic store (service on port 8889) | Shared | Cron-synced from vault every 30 min (proposed — sync not yet wired) | NO — retrieved via tools |

---

## Layer 1 — Per-profile MEMORY.md (hot, always-on)

A small markdown file injected into the system prompt **every turn** for that profile. This is the agent's standing operating procedure: it is always present, so its contents are paid in tokens on every single request.

**What belongs here:** ONLY durable behavioral rules — preferences, do/don't constraints, hard operational invariants. Not project facts, not long-form knowledge, not logs, not anything you might only need once. Those live in the vault.

**Cap: ~2200 characters.** This is a hard discipline boundary, not a soft suggestion. Every character here is paid on every turn and crowds out actual task context and the skills prompt. An overgrown MEMORY.md silently degrades reasoning and inflates token cost. Rule of thumb: if it is not a rule the agent must obey on most turns, it does not belong here.

The native setup keeps each profile's file at `${HERMES_HOME}/profiles/<profile>/memories/MEMORY.md`. Keep it well under the cap — a lean MEMORY.md validates the discipline.

A sibling `USER.md` may hold stable facts about the operator (name, timezone, preferred tools). Also injected every turn — keep it equally lean.

**Example MEMORY.md content** (these are illustrative examples, not real content):

```markdown
- Always run the linter before claiming a task done.
- Never force-push shared branches.
- Prefer the vault over re-deriving known architecture.
- When uncertain about credentials, check the secrets manager first.
```

The moment a rule is no longer needed on most turns, move it to the vault (Meta/hermes-learnings/ for workflow rules, Architecture/ for system rules). Prune ruthlessly.

---

## Layer 2 — The Vault (warm, long-form, browsable)

An Obsidian-compatible markdown vault for durable long-form knowledge that is too big, too rare, or too structured for MEMORY.md. Plain files, greppable, git-tracked, and editable by both humans and the agent.

The vault is the **human-readable source of truth** for everything worth knowing about this workflow and the systems it operates on. It is also the feedstock for Layer 3.

### Top-level sections

| Directory | What belongs there |
|---|---|
| `Architecture/` | How systems are built — component/service descriptions, data flows, interface/contract notes, Architecture Decision Records (ADRs) |
| `Operations/` | Runbooks, deploy/restart/build procedures, incident playbooks, cron inventory, backup/restore |
| `Research/` | Investigations, tool/model comparisons, spike notes, benchmark results, open questions |
| `Project/` | Per-project working knowledge — goals, current state, gotchas, task history, project-specific conventions |
| `Meta/hermes-learnings/` | The agent's reflections about itself and the workflow — lessons learned, postmortems, 'what went wrong and the fix' |
| `Security/` | Security posture notes, threat model, secret-handling policy — **NEVER** actual secrets |
| `Strategy/` | Longer-horizon goals, roadmaps, priorities |

Each top-level directory ships a seed `README.md` describing what belongs there.

### The Project/ subtree

`Project/` is **its own nested git repository**, intentionally separate from the outer vault repo. Why: project knowledge often has a different ownership, sharing, and retention boundary than general workflow knowledge, and it warrants an independent nightly backup. The outer vault's `.gitignore` excludes this path so it is never double-tracked.

First-run setup:
```bash
cd vault/Project && git init
# or: git clone <your-project-knowledge-repo> .
```

### What does NOT go in the vault

- Behavioral rules that the agent must obey every turn → those go in the per-profile MEMORY.md
- Actual secrets, credentials, tokens → never; see `.gitignore` and `Security/` policy
- Ephemeral scratch that will be stale in a week → prune it from Research/ periodically

---

## Layer 3 — Hindsight semantic memory (cold-but-searchable)

Hindsight is a pgvector-backed semantic memory service (reference deployment exposes HTTP on **port 8889**) that embeds vault content so the agent can retrieve by meaning rather than path. It is strictly additive: it never replaces MEMORY.md or the vault, it makes the vault's accumulated knowledge recall-able when the agent doesn't know where to look.

### Tool verbs

| Verb | Effect |
|---|---|
| `hindsight_retain` | Store / ingest a chunk into the semantic store |
| `hindsight_recall` | Semantic search — returns relevant chunks given a query |
| `hindsight_reflect` | Synthesize / condense across recalled material |

### Sync model (proposed)

> **Proposed / not yet implemented.** The sync design is: a cron job every **30 minutes**, mtime-incremental — only re-ingests vault files whose mtime is newer than the last successful run. Each run has a wall-clock budget of ~90 seconds, stopping cleanly if it cannot finish in time and resuming on the next tick. The shipped sync script is a dry-run stub; the Hindsight ingest API is not yet wired.

See [`config/hindsight/hindsight.example.yaml`](../config/hindsight/hindsight.example.yaml) and [`scripts/memory/vault-hindsight-sync.example.sh`](../scripts/memory/vault-hindsight-sync.example.sh) for the provided templates.

### Timeout note

The embedder calls your configured LLM backend (`${LLM_BASE_URL}`). If your backend is self-hosted and has cold-start latency, set client timeouts to at least 120 seconds to avoid spurious failures. The provided config and sync script both default to 120 s. Hosted APIs do not have cold-start latency.

---

## How the layers compose (a turn)

1. **MEMORY.md + USER.md** are injected into the system prompt every turn — behavioral rules are always present.
2. The agent works the task. If it needs background knowledge it has two paths:
   - Grep or open the vault directly (knows the path).
   - Call `hindsight_recall` with a semantic query to surface relevant vault-derived chunks when the path is unknown.
3. At the end of a session, durable new **behavioral rules** are distilled by hand into the relevant profile's MEMORY.md (respecting the ~2200-char cap). Durable new **knowledge** is written to the appropriate vault directory.
4. The cron syncs vault → Hindsight every 30 minutes so recall stays current without manual intervention.

**The curation discipline is the point.** Promote knowledge up the layers deliberately. Do not dump. A bloated MEMORY.md is an ops problem; a bloated vault with no cleanup pollutes semantic recall.

---

## Deployment topologies

### Native

The vault lives as a directory under the repo or at a path you choose (e.g. `${HERMES_HOME}/vault`). Hindsight would run as a local service on port 8889. The sync script runs via launchd or cron.

> **Proposed / not yet implemented:** Hindsight sync (Layer 3) is a design target. The current scaffold ships a bare pgvector container and a dry-run sync stub — it is not wired end-to-end.

### Docker (optional)

In the Docker compose stack, Hindsight runs as the `hindsight` container (pgvector) with port 8889 published to the host. Sync semantics are the same once wired — the script would target the `:8889` endpoint. If the embedder inside the container needs to reach a self-hosted backend on the host, see the docker-to-host networking caveat in [`docs/architecture/topologies.md`](architecture/topologies.md).

---

## Operating & extending

- **Keep MEMORY.md under the cap.** Review it quarterly. If a rule hasn't been relevant in weeks, move it to Meta/hermes-learnings/ as a lesson note.
- **Back up the Project/ repo nightly.** It is its own git repo for a reason — it needs its own commit/push job; see Operations/ runbooks and `cron/README.md`.
- **Monitor the 30-minute sync cron.** Check exit status. A silent failure means Hindsight's recall is stale.
- **Re-run sync manually after large vault edits** if you need recall to be current before the next scheduled tick:
  ```bash
  bash scripts/memory/vault-hindsight-sync.example.sh
  ```
- **Never commit secrets to the vault.** The `.gitignore` provides defense-in-depth, but policy enforcement lives in `vault/Security/`.
- **Prune Research/ periodically.** Dead investigation notes accumulate and pollute semantic recall. If a finding became durable, promote it to Architecture/ or Operations/ and leave a pointer.

---

## See also

- [`../vault/README.md`](../vault/README.md) — vault layout, section guides, Project/ setup
- [`../config/hindsight/hindsight.example.yaml`](../config/hindsight/hindsight.example.yaml) — Hindsight sync configuration template
- [`../scripts/memory/vault-hindsight-sync.example.sh`](../scripts/memory/vault-hindsight-sync.example.sh) — mtime-incremental sync script skeleton

Cross-references (owned by other elements, may land later):
- [`cron/README.md`](../cron/README.md) — full cron/launchd inventory, including the 30-minute vault→Hindsight sync cadence
- [`docs/profiles.md`](profiles.md) — orchestrator/coding/planner/qa-tester role definitions and per-profile MEMORY.md paths
