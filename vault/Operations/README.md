# Operations

The workflow's **runbooks and operational knowledge** — the things you do to keep it running.

---

## What belongs here

- **Deploy, build, and restart procedures** — step-by-step instructions to bring a service up, roll it forward, or roll it back
- **Log-tailing and health-check recipes** — how to observe system state without knowing it from memory
- **Incident playbooks** — `symptom → diagnosis → fix` chains for known failure modes
- **Cron and watchdog inventory** — a `crons.md` listing every scheduled job with its cadence, owner, and purpose
- **Backup and restore procedures** — including the nightly Project/ repo backup
- **Bridge, launchd, and Docker operational details** — the "how to run the stack" knowledge

Good operations docs answer: "How do I do X right now, on a cold brain?"

---

## Suggested conventions

- **One runbook per task** — keep them focused and copy-pasteable
- **Numbered, imperative steps** — "1. Run `systemctl restart hermes` 2. Check logs with `…`"
- **Mark destructive commands clearly** — prefix with `# DESTRUCTIVE:` or a bold warning
- **`crons.md`** — maintain a single file listing all scheduled jobs: name, command, schedule, what it does, and where to check its exit status

---

## What does NOT belong here

| Content type | Where it goes |
|---|---|
| Design rationale and architecture decisions | `Architecture/` |
| Secrets or credentials | Never in the vault — use the secrets manager |
| Investigation notes and spike results | `Research/` |

---

A good runbook lets a cold operator execute it without prior context.
