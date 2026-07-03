#!/usr/bin/env bash
# =============================================================================
# tests/smoke/llm.sh — LLM backend reachability smoke test.
#
# Proves the configured backend is up and the configured model answers a
# deterministic prompt. Supports two protocols:
#
#   anthropic_messages (default): Anthropic Messages wire protocol.
#     POST {BASE_URL}/v1/messages with x-api-key + anthropic-version headers.
#     Used by a local llama-swap proxy, Claude, or any hosted
#     Anthropic-compatible endpoint.
#     There is no /models endpoint — reachability is tested via a real request.
#
#   openai: OpenAI-compatible endpoints.
#     1. GET  {BASE_URL}/models     — endpoint reachable, model listed (exact id).
#     2. POST {BASE_URL}/chat/completions "Say PONG" — real generation works.
#
# If the endpoint is unreachable this SKIPs (backend offline is not a repo bug);
# but if the endpoint answers yet fails the generation, that's a FAIL.
#
# Default backend: a local llama-swap proxy (Anthropic Messages — recommended
# default). NO /v1 suffix; the script appends /v1/messages internally.
#
# Env overrides:
#   LLM_BASE_URL    (default http://127.0.0.1:<PORT> — no /v1 for Anthropic mode)
#   LLM_MODEL       (default <your-model-alias> — exact id, not a substring)
#   LLM_API_KEY     (default "local")
#   LLM_TRANSPORT   (default anthropic_messages; set to openai for OpenAI-style APIs)
#               Alias: LLM_PROTOCOL (back-compat; LLM_TRANSPORT takes priority)
#               Normalized: anthropic|anthropic-messages → anthropic_messages
#   LLM_TIMEOUT     (default 60; raise to >=120 for backends with cold-start latency)
#
# ANTHROPIC_MESSAGES example (default — local llama-swap):
#   LLM_BASE_URL=http://127.0.0.1:8080 \
#   LLM_MODEL=<your-model-alias> \
#   bash scripts/test.sh smoke
#
# ANTHROPIC_MESSAGES example (any hosted Anthropic-compatible endpoint):
#   LLM_BASE_URL=https://your-anthropic-compatible-endpoint.example \
#   LLM_MODEL=<YOUR_MODEL_NAME> \
#   LLM_API_KEY=$ANTHROPIC_API_KEY \
#   bash scripts/test.sh smoke
#
# OPENAI example (local proxy, OpenRouter, etc.):
#   LLM_TRANSPORT=openai \
#   LLM_BASE_URL=http://127.0.0.1:8001/v1 \
#   LLM_MODEL=my-alias \
#   LLM_API_KEY=local \
#   LLM_TIMEOUT=180 bash scripts/test.sh smoke
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:<PORT>}"
MODEL="${LLM_MODEL:-<your-model-alias>}"
API_KEY="${LLM_API_KEY:-local}"
TIMEOUT="${LLM_TIMEOUT:-60}"
# Transport: canonical var LLM_TRANSPORT; LLM_PROTOCOL accepted as back-compat alias.
# Normalise so that "anthropic" and "anthropic-messages" both resolve to anthropic_messages.
TRANSPORT="${LLM_TRANSPORT:-${LLM_PROTOCOL:-anthropic_messages}}"
case "$TRANSPORT" in
  anthropic|anthropic-messages) TRANSPORT=anthropic_messages ;;
esac
# Enforce a minimum floor.
if ! [ "$TIMEOUT" -ge 30 ] 2>/dev/null; then TIMEOUT=60; fi

section "SMOKE: LLM backend"

if ! have curl; then
  skip "LLM backend — curl not installed"
  info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
  [ "$FAIL_COUNT" -eq 0 ]; exit $?
fi

info "BASE_URL=$BASE_URL  MODEL=$MODEL  TRANSPORT=$TRANSPORT  TIMEOUT=${TIMEOUT}s"

