# Secure secrets for this example (eating our own dog food)

> **This is an EXAMPLE, not the canon.** The reusable secrets model — vault +
> launch-time injection + sandbox + tiered handling — lives in
> [`docs/secrets.md`](../../docs/secrets.md) and
> [`docs/security-hardening.md`](../../docs/security-hardening.md). This file
> just walks *our* Home Assistant token through that generic model so you can see
> it work end to end. Substitute your own task secret anywhere you see
> `HASS_TOKEN`. **Placeholder tokens only — never commit a real one.**

The Home Assistant healthcheck needs exactly one task secret: a **long-lived
access token**. That makes it a clean worked example of **Tier 2** in the tiered
secrets model: a per-task credential that should be brokered, not handed to the
agent.

---

## Step 1 — Store the token in your vault

The repo treats Secrets-Kit (`seckit`) and 1Password (`op`) as equal,
first-class options. Use whichever you already run.

### Option A — Secrets-Kit (`seckit`)

A secret is identified by the triple **(service, account, name)**. Store the HA
token under the `hermes` service (same convention as the inference key in
[`docs/secrets.md`](../../docs/secrets.md)):

```bash
# Read from stdin so the value never lands in shell history or argv.
printf '%s' 'PLACEHOLDER-long-lived-token' | seckit set \
  --name HASS_TOKEN \
  --stdin \
  --kind api_key \
  --service hermes \
  --account local-dev \
  --source-label "Home Assistant long-lived token" \
  --rotation-days 90

seckit list --service hermes      # verify it landed (value stays redacted)
```

### Option B — 1Password (`op`)

Store the token as a field on an item in a vault, e.g. an **API Credential**
item `HA Token` in a vault `Hermes`, field `credential`. Its reference is:

```
op://Hermes/HA Token/credential
```

Create it (placeholder value):

```bash
op item create --category 'API Credential' --vault Hermes --title 'HA Token' \
  'credential[password]=PLACEHOLDER-long-lived-token'
```

Either way: **the token now lives only in your vault.** It is not in the repo,
not in a `.env`, not in your shell history.

---

## Step 2 — The BROKER: the host holds the token, the agent never does

The naive approach is to inject `HASS_TOKEN` into the agent and let it `curl` HA.
But then the agent (and, without `docker_forward_env: []`, the sandbox) holds a
live credential. The **preferred Tier-2 pattern is a broker**: a small host-side
helper that reads the token from the vault, performs the read-only HA call
itself, and returns **only the health result**. The agent asks "is HA healthy?"
and gets findings back — the token never enters the agent process or the docker
sandbox.

`ha-broker.sh` in this directory is that broker (the single-machine, no-daemon
sibling of the HTTP host-bridge in
[`docs/security-hardening.md` §7](../../docs/security-hardening.md#7-privileged-helpers-the-host-bridge-pattern)).
It resolves the token vault-first and **never prints it** — every output line
passes through a `scrub()` guard that redacts bearer headers, `op://` refs, and
JWT-shaped strings.

### Run the broker with a vault-resolved token

```bash
export HASS_BASE_URL="http://homeassistant.local:8123"

# Secrets-Kit: broker reads HASS_TOKEN from the hermes service
HASS_VAULT=seckit bash ha-broker.sh

# 1Password: broker reads the op:// reference
HASS_VAULT=op HASS_OP_REF="op://Hermes/HA Token/credential" bash ha-broker.sh
```

The token is resolved into the **broker** process only and held in a shell
variable; it is attached to the request `Authorization: Bearer` header inside the
broker and is never emitted.

### How the agent invokes it (so it never holds the token)

The agent runs an allowlisted command like `bash ha-broker.sh` (or calls the
HTTP bridge's `ha_healthcheck` operation). It receives the findings text, not the
token. With the docker terminal backend and `docker_forward_env: []`, even a
prompt-injected command in the sandbox cannot read `HASS_TOKEN` — it simply is
not present in that process.

---

## Step 3 — Fallback: scoped forward (only if a broker is impractical)

If you genuinely cannot run a broker, the lesser Tier-2 option is to forward
**exactly one** task secret into the sandbox and let the agent call HA directly:

```yaml
terminal:
  backend: docker
  docker_forward_env: [HASS_TOKEN]   # one secret, task-scoped; prefer the broker
```

Inject it for that run with `seckit run --names HASS_TOKEN -- ...` or
`op run --env-file .env.op -- ...`. This is weaker than the broker (the agent now
holds the token) — use a read-only token and rotate after use.

---

## Step 4 — Prove it (placeholder token, offline)

You can prove the broker mechanism — including the no-leak guarantee — with a
**fake** token and no network:

```bash
HASS_MOCK=1 HASS_TOKEN="placeholder-not-a-real-token" bash ha-broker.sh \
  | tee /tmp/ha-broker-proof.txt

# the placeholder must NOT appear anywhere in the output:
grep -F "placeholder-not-a-real-token" /tmp/ha-broker-proof.txt \
  && echo "LEAK (fail)" || echo "no leak (pass)"
```

The mock path emits a representative healthcheck (stuck entities, grouped log
errors, a dashboard "entity not found" note, structural suggestions) so you can
see the *shape* of a real run without touching Home Assistant.

---

## The point

Nothing here is HA-specific. Swap `HASS_TOKEN` for a deploy key, a webhook
secret, or any task credential and the same three moves apply: **store it in a
vault, broker the privileged action on the host, never let the agent hold the
secret.** Home Assistant is just the task we happen to run against our own house.
