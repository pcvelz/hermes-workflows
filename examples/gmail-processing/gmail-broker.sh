#!/usr/bin/env bash
# =============================================================================
# gmail-broker.sh — host-side BROKER for the Gmail processing example
#
# WHY A BROKER?
#   The agent should NEVER hold the Gmail API credential. In the tiered secrets
#   model (docs/secrets.md "Secrets + the sandbox", Tier 2) the preferred pattern
#   for a task secret is the host-bridge BROKER: a small host-side helper that
#   holds the credential, performs exactly the privileged action, and returns
#   ONLY the result. The agent asks "any new matching mail?"; this broker
#   authenticates, fetches, and answers with redacted message metadata. The
#   credential never enters the agent process or the docker sandbox (which runs
#   with docker_forward_env: []).
#
#   This is the runnable, single-machine sibling of the HTTP host-bridge in
#   docs/security-hardening.md §7 (scripts/bridge/hermes-bridge.py). Same idea,
#   no daemon: the agent would invoke this broker (e.g. via an allowlisted
#   pre-approved command) instead of ever receiving the Gmail credential.
#
# READ-ONLY: this broker only RETRIEVES mail (the Composio Gmail MCP server it
# fronts is minted with a fetch/list/get allowlist — never send/modify/delete).
# It never sends, never modifies, never deletes, never marks-as-read. It reads.
#
# WHERE THE CREDENTIAL COMES FROM (vault-first, in priority order):
#   1. GMAIL_API_KEY already in the environment (e.g. injected by `seckit run
#      --names GMAIL_API_KEY` / `op run` for THIS broker process only). Used as-is.
#   2. seckit:   GMAIL_VAULT=seckit  → `seckit get GMAIL_API_KEY --raw` (service hermes)
#   3. 1Password: GMAIL_VAULT=op + GMAIL_OP_REF=op://Hermes/Composio Gmail/credential
#                                    → `op read "$GMAIL_OP_REF"`
#   The resolved credential is held in a shell variable in THIS process only and
#   is NEVER printed, logged, or echoed. All output is scrubbed (see scrub()).
#
# WHAT THE CREDENTIAL IS
#   The broker fronts a Composio Gmail "Tool Router" MCP server (read-only
#   allowlist). Composio holds the Gmail OAuth grant on your behalf; the broker
#   authenticates to Composio's REST tools API with a custom `x-api-key` header
#   (NOT Authorization: Bearer). GMAIL_USER_ID scopes which connected account is
#   used. Setup is documented in SECURE-SECRETS.md — no Google OAuth client, no
#   App Password, no IMAP here.
#
# Requirements: curl (required); jq (optional — richer parsing if present).
#
# Usage (vault-resolved credential — never on the command line):
#   # store once in your vault:
#   #   seckit set --name GMAIL_API_KEY --stdin --kind api_key --service hermes --account local-dev
#   #   (or 1Password: op item create ... → op://Hermes/Composio Gmail/credential)
#   export GMAIL_USER_ID="your-composio-user-id"
#   export GMAIL_QUERY="subject:invoice newer_than:7d"
#   GMAIL_VAULT=seckit bash gmail-broker.sh
#   GMAIL_VAULT=op GMAIL_OP_REF="op://Hermes/Composio Gmail/credential" bash gmail-broker.sh
#
# Usage (mock / offline proof — no network, no real account, placeholder cred):
#   GMAIL_MOCK=1 GMAIL_API_KEY="placeholder-not-a-real-key" bash gmail-broker.sh
#
# Environment variables:
#   GMAIL_USER_ID   Composio connected-account user id. REQUIRED unless GMAIL_MOCK=1.
#   GMAIL_API_KEY   Composio x-api-key, if injected directly (Tier-2 forward). Optional.
#   GMAIL_VAULT     "seckit" | "op" — where to resolve the credential if not in env.
#   GMAIL_OP_REF    op:// reference (when GMAIL_VAULT=op).
#   GMAIL_QUERY     Gmail server-side prefilter. Default: subject:invoice newer_than:7d
#   GMAIL_SUBJECT_GLOB  case-insensitive fnmatch-style glob applied to subjects.
#                   Default: *invoice*  (a '*' matches everything).
#   GMAIL_MAX       max_results for the fetch. Default: 10.
#   GMAIL_TIMEOUT   curl --max-time seconds. Default: 30.
#   GMAIL_API_URL   Composio tools execute endpoint. Default:
#                   https://backend.composio.dev/api/v3/tools/execute/GMAIL_FETCH_EMAILS
#   GMAIL_MOCK      If "1", skip the network and emit canned messages so the broker
#                   mechanism can be proven offline with a placeholder credential.
# =============================================================================

