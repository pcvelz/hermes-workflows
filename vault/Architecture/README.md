# Architecture

Durable documentation of **how the systems in this workflow are built**.

---

## What belongs here

- **Component and service descriptions** — what each piece does, its inputs/outputs, its dependencies
- **Data-flow and integration diagrams** — how information moves between components (mermaid diagrams in fenced blocks, or linked images)
- **Interface and contract notes** — API shapes, protocol choices, versioning policies
- **Architecture Decision Records (ADRs)** — capturing WHY a structural choice was made, what alternatives were considered, and what tradeoffs were accepted

Good architecture docs answer: "How does X work?" and "Why was X built this way rather than Y?"

---

## Suggested conventions

- **One file per system or component** (`hermes-agent.md`, `llm-backend.md`, `semantic-memory.md`, …)
- **ADRs in a `decisions/` subfolder** or named `ADR-NNN-short-title.md` at this level
- **Diagrams** as fenced mermaid blocks (renderable in Obsidian and GitHub) or as linked images in an `assets/` subfolder
- **Link to Operations/** for the corresponding runbook when a system also needs operational procedures

---

## What does NOT belong here

| Content type | Where it goes |
|---|---|
| Step-by-step operational procedures (how to restart, deploy, tail logs) | `Operations/` |
| Transient investigation notes and spike results | `Research/` |
| Behavioral rules for the agent | Per-profile `MEMORY.md` |

---

Start by documenting the highest-churn or least-understood system first.
