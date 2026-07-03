#!/usr/bin/env bash
# =============================================================================
# llm-smoke-test.sh
#
# Smoke test for LLM backend endpoints — supports both protocols:
#
#   anthropic_messages (default): Anthropic Messages wire protocol.
#     Used by a local llama-swap proxy, Claude, or any hosted
#     Anthropic-compatible endpoint.
#     Tests: POST {BASE_URL}/v1/messages with x-api-key + anthropic-version.
#
#   openai: OpenAI-compatible endpoints.
#     Tests: GET /v1/models (model listed, exact id), then
#            POST /v1/chat/completions (PONG).
#
# The protocol is auto-selected via LLM_TRANSPORT (default: anthropic_messages).
# LLM_PROTOCOL is accepted as a back-compat alias when LLM_TRANSPORT is unset.
# Set LLM_TRANSPORT=openai (or LLM_PROTOCOL=openai) to test OpenAI-style backends.
#
# Default backend: a local llama-swap proxy (recommended default), speaking
# the Anthropic Messages protocol. Replace <PORT> and <your-model-alias> with
# your actual llama-swap port and model id (see docs/backend.md).
# IMPORTANT: do NOT append /v1 to the base URL; the SDK and this script
# append /v1/messages internally. Adding /v1 yourself causes a 404.
#
# Requirements: curl (required), jq (optional — falls back to grep if absent).
#
# Usage:
#   bash scripts/llm/llm-smoke-test.sh
#
# Override via environment variables (all optional):
#   LLM_BASE_URL    Base URL of the endpoint (no trailing /v1 for Anthropic-mode)
#                   Default: http://127.0.0.1:<PORT> (replace <PORT> with your
#                   llama-swap port; see docs/backend.md)
#   LLM_MODEL       Exact model id to test
#                   Default: <your-model-alias> (replace with your model id)
#   LLM_API_KEY     API key (x-api-key header for Anthropic, Bearer for OpenAI)
#                   Default: "local" (fine for a local proxy that needs no key)
#   LLM_TRANSPORT   Protocol: anthropic_messages (default) or openai
#                   Alias: LLM_PROTOCOL (back-compat; LLM_TRANSPORT takes priority)
#                   Normalized: anthropic|anthropic-messages → anthropic_messages
#   LLM_TIMEOUT     curl --max-time in seconds
#                   Default: 60
#
# ANTHROPIC_MESSAGES example (default — local llama-swap):
#   LLM_BASE_URL=http://127.0.0.1:8080 \
#   LLM_MODEL=<your-model-alias> \
#     bash scripts/llm/llm-smoke-test.sh
#
# ANTHROPIC_MESSAGES example (any hosted Anthropic-compatible endpoint):
#   LLM_BASE_URL=https://your-anthropic-compatible-endpoint.example \
#   LLM_MODEL=<YOUR_MODEL_NAME> \
#   LLM_API_KEY=$ANTHROPIC_API_KEY \
#     bash scripts/llm/llm-smoke-test.sh
#
# OPENAI example (local proxy, OpenRouter, etc.):
#   LLM_TRANSPORT=openai \
#   LLM_BASE_URL=http://127.0.0.1:8001/v1 \
#   LLM_MODEL=my-model \
#   LLM_API_KEY=local \
#     bash scripts/llm/llm-smoke-test.sh
#
# Note: for backends with cold-start latency (local model on demand), raise
# LLM_TIMEOUT to >=120 s to avoid spurious timeouts during model load.
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — env-overridable defaults
# ---------------------------------------------------------------------------
# Default to a local llama-swap proxy (Anthropic Messages, no /v1 suffix).
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_use_color=0
if [ -t 1 ]; then
  _use_color=1
fi

_color() {
  local code="$1"; shift
  if [ "$_use_color" -eq 1 ]; then
    printf "\033[%sm%s\033[0m\n" "$code" "$*"
  else
    printf "%s\n" "$*"
  fi
}

pass()  { _color "0;32" "[PASS] $*"; }
fail()  { _color "0;31" "[FAIL] $*"; (( _fail_count++ )) || true; }
info()  { _color "0;36" "[INFO] $*"; }
warn()  { _color "0;33" "[WARN] $*"; }

_fail_count=0

