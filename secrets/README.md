# secrets/

This directory holds **templates and examples** for wiring API keys into Hermes.
It contains **no real secret values** — only env-var names and placeholders.

The full walkthrough lives in [`../docs/secrets.md`](../docs/secrets.md). This
README is the quick reference.

## Files

| File | What it is |
|------|------------|
| `seckit-defaults.example.json` | Template for `~/.config/seckit/defaults.json`. Lets contributors run the short `seckit run -- …` form without repeating `--service`/`--account`. Copy it into place and edit. |

## Approach in one paragraph

A Hermes profile names the key it needs by env var (`key_env: LLM_API_KEY`); it
never stores the value. A secrets layer stores that value at rest and injects it
into Hermes's environment at launch:

- **Primary (macOS): Secrets-Kit** —
  `seckit set --name LLM_API_KEY --stdin --service hermes …` to store, then
  `seckit run --service hermes --names LLM_API_KEY -- ./scripts/start.sh` to launch.
- **Alternative: 1Password** —
  `op run --env-file .env.op -- ./scripts/start.sh`, with `LLM_API_KEY` set to an
  `op://…` reference in `.env.op`.

## Honest framing

Both approaches do **at-rest storage + launch-time injection**, not per-request
isolation. Once Hermes starts, it (and its subprocesses) can read the real key
from the environment. True "Hermes never holds the secret" isolation needs a local
egress proxy / sidecar that holds the key and is a roadmap item — see
[`../docs/secrets.md`](../docs/secrets.md).

## Rules

- **Never commit a real key.** This directory is for templates only.
- Provision keys via `seckit set --stdin` (or `op`) so values never hit shell
  history or argv.
- Use `--names` with `seckit run` for least privilege.
