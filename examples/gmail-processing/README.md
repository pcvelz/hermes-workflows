# Example: Gmail processing (dog-food workflow)

> **Status: proof-of-concept / design target — not a running system.**
> This example demonstrates *how* a standalone Hermes would point a scheduled
> agent at a Gmail inbox, **retrieve** matching mail read-only, extract a few
> structured fields, and remember what it has already handled between runs. The
> cron job here is a **starter prompt template**, not an active schedule, and the
> agent adapters it would call are unimplemented in this scaffold (see the repo
> [`README.md`](../../README.md) and [`docs/backend.md`](../../docs/backend.md)).
> Nothing here sends, modifies, labels, or deletes mail.

This is a second "eat our own dog food" target, alongside
[`home-assistant-healthcheck`](../home-assistant-healthcheck/). A common, boring,
useful thing an always-on agent can do is watch a mailbox for a class of message
you care about — invoices, order confirmations, a specific sender — pull out the
fields that matter, and tell you, without you living in your inbox.

It is deliberately the **read-only half** of the original workflow this was
migrated from. The earlier private version also *acted* on a mail (opening a
confirmation link in a browser). That action half is intentionally **not** part
of this example: this POC retrieves and summarizes only.

---

## What it does (and does not do)

**Does (read-only):**

- **Retrieve** recent mail via a Gmail server-side prefilter query
  (`GMAIL_QUERY`, e.g. `subject:invoice newer_than:7d`).
- **Gate deterministically** — keep a message only if its subject matches
  `GMAIL_SUBJECT_GLOB` (e.g. `*invoice*`) **and** its id is not already in the
  agent's processed list. No LLM tokens are spent on misses.
- **Baseline on first run** — record currently-matching ids without reporting, so
  historical mail is never replayed.
- **Extract structured fields** from each kept message (for invoices: sender,
  invoice number, amount, due date, PO/reference) and report a short summary
  through whatever channel you wire up.

**Does not:**

- Never sends, replies, marks-as-read, labels, archives, drafts, or deletes.
  Fetch/list/get only — the Composio Gmail MCP server is minted with a read-only
  allowlist, so there is no write tool to reach for.
- Never opens or clicks links found in the mail. It extracts text; it does not
  act on the mail's contents. (Acting on a link is a separate higher-trust
  workflow this read-only example deliberately omits.)

---

## Point it at your own Gmail

Everything is driven by environment variables. There are **no real email
addresses, user ids, or credentials in this repo** — fill in your own.

| Variable             | Example                          | Meaning                                                                 |
| -------------------- | -------------------------------- | ----------------------------------------------------------------------- |
| `GMAIL_USER_ID`      | `your-composio-user-id`          | Your Composio connected-account id (scopes which mailbox is read).      |
| `GMAIL_API_KEY`      | `${GMAIL_API_KEY}`               | Composio **x-api-key** (placeholder — never commit it).                 |
| `GMAIL_QUERY`        | `subject:invoice newer_than:7d`  | Gmail server-side prefilter.                                            |
| `GMAIL_SUBJECT_GLOB` | `*invoice*`                      | Case-insensitive post-filter glob on the subject (`*` matches all).     |

This example fronts Gmail with a **Composio Tool Router MCP server** scoped to a
read-only allowlist — no Google OAuth client, no `token.json`, no App Password,
no IMAP in this repo. The only secret you store is the Composio key. Set it up
and store it in a secrets manager — see [`SECURE-SECRETS.md`](SECURE-SECRETS.md)
in this directory, which walks our Composio key through the repo's generic vault
+ broker model (Secrets-Kit **or** 1Password, equal footing). **Preferred: the
broker** (`gmail-broker.sh`) so the agent never holds the credential at all.

```bash
export GMAIL_USER_ID="your-composio-user-id"
export GMAIL_API_KEY="…"   # injected from your secrets store, never hard-coded
```

---

## Files in this example

| File                      | What it is                                                                                                                            |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `run.sh`                  | **Entrypoint** — orchestrates the example: checks prerequisites and runs the broker. Has a `GMAIL_MOCK=1` offline mode. Start here.  |
| `gmail-broker.sh`         | **Broker**: reads the credential from the vault, retrieves mail read-only, applies the subject-glob gate, never prints the credential. Has a `GMAIL_MOCK=1` offline proof mode. |
| `SECURE-SECRETS.md`       | Dog-food walkthrough: our Composio key through the generic vault + **broker** model.                                                 |
| `jobs.json.example`       | Cron job definition (repo cron shape) — a **starter prompt template** (`enabled:false`).                                             |
| `processing-skill.md`     | The workflow prompt / skill the agent runs: read-only retrieve → gate → extract → report.                                           |
| `MEMORY.seed.md.example`  | Memory-seed example — the processed-id list + no-replay baseline the agent retains between runs.                                     |

