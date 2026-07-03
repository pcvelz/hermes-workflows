# Security

**Security posture notes, threat model, and secret-handling policy** for this workflow.

> **NEVER commit actual secrets, credentials, tokens, or keys here.** This directory holds policy and documentation only. All secrets live in the secrets manager (see secret-handling policy below).

---

## What belongs here

- **Threat model** — what assets exist, who the adversaries are, what attack surfaces the workflow exposes
- **Secret-handling policy** — how secrets are stored (secrets manager), referenced (env vars or config placeholders), and rotated
- **Access control notes** — who or what has access to which systems and why
- **Security posture decisions** — why a particular security choice was made (link to `Architecture/` for the structural rationale)
- **Audit and review records** — periodic reviews of secrets inventory, access grants, or posture

## What does NOT belong here

- Actual secrets, tokens, API keys, passwords — use the secrets manager
- Sensitive notes about specific vulnerabilities that should not be public — put those in `Security/private/` (excluded from the outer vault repo via `.gitignore`)

---

## Secret-handling quick reference

1. **Store** secrets in the designated secrets manager (e.g., 1Password, Vault, `op`).
2. **Reference** them in configs via env var placeholders (`<your-token>`, `${MY_API_KEY}`) — never inline.
3. **Never** commit `.env` files, `*.key`, `*.pem`, or `secrets.*` to any git repo (the vault `.gitignore` provides defense-in-depth).
4. **Rotate** on any suspected exposure; document the rotation in this directory.

---

Policy documentation lives here. Sensitive operational notes go in `Security/private/` (git-ignored).
