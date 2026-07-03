#!/usr/bin/env bash
# =============================================================================
# control-client.sh — minimal reference client for the Hermes localhost
#                      control endpoint ("plug into the matrix").
#
# *** REQUIRES HERMES DESKTOP RUNNING ***
#
# The localhost control API is exposed by the separate **Hermes Desktop** app,
# which is NOT part of this repository. This script only talks to that endpoint;
# it cannot start it. If Desktop is not running there is no control server to
# reach, and this script will tell you so and exit.
#
# What it does (all GET / read-only — observe, do not steer):
#   1. Reads port + bearer token from the Desktop control.json file.
#   2. GET /health           — is the control server alive?
#   3. GET /sessions         — list known sessions.
#   4. GET /sessions/<id>/transcript — tail the transcript of one session.
#
# This is deliberately read-only. For steering (POST /chat,
# POST /sessions/<id>/message) see docs/operating.md §3 — the same token and
# port apply; we keep this reference client observation-only on purpose.
#
# Requirements: curl (required), jq (optional — used to pretty-print and to
# parse control.json; falls back to a grep/sed parser if jq is absent).
#
# Usage:
#   bash scripts/observe/control-client.sh                 # health + sessions
#   bash scripts/observe/control-client.sh <session-id>    # + transcript tail
#
# Override via environment variables (all optional):
#   CONTROL_JSON   Path to the Desktop control file.
#                  Default: $HOME/.hermes-desktop/control.json
#   CONTROL_HOST   Host the control server is bound to.
#                  Default: 127.0.0.1 (Desktop binds loopback only)
#   CONTROL_TAIL   How many trailing transcript lines to show.
#                  Default: 40
#   CONTROL_TIMEOUT  curl --max-time in seconds. Default: 15
# =============================================================================
set -euo pipefail

CONTROL_JSON="${CONTROL_JSON:-$HOME/.hermes-desktop/control.json}"
CONTROL_HOST="${CONTROL_HOST:-127.0.0.1}"
CONTROL_TAIL="${CONTROL_TAIL:-40}"
CONTROL_TIMEOUT="${CONTROL_TIMEOUT:-15}"

SESSION_ID="${1:-}"

err() { printf '%s\n' "$*" >&2; }

# --- Preconditions --------------------------------------------------------
command -v curl >/dev/null 2>&1 || { err "ERROR: curl is required."; exit 1; }

HAVE_JQ=0
if command -v jq >/dev/null 2>&1; then
  HAVE_JQ=1
fi

if [ ! -f "$CONTROL_JSON" ]; then
  err "ERROR: control file not found at: $CONTROL_JSON"
  err ""
  err "  Hermes Desktop is not running (or its control server is disabled)."
  err "  The control endpoint is provided by the separate Hermes Desktop app,"
  err "  which is NOT part of this repository. Start Desktop and try again."
  err "  (To opt out of the control server entirely, Desktop honours"
  err "   HERMES_DESKTOP_CONTROL_DISABLE=1.)"
  exit 1
fi

# --- Read port + token ----------------------------------------------------
# control.json shape: { "pid": <n>, "port": <n>, "started_at": "...", "token": "<bearer>" }
if [ "$HAVE_JQ" -eq 1 ]; then
  CONTROL_PORT="$(jq -r '.port // empty' "$CONTROL_JSON")"
  CONTROL_TOKEN="$(jq -r '.token // empty' "$CONTROL_JSON")"
else
  # Minimal fallback parser — extracts "port": <n> and "token": "<...>".
  CONTROL_PORT="$(sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$CONTROL_JSON" | head -n1)"
  CONTROL_TOKEN="$(sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONTROL_JSON" | head -n1)"
fi

if [ -z "${CONTROL_PORT:-}" ] || [ -z "${CONTROL_TOKEN:-}" ]; then
  err "ERROR: could not read port/token from $CONTROL_JSON"
  err "       Expected JSON with .port and .token (install jq for robust parsing)."
  exit 1
fi

BASE="http://${CONTROL_HOST}:${CONTROL_PORT}"

# --- Helpers --------------------------------------------------------------
# GET a path; pretty-print with jq when available, else raw.
control_get() {
  local path="$1"
  curl -sS --max-time "$CONTROL_TIMEOUT" \
    -H "Authorization: Bearer $CONTROL_TOKEN" \
    "${BASE}${path}"
}

pretty() {
  if [ "$HAVE_JQ" -eq 1 ]; then
    jq . 2>/dev/null || cat
  else
    cat
  fi
}

# --- 1. Health ------------------------------------------------------------
echo "== control endpoint =="
echo "endpoint: $BASE   (requires Hermes Desktop running)"
echo
echo "== GET /health =="
if ! control_get "/health" | pretty; then
  err ""
  err "ERROR: could not reach the control server at $BASE."
  err "       Desktop may have stopped, or it is bound to a different host."
  err "       (Override the bound host with CONTROL_HOST if needed.)"
  exit 1
fi

# --- 2. Sessions ----------------------------------------------------------
echo
echo "== GET /sessions =="
control_get "/sessions" | pretty

# --- 3. Transcript tail (optional) ----------------------------------------
if [ -n "$SESSION_ID" ]; then
  echo
  echo "== GET /sessions/${SESSION_ID}/transcript  (last ${CONTROL_TAIL} lines) =="
  if [ "$HAVE_JQ" -eq 1 ]; then
    control_get "/sessions/${SESSION_ID}/transcript" | jq . 2>/dev/null | tail -n "$CONTROL_TAIL" \
      || control_get "/sessions/${SESSION_ID}/transcript" | tail -n "$CONTROL_TAIL"
  else
    control_get "/sessions/${SESSION_ID}/transcript" | tail -n "$CONTROL_TAIL"
  fi
else
  echo
  echo "(tip: pass a session id to tail its transcript — see /sessions output above)"
fi
