# Project

**Per-project working knowledge** — the living context for each repo or product this workflow operates on.

What goes here: goals, current state, gotchas, task history, project-specific conventions, environment notes, and anything that would take meaningful effort to re-derive cold.

---

> **IMPORTANT: This directory is its OWN git repository.**
>
> `vault/Project/` is nested inside the vault but intentionally separate from the outer vault repo. Project knowledge often has a different ownership, sharing, and retention boundary than general workflow knowledge — and it gets an **independent nightly backup**. The outer vault's `.gitignore` already excludes this path so it is never double-tracked.

---

## First-run setup

```bash
# Option A: start fresh
cd vault/Project
git init
git commit --allow-empty -m "init: project knowledge repo"

# Option B: clone an existing project-knowledge repo
cd vault/Project
git clone <your-project-knowledge-repo> .
```

After setup, confirm the outer vault repo doesn't see the contents:
```bash
# From vault root — Project/ should appear as an untracked directory, not files
git status
```

---

## Layout suggestion

```
Project/
├── <project-name>/
│   ├── README.md      — what it is, where it lives, how to run/deploy
│   ├── notes/         — working notes, gotchas, in-progress context
│   └── decisions.md   — project-specific choices and their rationale
└── ...
```

One subfolder per project or product. Keep each project's context self-contained so the agent can load the relevant folder without reading everything.

---

## Backup

A nightly job should commit and push (or snapshot) this repo. Without a backup, a disk loss or git corruption loses all accumulated project context. See `vault/Operations/` for the backup runbook and `cron/README.md` for the nightly cron schedule.

---

## What does NOT belong here

- **Secrets or credentials** — use the secrets manager; reference config locations, never inline values
- **General workflow architecture** — that belongs in `vault/Architecture/`
- **Behavioral rules** — those belong in the relevant profile's `MEMORY.md`

---

Each project's knowledge lives here so the agent always knows where to look.
