# Secure secrets for this example (eating our own dog food)

> **This is an EXAMPLE, not the canon.** The reusable secrets model — vault +
> launch-time injection + sandbox + tiered handling — lives in
> [`docs/secrets.md`](../../docs/secrets.md) and
> [`docs/security-hardening.md`](../../docs/security-hardening.md). This file
> just walks *our* Gmail credential through that generic model so you can see it
> work end to end. Substitute your own task secret anywhere you see
> `GMAIL_API_KEY`. **Placeholder credentials only — never commit a real one.**

The Gmail processing example needs exactly one task secret: a **Composio API
key** that fronts a read-only Gmail OAuth grant. That makes it a clean worked
example of **Tier 2** in the tiered secrets model: a per-task credential that
should be brokered, not handed to the agent.

---

## Step 0 — Why Composio (and not a raw Gmail OAuth client)

Rather than ship a Google OAuth client (`client_secrets.json` + `token.json`) or
an App Password into this repo, this example fronts Gmail with a **Composio Tool
Router MCP server** scoped to a **read-only** allowlist:

- `GMAIL_FETCH_EMAILS`, `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`,
  `GMAIL_FETCH_MESSAGE_BY_THREAD_ID`, `GMAIL_LIST_THREADS`, `GMAIL_LIST_LABELS`,
  `GMAIL_GET_PROFILE`, `GMAIL_GET_ATTACHMENT`.

Composio holds the Gmail OAuth grant on your behalf (a "connected account"). The
broker authenticates to Composio's REST tools API with a custom **`x-api-key`**
header (not `Authorization: Bearer`). `GMAIL_USER_ID` scopes which connected
account is used. Read-only is enforced **fail-closed** by the server's
allowlist — there is no send/modify/delete tool to reach for. Mint the server in
the Composio dashboard (or `POST /api/v3/mcp/servers`) and note its `user_id`.

> No Google client secret, no `token.json`, no App Password, no IMAP appears in
> this repo. The only secret you store is the Composio `x-api-key`.

---

## Step 1 — Store the credential in your vault

The repo treats Secrets-Kit (`seckit`) and 1Password (`op`) as equal,
first-class options. Use whichever you already run.

### Option A — Secrets-Kit (`seckit`)

A secret is identified by the triple **(service, account, name)**. Store the
Composio key under the `hermes` service:

```bash
# Read from stdin so the value never lands in shell history or argv.
printf '%s' 'PLACEHOLDER-composio-api-key' | seckit set \
  --name GMAIL_API_KEY \
  --stdin \
  --kind api_key \
  --service hermes \
  --account local-dev \
  --source-label "Composio Gmail read-only key" \
  --rotation-days 90

seckit list --service hermes      # verify it landed (value stays redacted)
```

### Option B — 1Password (`op`)

Store the key as a field on an item in a vault, e.g. an **API Credential** item
`Composio Gmail` in a vault `Hermes`, field `credential`. Its reference is:

```
op://Hermes/Composio Gmail/credential
```

Create it (placeholder value):

```bash
op item create --category 'API Credential' --vault Hermes --title 'Composio Gmail' \
  'credential[password]=PLACEHOLDER-composio-api-key'
```

Either way: **the credential now lives only in your vault.** It is not in the
repo, not in a `.env`, not in your shell history.

---

## Step 2 — The BROKER: the host holds the credential, the agent never does

The naive approach is to inject `GMAIL_API_KEY` into the agent and let it call
Composio directly. But then the agent (and, without `docker_forward_env: []`,
the sandbox) holds a live credential. The **preferred Tier-2 pattern is a
broker**: a small host-side helper that reads the credential from the vault,
performs the read-only fetch itself, applies the subject-glob gate, and returns
**only the filtered message metadata**. The agent asks "any new matching mail?"
and gets a gated list back — the credential never enters the agent process or
the docker sandbox.

`gmail-broker.sh` in this directory is that broker (the single-machine,
no-daemon sibling of the HTTP host-bridge in
[`docs/security-hardening.md` §7](../../docs/security-hardening.md#7-privileged-helpers-the-host-bridge-pattern)).
It resolves the credential vault-first and **never prints it** — every output
line passes through a `scrub()` guard that redacts `x-api-key` / bearer headers,
Composio `ak_...` keys, `op://` refs, and JWT-shaped strings.

### Run the broker with a vault-resolved credential

```bash
export GMAIL_USER_ID="your-composio-user-id"
export GMAIL_QUERY="subject:invoice newer_than:7d"

# Secrets-Kit: broker reads GMAIL_API_KEY from the hermes service
GMAIL_VAULT=seckit bash gmail-broker.sh

# 1Password: broker reads the op:// reference
GMAIL_VAULT=op GMAIL_OP_REF="op://Hermes/Composio Gmail/credential" bash gmail-broker.sh
```

The credential is resolved into the **broker** process only and held in a shell
variable; it is attached to the request `x-api-key` header inside the broker and
is never emitted.

### How the agent invokes it (so it never holds the credential)

The agent runs an allowlisted command like `bash gmail-broker.sh` (or calls the
HTTP bridge's gmail-fetch operation). It receives the gated message metadata, not
the credential. With the docker terminal backend and `docker_forward_env: []`,
even a prompt-injected command in the sandbox cannot read `GMAIL_API_KEY` — it
simply is not present in that process.

---

## Step 3 — Fallback: scoped forward (only if a broker is impractical)

If you genuinely cannot run a broker, the lesser Tier-2 option is to forward
**exactly one** task secret into the sandbox and let the agent call the
read-only `composio_gmail` MCP tools directly:

```yaml
terminal:
  backend: docker
  docker_forward_env: [GMAIL_API_KEY]   # one secret, task-scoped; prefer the broker
```

Inject it for that run with `seckit run --names GMAIL_API_KEY -- ...` or
`op run --env-file .env.op -- ...`. This is weaker than the broker (the agent now
holds the key) — keep the Composio server's allowlist read-only and rotate the
key after use.

---

## Step 4 — Prove it (placeholder credential, offline)

You can prove the broker mechanism — including the no-leak guarantee — with a
**fake** credential and no network:

```bash
GMAIL_MOCK=1 GMAIL_API_KEY="placeholder-not-a-real-key" bash gmail-broker.sh \
  | tee /tmp/gmail-broker-proof.txt

# the placeholder must NOT appear anywhere in the output:
grep -F "placeholder-not-a-real-key" /tmp/gmail-broker-proof.txt \
  && echo "LEAK (fail)" || echo "no leak (pass)"
```

The mock path emits two representative messages (one matching the subject glob,
one not) so you can see the *shape* of a real run — fetch, deterministic gate,
and the read-only fields the agent would extract — without touching any account.

---

## The point

Nothing here is Gmail-specific. Swap `GMAIL_API_KEY` for a Slack token, a
Notion key, or any task credential and the same three moves apply: **store it in
a vault, broker the privileged action on the host, never let the agent hold the
secret.** Gmail-via-Composio is just the read-only retrieval task we happen to
run against our own inbox.