require() {
  local cmd="$1"
  if ! command -v "$cmd" &>/dev/null; then
    printf "\033[0;31m[ERROR] Required command not found: %s\033[0m\n" "$cmd" >&2
    printf "        Install curl and re-run this script.\n" >&2
    exit 2
  fi
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
require curl

if [ "$TIMEOUT" -lt 30 ] 2>/dev/null; then
  warn "TIMEOUT=${TIMEOUT}s is very low — consider raising LLM_TIMEOUT to >=60s."
fi

JQ_AVAILABLE=0
if command -v jq &>/dev/null; then
  JQ_AVAILABLE=1
fi

info "BASE_URL  : $BASE_URL"
info "MODEL     : $MODEL"
info "TRANSPORT : $TRANSPORT"
info "TIMEOUT   : ${TIMEOUT}s"
if [ "$JQ_AVAILABLE" -eq 0 ]; then
  info "jq not found — falling back to grep for JSON parsing"
fi
printf "\n"

# =============================================================================
# ANTHROPIC MESSAGES protocol
# POST {BASE_URL}/v1/messages
# Headers: x-api-key, anthropic-version, content-type
# =============================================================================
if [ "$TRANSPORT" = "anthropic_messages" ]; then

  # -------------------------------------------------------------------------
  # Test 1 (Anthropic) — POST /v1/messages with a minimal request.
  # There is no /models endpoint in the Anthropic Messages protocol;
  # we verify reachability and correct auth by sending a real (tiny) request.
  # -------------------------------------------------------------------------
  info "Test 1 — POST ${BASE_URL}/v1/messages (Anthropic Messages — reachability + model check)"

  _ANTH_BODY=$(cat <<EOF
{
  "model": "$MODEL",
  "max_tokens": 1,
  "messages": [{"role": "user", "content": "ping"}]
}
EOF
)

  _anth_body=""
  _anth_exit=0
  _anth_http_code=""
  _anth_body=$(
    curl -sS \
      --max-time "$TIMEOUT" \
      -w "\n__HTTP_CODE__:%{http_code}" \
      -X POST \
      -H "x-api-key: $API_KEY" \
      -H "anthropic-version: 2023-06-01" \
      -H "Content-Type: application/json" \
      -d "$_ANTH_BODY" \
      "${BASE_URL}/v1/messages" 2>&1
  ) || _anth_exit=$?

  if [ "$_anth_exit" -ne 0 ]; then
    fail "Test 1 - Reachability: curl failed (exit ${_anth_exit}). Is your backend running?"
    info "  Hint: check that BASE_URL is correct (no trailing /v1 for Anthropic-mode) and the backend is up."
    info "  See docs/backend.md for setup details."
  else
    _anth_http_code=$(printf '%s' "$_anth_body" | grep '__HTTP_CODE__:' | sed 's/.*__HTTP_CODE__://' || true)
    _anth_body_clean=$(printf '%s' "$_anth_body" | grep -v '__HTTP_CODE__:' || true)

    # 401/403 = wrong key but endpoint is live.
    # 200 or 4xx from model error = endpoint reachable.
    # 404 typically means wrong base URL or /v1 double-suffix.
    if [ "$_anth_http_code" = "404" ]; then
      fail "Test 1 - Reachability: got HTTP 404 — wrong base URL path?"
      info "  Hint: ensure LLM_BASE_URL has no /v1 suffix for Anthropic-Messages endpoints."
      info "  Local llama-swap example: LLM_BASE_URL=http://127.0.0.1:<PORT> (no /v1 suffix)"
      info "  Response (truncated): ${_anth_body_clean:0:200}"
    elif [ "$_anth_http_code" = "401" ] || [ "$_anth_http_code" = "403" ]; then
      warn "Test 1 - Reachability: endpoint live (HTTP ${_anth_http_code}) but auth rejected."
      info "  Set LLM_API_KEY to your backend's API key (default \"local\" for a proxy that needs no key)."
      info "  Response (truncated): ${_anth_body_clean:0:200}"
      (( _fail_count++ )) || true
    else
      # Check that the model id appears in the response (Anthropic echoes model in body).
      _model_found=0
      if [ "$JQ_AVAILABLE" -eq 1 ]; then
        if printf '%s' "$_anth_body_clean" | jq -e --arg m "$MODEL" '.model == $m' 2>/dev/null | grep -q "true"; then
          _model_found=1
        fi
      else
        if printf '%s' "$_anth_body_clean" | grep -qF "\"$MODEL\""; then
          _model_found=1
        fi
      fi

      if [ "$_model_found" -eq 1 ]; then
        pass "Test 1 - Reachability: endpoint live (HTTP ${_anth_http_code}), model '${MODEL}' confirmed in response"
      else
        # Endpoint answered but model id not found — could be error body.
        # If HTTP 2xx it's likely a model-not-found error; still counts as reachable.
        if printf '%s' "$_anth_http_code" | grep -q '^2'; then
          warn "Test 1 - Reachability: endpoint live (HTTP ${_anth_http_code}) but model id '${MODEL}' not echoed."
          info "  Response (truncated): ${_anth_body_clean:0:200}"
        else
          fail "Test 1 - Reachability: HTTP ${_anth_http_code} — endpoint returned error."
          info "  Response (truncated): ${_anth_body_clean:0:200}"
        fi
      fi
    fi
  fi

  printf "\n"

  # -------------------------------------------------------------------------
  # Test 2 (Anthropic) — PONG generation via /v1/messages
  # -------------------------------------------------------------------------
  info "Test 2 — POST ${BASE_URL}/v1/messages (\"Reply with exactly the single word: PONG\")"

  _PONG_BODY=$(cat <<EOF
{
  "model": "$MODEL",
  "max_tokens": 16,
  "messages": [{"role": "user", "content": "Reply with exactly the single word: PONG"}]
}
EOF
)

  _t_start=$(date +%s)
  _pong_body=""
  _pong_exit=0
  _pong_body=$(
    curl -sS \
      --max-time "$TIMEOUT" \
      -X POST \
      -H "x-api-key: $API_KEY" \
      -H "anthropic-version: 2023-06-01" \
      -H "Content-Type: application/json" \
      -d "$_PONG_BODY" \
      "${BASE_URL}/v1/messages" 2>&1
  ) || _pong_exit=$?
  _t_end=$(date +%s)
  _elapsed=$(( _t_end - _t_start ))

  if [ "$_pong_exit" -ne 0 ]; then
    fail "Test 2 - Completion: curl failed (exit ${_pong_exit}) after ${_elapsed}s."
    if [ "$_elapsed" -ge "$TIMEOUT" ]; then
      info "  This looks like a timeout. Raise LLM_TIMEOUT if your backend has cold-start latency."
    fi
    info "  See docs/backend.md — Smoke test section."
  else
    info "Test 2 - Completion: elapsed ${_elapsed}s$( [ "$_elapsed" -gt 10 ] && echo " (slow response — possible cold start)" || echo " (fast)" )"

    # Anthropic Messages response: .content[0].text (or .content[].text)
    _content=""
    if [ "$JQ_AVAILABLE" -eq 1 ]; then
      _content=$(printf '%s' "$_pong_body" | jq -r '.content[0].text // empty' 2>/dev/null || true)
    else
      _content=$(printf '%s' "$_pong_body" | grep -o '"text":"[^"]*"' | head -1 | sed 's/"text":"//;s/"$//' || true)
    fi

    _content_upper=$(printf '%s' "$_content" | tr '[:lower:]' '[:upper:]')

    if printf '%s' "$_content_upper" | grep -q "PONG"; then
      pass "Test 2 - Completion: response contains PONG (content: '${_content}')"
    else
      fail "Test 2 - Completion: expected PONG in response, got: '${_content}'"
      info "  Raw response (truncated to 300 chars): ${_pong_body:0:300}"
      info "  Hint: the backend is reachable but returned unexpected output."
      info "  Verify that LLM_MODEL is correct and the model is functioning."
    fi
  fi

# =============================================================================
# OPENAI protocol
# GET  {BASE_URL}/models
# POST {BASE_URL}/chat/completions
# Header: Authorization: Bearer <key>
# =============================================================================
else

  # -------------------------------------------------------------------------
  # Test 1 (OpenAI) — GET /models: endpoint reachable and model listed
  # -------------------------------------------------------------------------
  info "Test 1 — GET ${BASE_URL}/models"

  _models_body=""
  _curl_exit=0
  _models_body=$(
    curl -sS \
      --max-time "$TIMEOUT" \
      -H "Authorization: Bearer $API_KEY" \
      "${BASE_URL}/models" 2>&1
  ) || _curl_exit=$?

  if [ "$_curl_exit" -ne 0 ]; then
    fail "Test 1 - /models: curl failed (exit ${_curl_exit}). Is your backend running on this URL?"
    info "  Hint: check that your backend is active and LLM_BASE_URL is correct (include /v1 for OpenAI mode)."
    info "  See docs/backend.md for setup details."
  else
    _model_found=0
    if [ "$JQ_AVAILABLE" -eq 1 ]; then
      # Exact id match — compare the full string, not a substring
      if printf '%s' "$_models_body" | jq -e --arg m "$MODEL" '.data[] | select(.id == $m)' 2>/dev/null | grep -q '"id"'; then
        _model_found=1
      fi
    else
      # Grep fallback: look for the exact id surrounded by quotes
      if printf '%s' "$_models_body" | grep -qF "\"$MODEL\""; then
        _model_found=1
      fi
    fi

    if [ "$_model_found" -eq 1 ]; then
      pass "Test 1 - /models: model found (${MODEL})"
      if [ "$JQ_AVAILABLE" -eq 1 ]; then
        _ids=$(printf '%s' "$_models_body" | jq -r '.data[].id' 2>/dev/null | head -5 | tr '\n' '  ')
        info "  Available models (first 5): ${_ids}"
      fi
    else
      fail "Test 1 - /models: model '${MODEL}' NOT found in response (exact id match)."
      info "  Hint: the model may not be loaded, or the id may not match exactly."
      info "  Check that LLM_MODEL matches the id as returned by /models."
      info "  See docs/backend.md for details."
      info "  Response (truncated): ${_models_body:0:200}"
    fi
  fi

  printf "\n"

  # -------------------------------------------------------------------------
  # Test 2 (OpenAI) — /chat/completions: 'Say PONG' completion
  # -------------------------------------------------------------------------
  info "Test 2 — POST ${BASE_URL}/chat/completions (\"Reply with exactly the single word: PONG\")"

  _REQUEST_BODY=$(cat <<EOF
{
  "model": "$MODEL",
  "messages": [
    {"role": "user", "content": "Reply with exactly the single word: PONG"}
  ],
  "max_tokens": 16,
  "temperature": 0
}
EOF
)

  _t_start=$(date +%s)
  _completion_body=""
  _curl_exit2=0
  _completion_body=$(
    curl -sS \
      --max-time "$TIMEOUT" \
      -X POST \
      -H "Authorization: Bearer $API_KEY" \
      -H "Content-Type: application/json" \
      -d "$_REQUEST_BODY" \
      "${BASE_URL}/chat/completions" 2>&1
  ) || _curl_exit2=$?
  _t_end=$(date +%s)
  _elapsed=$(( _t_end - _t_start ))

  if [ "$_curl_exit2" -ne 0 ]; then
    fail "Test 2 - Completion: curl failed (exit ${_curl_exit2}) after ${_elapsed}s."
    if [ "$_elapsed" -ge "$TIMEOUT" ]; then
      info "  This looks like a timeout. Raise LLM_TIMEOUT if your backend has cold-start latency."
    fi
    info "  See docs/backend.md — Smoke test section."
  else
    info "Test 2 - Completion: elapsed ${_elapsed}s$( [ "$_elapsed" -gt 10 ] && echo " (slow response — possible cold start)" || echo " (fast)" )"

    _content=""
    if [ "$JQ_AVAILABLE" -eq 1 ]; then
      _content=$(printf '%s' "$_completion_body" | jq -r '.choices[0].message.content // empty' 2>/dev/null || true)
    else
      # Grep fallback: extract content value from JSON (simple heuristic)
      _content=$(printf '%s' "$_completion_body" | grep -o '"content":"[^"]*"' | head -1 | sed 's/"content":"//;s/"$//' || true)
    fi

    # Uppercase for case-insensitive PONG check
    _content_upper=$(printf '%s' "$_content" | tr '[:lower:]' '[:upper:]')

    if printf '%s' "$_content_upper" | grep -q "PONG"; then
      pass "Test 2 - Completion: response contains PONG (content: '${_content}')"
    else
      fail "Test 2 - Completion: expected PONG in response, got: '${_content}'"
      info "  Raw response (truncated to 300 chars): ${_completion_body:0:300}"
      info "  Hint: the backend is reachable but returned unexpected output."
      info "  Verify that LLM_MODEL is correct and the model is functioning."
    fi
  fi

fi  # end protocol branch

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
printf "\n"
if [ "$_fail_count" -eq 0 ]; then
  _color "0;32" "ALL CHECKS PASSED"
  exit 0
else
  _color "0;31" "SMOKE TEST FAILED (${_fail_count} check(s) failed)"
  printf "See [INFO] hints above. More details: docs/backend.md\n"
  exit 1
fi
