# Secrets — host-side management (no secrets container)

This scaffold deliberately does **not** run a Bitwarden/Vaultwarden secrets
container. Secrets are managed host-side, not in a container.

## Recommended approach

See **[docs/secrets.md](../../docs/secrets.md)** for the full guide.

**macOS — Secrets-Kit** (`github.com/unixwzrd/Secrets-Kit`):

Stores API keys in the macOS login Keychain and injects them into Hermes at
launch via `seckit run`. Keys stay out of `.env` files, shell history, and
commit history.

```bash
# Provision the local llama-swap bearer token once (only if your proxy requires
# one; replace sk-REPLACE-ME with a real key):
printf '%s' "sk-REPLACE-ME" | seckit set \
  --name LLM_API_KEY --stdin --kind api_key \
  --service hermes --account local-dev \
  --source-label "Local llama-swap proxy" \
  --source-url "http://127.0.0.1:<PORT>" \
  --rotation-days 90

# Launch with key injected at runtime:
seckit run --service hermes --account local-dev --names LLM_API_KEY -- \
  docker compose --env-file .env up -d
```

**macOS / cross-platform — 1Password `op run`**:

```bash
op run --env-file=<(echo LLM_API_KEY=op://vault/item/field) -- \
  docker compose --env-file .env up -d
```

## What Secrets-Kit provides (honest framing)

Secrets-Kit is an **at-rest store + launch-time env injector**. It replaces
scattered `.env` files and plaintext keys with macOS Keychain storage. The
launched process (Hermes / Docker Compose) still receives the real key as an
environment variable — this is by design and confirmed by the tool's own
security model. It is not a per-request credential broker.

If you need the agent to never hold the long-lived key directly, a thin local
egress proxy (itself launched via `seckit run`) is the appropriate architecture;
see `docs/secrets.md` for the roadmap note.

## Why no Bitwarden container

A Bitwarden/Vaultwarden container would add operational complexity (volume
management, admin setup, agent-wiring) without providing stronger security than
host-side Keychain injection for a single-machine POC. The community-standard
pattern of env-vars-in-container + optional Bitwarden is superseded here by
Secrets-Kit / `op run` — both of which are purpose-built for keeping keys out of
`.env` files from day one.