---

## Secure secrets: use the broker (preferred)

The recommended way to give this workflow a credential is the **broker** — the
host reads the Composio key from your vault and performs the fetch itself, so the
agent (and the docker sandbox) never holds it. Full walkthrough:
[`SECURE-SECRETS.md`](SECURE-SECRETS.md). Quick version:

```bash
export GMAIL_USER_ID="your-composio-user-id"
export GMAIL_QUERY="subject:invoice newer_than:7d"
GMAIL_VAULT=seckit bash examples/gmail-processing/gmail-broker.sh
# or 1Password:
GMAIL_VAULT=op GMAIL_OP_REF="op://Hermes/Composio Gmail/credential" \
  bash examples/gmail-processing/gmail-broker.sh
```

**Prove it offline (placeholder credential, no network):**

```bash
GMAIL_MOCK=1 GMAIL_API_KEY="placeholder-not-a-real-key" \
  bash examples/gmail-processing/gmail-broker.sh | tee /tmp/proof.txt
grep -F "placeholder-not-a-real-key" /tmp/proof.txt && echo LEAK || echo "no leak (pass)"
```

The broker scrubs every output line (`x-api-key` / bearer headers, Composio
`ak_...` keys, `op://` refs, JWT-shaped strings) so the credential can never
reach stdout, the agent, or a log.

---

## Run the example

The canonical entrypoint is `run.sh`. It verifies prerequisites and then invokes
the broker.

**Offline proof (no real account or credential needed):**

```bash
GMAIL_MOCK=1 bash examples/gmail-processing/run.sh
```

**Live run — broker resolves the credential from your vault (preferred):**

```bash
export GMAIL_USER_ID="your-composio-user-id"
export GMAIL_QUERY="subject:invoice newer_than:7d"
GMAIL_VAULT=seckit bash examples/gmail-processing/run.sh
# or 1Password:
GMAIL_VAULT=op GMAIL_OP_REF="op://Hermes/Composio Gmail/credential" \
  bash examples/gmail-processing/run.sh
```

**Live run — direct credential injection (fallback):**

```bash
export GMAIL_USER_ID="your-composio-user-id"
export GMAIL_API_KEY="…"   # Composio x-api-key — never commit it
bash examples/gmail-processing/run.sh
```

If that prints a gated list of matching messages (KEEP / drop lines), the agent
would have everything it needs to process them.

---

## How this would fire in a real deployment

This example is wired to demonstrate the two scaffold mechanisms working
together — **honestly, as a design target, not a measured run**:

1. **Cron** (`jobs.json.example`) schedules the pass on an interval. In this
   scaffold, cron entries are starter prompt templates; a real deployment would
   load them into the gateway's dispatcher
   (`docs/architecture/data-flow.md`).
2. **Memory** (`MEMORY.seed.md.example`) is the per-run context the agent keeps.
   On each run it reads its processed-id list so it does not re-report mail it has
   already summarized, and appends newly-handled ids — with a no-replay baseline
   on first start.

When the dispatcher and Gmail adapter are implemented, this same job + memory
pair would run unattended. Until then, the broker is the part you can actually
run today.

---

## Provenance (migrated from a private workflow)

This example is the de-leaked, read-only descendant of a private "Gmail watcher"
that followed the same **poll → gate → act → state** shape. Two things were
stripped to make it a safe, generic POC:

- **The act half.** The original opened a confirmation link in a browser and
  clicked through it. That browser-action step is **not** included here — this
  example only retrieves and summarizes. Acting on mail contents is a separate,
  higher-trust workflow.
- **All personal coupling.** No real email address, no real user id, no real key,
  no `/Users/...` paths. Everything is an environment variable with a placeholder
  default, and the credential flows through the vault + broker pattern.

The clean, generic parts were kept: the server-side query prefilter, the
deterministic subject-glob + already-seen gate, the no-replay first-run baseline,
and the processed-id state — all of which are reusable for any mailbox-watching
workflow.