set -euo pipefail

USER_ID="${GMAIL_USER_ID:-}"
QUERY="${GMAIL_QUERY:-subject:invoice newer_than:7d}"
SUBJECT_GLOB="${GMAIL_SUBJECT_GLOB:-*invoice*}"
MAX="${GMAIL_MAX:-10}"
TIMEOUT="${GMAIL_TIMEOUT:-30}"
VAULT="${GMAIL_VAULT:-}"
OP_REF="${GMAIL_OP_REF:-}"
MOCK="${GMAIL_MOCK:-0}"
API_URL="${GMAIL_API_URL:-https://backend.composio.dev/api/v3/tools/execute/GMAIL_FETCH_EMAILS}"

# ---------------------------------------------------------------------------
# scrub(): the leak guard. Every line the broker prints passes through this so
# that even if a credential-shaped string ever reached stdout (e.g. echoed back
# inside a Composio error body) it is redacted before a human or the agent ever
# sees it. We redact: (a) the resolved credential's exact value, and (b) common
# credential shapes (Composio keys like ak_..., x-api-key headers, bearer
# headers, op:// refs, long JWTs).
# ---------------------------------------------------------------------------
scrub() {
  local cred_value="${1:-}"
  sed -E \
    -e "s/x-api-key:[[:space:]]*[A-Za-z0-9._~+/=-]+/x-api-key: <redacted>/Ig" \
    -e "s/Bearer[[:space:]]+[A-Za-z0-9._~+/=-]+/Bearer <redacted>/g" \
    -e "s/ak_[A-Za-z0-9._~+/=-]{8,}/<redacted-composio-key>/g" \
    -e "s/eyJ[A-Za-z0-9._~+/=-]{20,}/<redacted-jwt>/g" \
    -e "s#op://[^[:space:]\"']+#op://<redacted-ref>#g" \
    | { if [[ -n "$cred_value" ]]; then
          # Redact the exact resolved value too (belt and suspenders).
          sed -E "s/$(printf '%s' "$cred_value" | sed -E 's/[][\\.^$*+?(){}|/]/\\&/g')/<redacted-credential>/g"
        else
          cat
        fi
      }
}

emit() {
  # Print a line, always scrubbed against the live credential.
  printf '%s\n' "$1" | scrub "${API_KEY:-}"
}

# ---------------------------------------------------------------------------
# glob_match(): case-insensitive fnmatch-style match of $1 (subject) against the
# configured GLOB. Uses bash's [[ == ]] glob (so * and ? work).
# ---------------------------------------------------------------------------
glob_match() {
  local subject_lc glob_lc
  subject_lc="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  glob_lc="$(printf '%s' "$SUBJECT_GLOB" | tr '[:upper:]' '[:lower:]')"
  # shellcheck disable=SC2053
  [[ "$subject_lc" == $glob_lc ]]
}

# ---------------------------------------------------------------------------
# Resolve the credential (vault-first). Held in $API_KEY, never printed.
# ---------------------------------------------------------------------------
API_KEY="${GMAIL_API_KEY:-}"

