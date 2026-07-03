# Security Policy

## No secrets in this repository

This repo ships **only portable templates** with placeholders (`HERMES_HOME`, `~/`,
`<your-token>`, `<your-domain>`). **Never commit:**

- `.env` files, `auth.json`, or any credentials manager export
- any `*.db` / `*.db-shm` / `*.db-wal` (kanban/state databases)
- API keys, tokens, or private vault content

These are listed in [`.gitignore`](.gitignore). If you ever see one of them staged, **stop** and
unstage it.

## Handling secrets locally

Never paste a key into a tracked file. Hermes profiles reference a key by env-var
**name** (`key_env: LLM_API_KEY`) and read the value from the environment at runtime.
Store the value at rest and inject it at launch using one of:

- **Secrets-Kit (`seckit`)** — macOS Keychain store + `seckit run … -- <start>` launch-time
  injection. The recommended default on macOS.
- **1Password (`op`)** — `op run --env-file` with `op://` references, for users who already
  run 1Password.

Both do **at-rest storage + launch-time injection**, not per-request isolation — once Hermes
starts, the process holds the real key in its environment. See [`docs/secrets.md`](docs/secrets.md)
for the full walkthrough, the honest framing, and the egress-proxy roadmap for true isolation.

## Network-exposure caveats

- **Inference backend bind.** If you self-host your inference backend and it binds to loopback
  only, exposing it (e.g. on `0.0.0.0`) so containers can reach it **also exposes it to your
  LAN**. Only do this on a trusted network or behind a tunnel.
- **Host bridge.** The bridge (`scripts/bridge/hermes-bridge.py`, port `9876`) performs
  privileged build/deploy/git operations. It must **never** be exposed beyond `localhost`.
  If you must bind it for the Docker topology, set `HERMES_BRIDGE_TOKEN` and a host firewall
  rule.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Email `<security-contact-email>`
with details and reproduction steps. We aim to acknowledge within **7 days** and will
coordinate a fix before public disclosure.

## Supported versions

This is a **foundation / base** project; security fixes target the `main` branch.
