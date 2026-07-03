---
name: gmail-processing
description: "Read-only Gmail processing — retrieve matching mail, extract structured fields, summarize. Never send, modify, or delete."
---

# Gmail processing (read-only)

> **POC / design target.** This skill describes the procedure a standalone
> Hermes would follow. It is non-destructive: it **retrieves and reads** mail
> only. It never sends a reply, never marks-as-read, never labels, archives, or
> deletes. If a step would require anything other than a fetch/list/get, stop
> and report instead.

## Inputs (environment)

- `GMAIL_USER_ID` — the Composio connected-account id whose mailbox is read.
- `GMAIL_QUERY` — Gmail server-side prefilter, e.g. `subject:invoice newer_than:7d`.
- `GMAIL_SUBJECT_GLOB` — a case-insensitive glob applied to subjects after the
  fetch, e.g. `*invoice*` (a bare `*` matches everything).

> **Preferred: do not hold the credential at all.** Invoke the **broker**
> (`gmail-broker.sh`, see [`SECURE-SECRETS.md`](SECURE-SECRETS.md)) instead. The
> broker reads the Composio key from the vault (`seckit`/`op`), performs the
> read-only fetch on the host, applies the subject-glob gate, and returns only
> the already-filtered message metadata — the credential never enters the agent
> or the docker sandbox (`docker_forward_env: []`). If you must call the Gmail
> MCP tools directly from the agent loop, only the **read-only** tools are
> exposed (the Composio server is minted with a fetch/list/get allowlist —
> `GMAIL_FETCH_EMAILS`, `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`,
> `GMAIL_FETCH_MESSAGE_BY_THREAD_ID`, `GMAIL_LIST_THREADS`, `GMAIL_LIST_LABELS`,
> `GMAIL_GET_PROFILE`, `GMAIL_GET_ATTACHMENT`). There is no send/modify/delete
> tool to reach for.

## Memory (read at start, append at end)

Read the already-processed message-id list from your memory seed
(`MEMORY.seed.md.example` is the starting shape). Use it to **suppress** any
message you have already summarized so a scheduled run does not re-report the
same invoice every tick. On the very first run, **baseline**: record the ids of
all currently-matching messages without reporting them, so historical mail is
not replayed.

## Procedure

### 1. Retrieve candidate mail

- Prefer the broker: `bash gmail-broker.sh` returns the already-gated list (each
  KEEP line is a candidate). If you are calling the MCP directly, invoke
  `GMAIL_FETCH_EMAILS` with `query = ${GMAIL_QUERY}` and a small `max_results`.
- The query is a coarse server-side prefilter; do not assume it is exact.

### 2. Gate (deterministic — no tokens spent on misses)

- Keep a message only if **both**:
  1. its subject matches `${GMAIL_SUBJECT_GLOB}` (case-insensitive), **and**
  2. its message-id is **not** already in your processed list (from memory).
- Everything else is dropped silently. This is the cheap filter that keeps a
  scheduled run from doing LLM work on every irrelevant email.

### 3. Process each kept message (read-only)

For each surviving message:

- Fetch the full body by id (`GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`) if you only
  have a snippet.
- Extract the structured fields the task cares about. For the default
  `*invoice*` example that is: **sender**, **invoice number**, **amount +
  currency**, **due date**, and any **PO / reference**. Adapt the field set to
  your own query (an order-confirmation, a delivery notice, a 2FA code, …).
- Do **not** open links, click buttons, or drive a browser. This skill reads
  mail and extracts text; it does not act on the mail's contents. (Acting on a
  link — e.g. confirming something — is a separate, higher-trust workflow that
  this read-only example deliberately does not perform.)

### 4. Report

- **Nothing new:** report a single line, e.g.
  `Gmail OK — 0 new messages matching *invoice* in the last 7d.`
- **Findings:** otherwise a short, skimmable per-message summary:
  - sender + subject,
  - the extracted structured fields,
  - the message-id (so a human can find it).
- Send via the configured notification channel. Keep it terse — this runs on a
  schedule; a wall of text trains the human to ignore it.

## Memory update (end of run)

- Append the message-id of every message you reported to the processed list so
  it is suppressed next run. Cap the list (e.g. last 300 ids) to bound growth.
- Never drop a genuinely new matching message just to keep the report short.

## Hard boundaries

- **Read only.** Fetch/list/get only. No reply, no send, no mark-as-read, no
  label, no archive, no delete, no draft. The Composio server exposes no write
  tool; do not attempt to obtain one.
- **No browser actions on mail contents.** Extract and report; do not navigate
  to or click links found in the mail.
- If a task seems to warrant a write (replying, confirming, filing), surface it
  as a recommendation for the human — do not perform it.