if [[ -z "$API_KEY" ]]; then
  case "$VAULT" in
    seckit)
      if ! command -v seckit >/dev/null 2>&1; then
        echo "ERROR: GMAIL_VAULT=seckit but seckit not found on PATH." >&2; exit 2
      fi
      API_KEY="$(seckit get GMAIL_API_KEY --raw --service hermes 2>/dev/null || seckit get GMAIL_API_KEY --raw 2>/dev/null || true)"
      ;;
    op)
      if ! command -v op >/dev/null 2>&1; then
        echo "ERROR: GMAIL_VAULT=op but the 1Password CLI (op) not found on PATH." >&2; exit 2
      fi
      [[ -n "$OP_REF" ]] || { echo "ERROR: GMAIL_VAULT=op requires GMAIL_OP_REF=op://..." >&2; exit 2; }
      API_KEY="$(op read "$OP_REF" 2>/dev/null || true)"
      ;;
    "")
      : # no vault requested; will validate below
      ;;
    *)
      echo "ERROR: unknown GMAIL_VAULT='$VAULT' (use 'seckit' or 'op')." >&2; exit 2
      ;;
  esac
fi

if [[ -z "$API_KEY" ]]; then
  echo "ERROR: no GMAIL_API_KEY resolved. Provide GMAIL_API_KEY, or set GMAIL_VAULT=seckit|op." >&2
  echo "  (the credential is resolved from the vault into this broker only; the agent never holds it)" >&2
  exit 2
fi

emit "Gmail processing via BROKER (read-only retrieve)"
emit "  credential source: ${VAULT:-env-injected}   (value held in broker process only, never printed)"
emit "  query: ${QUERY}    subject glob: ${SUBJECT_GLOB}"

# ---------------------------------------------------------------------------
# MOCK path — prove the broker mechanism offline with a placeholder credential.
# No network is touched; we emit two representative messages (one matching the
# glob, one not) so the proof can assert (a) the broker runs end-to-end, (b) the
# gate filters correctly, and (c) nothing leaks the credential.
# ---------------------------------------------------------------------------
if [[ "$MOCK" == "1" ]]; then
  emit "  MODE: MOCK (no network; canned messages; placeholder credential accepted)"
  emit ""
  emit "[fetch] (mock) 2 messages returned by the query prefilter:"
  emit ""

  # Two canned messages. Subjects deliberately straddle the default glob so the
  # gate's keep/drop behaviour is visible. No real addresses — example.com only.
  mock_subjects=(
    "Invoice 2026-0042 from Example Supplier B.V."
    "Your weekly newsletter — nothing to do here"
  )
  mock_from=(
    "billing@example.com"
    "news@example.com"
  )
  mock_id=("mock-msg-aaaa1111" "mock-msg-bbbb2222")
  mock_snippet=(
    "Attached: invoice INV-2026-0042, amount EUR 149.00, due 2026-07-15. PO #example."
    "This week at Example: three articles you probably will not read."
  )

  kept=0
  for i in 0 1; do
    subj="${mock_subjects[$i]}"
    if glob_match "$subj"; then
      kept=$((kept + 1))
      emit "  KEEP  id=${mock_id[$i]}"
      emit "        from:    ${mock_from[$i]}"
      emit "        subject: ${subj}"
      emit "        snippet: ${mock_snippet[$i]}"
    else
      emit "  drop  id=${mock_id[$i]}  subject: ${subj}   (does not match ${SUBJECT_GLOB})"
    fi
    emit ""
  done

  emit "[result] ${kept} message(s) matched the subject glob and would be handed to the agent."
  emit ""
  emit "What the agent does next (read-only — see processing-skill.md):"
  emit "  - For each KEPT message, fetch the full body via GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID."
  emit "  - Extract the structured fields the skill asks for (here: invoice number, amount, due date)."
  emit "  - Append a one-line summary to its memory and report via the configured channel."
  emit "  - It NEVER replies, marks-as-read, labels, archives, or deletes. Retrieve + summarize only."
  emit ""
  emit "Broker complete (mock). Credential was never printed."
  exit 0