# =============================================================================
# ANTHROPIC MESSAGES protocol
# POST {BASE_URL}/v1/messages — x-api-key + anthropic-version headers
# =============================================================================
if [ "$TRANSPORT" = "anthropic_messages" ]; then

  # --- Probe 1: reachability via minimal /v1/messages request ----------------
  # The Anthropic Messages protocol has no /models endpoint.
  # We send max_tokens:1 to minimise cost; even a 4xx from a valid endpoint
  # proves the path is routable.
  _probe_body=""
  _probe_rc=0
  _probe_body=$(curl -sS --max-time "$TIMEOUT" \
    -w "\n__HTTP_CODE__:%{http_code}" \
    -X POST \
    -H "x-api-key: $API_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}" \
    "${BASE_URL}/v1/messages" 2>&1) || _probe_rc=$?

  if [ "$_probe_rc" -ne 0 ]; then
    # Transport-level failure — backend unreachable (environmental).
    skip "LLM backend /v1/messages unreachable (curl exit $_probe_rc) — is your backend up on $BASE_URL? Skipping (environmental)."
    info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
    [ "$FAIL_COUNT" -eq 0 ]; exit $?
  fi

  _probe_http=$(printf '%s' "$_probe_body" | grep '__HTTP_CODE__:' | sed 's/.*__HTTP_CODE__://' || true)
  _probe_clean=$(printf '%s' "$_probe_body" | grep -v '__HTTP_CODE__:' || true)

  if [ "$_probe_http" = "404" ]; then
    fail "LLM backend /v1/messages returned HTTP 404 — wrong base URL path?"
    info "  Hint: ensure LLM_BASE_URL has no /v1 suffix for Anthropic-Messages endpoints."
    info "  Local llama-swap example: LLM_BASE_URL=http://127.0.0.1:<PORT> (no /v1 suffix)"
    info "  Response: ${_probe_clean:0:160}"
    info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
    [ "$FAIL_COUNT" -eq 0 ]; exit $?
  fi

  if [ "$_probe_http" = "401" ] || [ "$_probe_http" = "403" ]; then
    fail "LLM backend /v1/messages reachable (HTTP ${_probe_http}) but auth rejected — set LLM_API_KEY."
    info "  Response: ${_probe_clean:0:160}"
    info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
    [ "$FAIL_COUNT" -eq 0 ]; exit $?
  fi

  pass "LLM backend /v1/messages reachable (HTTP ${_probe_http})"

  # --- Test: PONG generation via /v1/messages --------------------------------
  _req='{"model":"'"$MODEL"'","max_tokens":16,"messages":[{"role":"user","content":"Reply with exactly the single word: PONG"}]}'
  _t0=$(date +%s); _resp=""; _rc2=0
  _resp=$(curl -sS --max-time "$TIMEOUT" -X POST \
    -H "x-api-key: $API_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" \
    -d "$_req" \
    "${BASE_URL}/v1/messages" 2>&1) || _rc2=$?
  _t1=$(date +%s); _elapsed=$(( _t1 - _t0 ))

  if [ "$_rc2" -ne 0 ]; then
    fail "LLM backend generation curl failed (exit $_rc2) after ${_elapsed}s"
    info "  If this timed out, raise LLM_TIMEOUT (>=120 for cold-start backends)."
  else
    # Anthropic Messages response body: .content[0].text
    _content=""
    if have jq; then
      _content=$(printf '%s' "$_resp" | jq -r '.content[0].text // empty' 2>/dev/null || true)
    else
      _content=$(printf '%s' "$_resp" | grep -o '"text":"[^"]*"' | head -1 | sed 's/"text":"//; s/"$//' || true)
    fi
    _up=$(printf '%s' "$_content" | tr '[:lower:]' '[:upper:]')
    if printf '%s' "$_up" | grep -q "PONG"; then
      pass "LLM backend generation returned PONG (${_elapsed}s, content='${_content}')"
    else
      fail "LLM backend generation did not return PONG (${_elapsed}s, content='${_content}', raw=${_resp:0:160})"
    fi
  fi

# =============================================================================
# OPENAI protocol
# GET  {BASE_URL}/models — exact model id check
# POST {BASE_URL}/chat/completions — PONG generation
# Header: Authorization: Bearer <key>
# =============================================================================
else

  # --- Test 1: /models reachability and exact model id match -----------------
  _models=""; _rc=0
  _models=$(curl -sS --max-time "$TIMEOUT" \
    -H "Authorization: Bearer $API_KEY" \
    "${BASE_URL}/models" 2>&1) || _rc=$?

  if [ "$_rc" -ne 0 ]; then
    # Endpoint down — environmental, not a repo defect.
    skip "LLM backend /models unreachable (curl exit $_rc) — is your backend up on $BASE_URL? Skipping (environmental)."
    info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
    [ "$FAIL_COUNT" -eq 0 ]; exit $?
  fi

  # Exact id match: look for the model id surrounded by double-quotes in the JSON.
  # This avoids false positives from substring matches (e.g. "my-model" matching
  # inside "my-model-extended").
  if printf '%s' "$_models" | grep -qF "\"$MODEL\""; then
    pass "LLM backend /models reachable; model '$MODEL' listed (exact match)"
  else
    fail "LLM backend /models reachable but model '$MODEL' NOT listed with exact id (response: ${_models:0:160})"
    info "  Hint: set LLM_MODEL to match the exact id returned by /models."
  fi

  # --- Test 2: PONG generation -----------------------------------------------
  _req=$(cat <<EOF
{"model":"$MODEL","messages":[{"role":"user","content":"Reply with exactly the single word: PONG"}],"max_tokens":16,"temperature":0}
EOF
)
  _t0=$(date +%s); _resp=""; _rc2=0
  _resp=$(curl -sS --max-time "$TIMEOUT" -X POST \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d "$_req" \
    "${BASE_URL}/chat/completions" 2>&1) || _rc2=$?
  _t1=$(date +%s); _elapsed=$(( _t1 - _t0 ))

  if [ "$_rc2" -ne 0 ]; then
    fail "LLM backend generation curl failed (exit $_rc2) after ${_elapsed}s"
    info "  If this timed out, raise LLM_TIMEOUT (>=120 for cold-start backends)."
  else
    _content=""
    if have jq; then
      _content=$(printf '%s' "$_resp" | jq -r '.choices[0].message.content // empty' 2>/dev/null || true)
    else
      _content=$(printf '%s' "$_resp" | grep -o '"content"[: ]*"[^"]*"' | head -1 | sed 's/.*"content"[: ]*"//; s/"$//' || true)
    fi
    _up=$(printf '%s' "$_content" | tr '[:lower:]' '[:upper:]')
    if printf '%s' "$_up" | grep -q "PONG"; then
      pass "LLM backend generation returned PONG (${_elapsed}s, content='${_content}')"
    else
      fail "LLM backend generation did not return PONG (${_elapsed}s, content='${_content}', raw=${_resp:0:160})"
    fi
  fi

fi  # end protocol branch

section "LLM backend summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