fi

# ---------------------------------------------------------------------------
# LIVE path — real read-only retrieve via the Composio tools execute endpoint.
# The credential is attached to the request header inside this broker; the
# header is never emitted (scrub strips x-api-key / Bearer).
# ---------------------------------------------------------------------------
[[ -n "$USER_ID" ]] || { echo "ERROR: set GMAIL_USER_ID (your Composio connected-account id), or use GMAIL_MOCK=1." >&2; exit 2; }

HAVE_JQ=0
command -v jq >/dev/null 2>&1 && HAVE_JQ=1

emit "  Endpoint: ${API_URL}"
emit "  user_id:  ${USER_ID}"
emit ""

# Build the request body. The Composio tool GMAIL_FETCH_EMAILS takes a Gmail
# server-side query + max_results. This is a fetch (read) tool only.
req_body="$(
  if [[ "$HAVE_JQ" -eq 1 ]]; then
    jq -nc --arg uid "$USER_ID" --arg q "$QUERY" --argjson n "$MAX" \
      '{user_id:$uid, arguments:{max_results:$n, query:$q}}'
  else
    printf '{"user_id":"%s","arguments":{"max_results":%s,"query":"%s"}}' \
      "$USER_ID" "$MAX" "$QUERY"
  fi
)"

emit "[fetch] POST GMAIL_FETCH_EMAILS (read-only) ..."
if ! resp="$(curl -fsS --max-time "$TIMEOUT" -X POST \
      -H "x-api-key: ${API_KEY}" \
      -H "Content-Type: application/json" \
      --data "$req_body" \
      "$API_URL" 2>/dev/null)"; then
  emit "      FAIL — Composio tools API unreachable or returned an error (auth/url/network)."
  emit "             Check GMAIL_USER_ID, the credential, and that the connected account is live."
  exit 1
fi

if [[ "$HAVE_JQ" -ne 1 ]]; then
  emit "      OK — response received. (install jq to parse + gate messages)"
  emit "      Raw message count cannot be parsed without jq; the agent would parse it instead."
  emit ""
  emit "Broker complete. Credential was held in this process only and never printed."
  exit 0
fi

# Extract messages array; tolerate both {data:{messages:[...]}} and {messages:[...]}.
msgs="$(printf '%s' "$resp" | jq -c '(.data.messages // .messages // [])')"
total="$(printf '%s' "$msgs" | jq 'length')"
emit "      OK — ${total} message(s) returned by the query prefilter."
emit ""

kept=0
# Iterate messages; apply the subject glob gate in the broker so the agent only
# ever sees the already-filtered, metadata-only view (never the credential).
while IFS= read -r m; do
  [[ -z "$m" ]] && continue
  subj="$(printf '%s' "$m" | jq -r '.subject // ""')"
  from="$(printf '%s' "$m" | jq -r '.sender // .from // "unknown"')"
  mid="$(printf '%s' "$m"  | jq -r '.messageId // .id // "unknown"')"
  snip="$(printf '%s' "$m" | jq -r '(.snippet // .preview // "") | .[0:160]')"
  if glob_match "$subj"; then
    kept=$((kept + 1))
    emit "  KEEP  id=${mid}"
    emit "        from:    ${from}"
    emit "        subject: ${subj}"
    [[ -n "$snip" ]] && emit "        snippet: ${snip}"
  else
    emit "  drop  id=${mid}  subject: ${subj}   (does not match ${SUBJECT_GLOB})"
  fi
  emit ""
done < <(printf '%s' "$msgs" | jq -c '.[]')

emit "[result] ${kept} message(s) matched the subject glob and would be handed to the agent."
emit ""
emit "The agent processes the kept messages READ-ONLY (see processing-skill.md):"
emit "  fetch full body by id, extract structured fields, summarize, report. Never write."
emit ""
emit "Broker complete. Credential was held in this process only and never printed."
