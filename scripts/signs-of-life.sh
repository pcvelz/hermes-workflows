#!/usr/bin/env bash
#
# signs-of-life.sh — non-intrusive liveness harness for a Hermes Workflows
# deployment. Read-only probes only (no chat posts, no mutating calls). Prints
# a PASS/FAIL table and exits non-zero if any check FAILs.
#
# This script is PUBLIC-SAFE: it contains no real hostnames, tokens, or
# secrets. Every endpoint/path is read from an env var with a generic,
# loopback-only default. Bring your own values via env vars or a .env you
# source before running.
#
# Usage:
#   scripts/signs-of-life.sh --target native|container [--snapshot <path>]
#   scripts/signs-of-life.sh --help
#
# Checks performed:
#   1. backend-reachable   GET ${LLM_BASE_URL}/v1/models returns HTTP 200
#                           (falls back to GET ${LLM_BASE_URL}/models if the
#                           first probe 404s).
#   2. gateway-health       GET ${GATEWAY_HEALTH_URL} returns HTTP 200. If the
#                           endpoint is unreachable (build serves no HTTP
#                           health endpoint), this is SKIPped, not FAILed —
#                           gateway-platform is the authoritative liveness.
#   3. gateway-platform      Reads ${HERMES_HOME}/profiles/${HERMES_PROFILE}/
#                           gateway_state.json and checks that at least one
#                           platform (or the one named in ${GATEWAY_PLATFORM},
#                           if set) reports state == "connected".
#   4. container-up          (--target container only) `docker ps` shows
#                           ${CONTAINER_NAME} with status "Up".
#
# Env vars (all optional; safe generic defaults shown):
#   LLM_BASE_URL          Backend base URL to probe for /v1/models.
#                         Default: http://host.docker.internal:8001
#   GATEWAY_HEALTH_URL    Gateway health endpoint.
#                         Default: http://127.0.0.1:9876/health
#   HERMES_HOME           Runtime home directory (native target).
#                         Default: $HOME/.hermes
#   HERMES_PROFILE        Profile name under HERMES_HOME/profiles/.
#                         Default: orchestrator
#   GATEWAY_PLATFORM      If set, require this specific platform name to be
#                         connected instead of "any platform connected".
#   CONTAINER_NAME        Docker container name to check (--target container).
#                         Default: hermes-agent
#   CURL_MAX_TIME         Per-request curl timeout in seconds. Default: 5
#
# Exit codes:
#   0  all checks PASS
#   1  one or more checks FAIL
#   2  usage error
#
set -uo pipefail

SCRIPT_NAME="$(basename "$0")"

usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} --target native|container [--snapshot <path>]
       ${SCRIPT_NAME} --help

Non-intrusive liveness harness for a Hermes Workflows deployment. Read-only
probes only. Prints a PASS/FAIL table; exits non-zero if any check FAILs.

Options:
  --target native|container   Required. Which deployment topology to check.
                               "container" additionally checks the agent
                               container is Up via \`docker ps\`.
  --snapshot <path>            Optional. Write a JSON object of check->PASS/FAIL
                               (+ target) to <path>. No wall-clock timestamp is
                               embedded, so re-runs diff cleanly.
  -h, --help                   Show this help and exit.

Environment variables (all optional; generic loopback-only defaults):
  LLM_BASE_URL          Backend base URL probed for /v1/models.
                         Default: http://host.docker.internal:8001
  GATEWAY_HEALTH_URL    Gateway health endpoint.
                         Default: http://127.0.0.1:9876/health
  HERMES_HOME           Runtime home directory (native target).
                         Default: \$HOME/.hermes
  HERMES_PROFILE        Profile name under HERMES_HOME/profiles/.
                         Default: orchestrator
  GATEWAY_PLATFORM      If set, require this specific platform to be
                         connected instead of "any platform connected".
  CONTAINER_NAME        Docker container name to check (--target container).
                         Default: hermes-agent
  CURL_MAX_TIME         Per-request curl timeout in seconds. Default: 5

Examples:
  ${SCRIPT_NAME} --target native
  ${SCRIPT_NAME} --target container --snapshot /tmp/sol.json
  LLM_BASE_URL=http://127.0.0.1:8001 ${SCRIPT_NAME} --target native
EOF
}

target=""
snapshot=""

while [ $# -gt 0 ]; do
  case "$1" in
    --target)
      target="${2:-}"
      shift 2
      ;;
    --target=*)
      target="${1#*=}"
      shift
      ;;
    --snapshot)
      snapshot="${2:-}"
      shift 2
      ;;
    --snapshot=*)
      snapshot="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$target" ]; then
  echo "ERROR: --target is required (native|container)." >&2
  usage >&2
  exit 2
fi
if [ "$target" != "native" ] && [ "$target" != "container" ]; then
  echo "ERROR: --target must be 'native' or 'container' (got: $target)." >&2
  usage >&2
  exit 2
fi

LLM_BASE_URL="${LLM_BASE_URL:-http://host.docker.internal:8001}"
GATEWAY_HEALTH_URL="${GATEWAY_HEALTH_URL:-http://127.0.0.1:9876/health}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_PROFILE="${HERMES_PROFILE:-orchestrator}"
CONTAINER_NAME="${CONTAINER_NAME:-hermes-agent}"
CURL_MAX_TIME="${CURL_MAX_TIME:-5}"

# Accumulator: parallel arrays (bash 3.2-safe — no associative arrays).
check_names=()
check_results=()   # PASS or FAIL
check_details=()   # short human-readable detail

record() {
  check_names+=("$1")
  check_results+=("$2")
  check_details+=("$3")
}

have_curl=0
if command -v curl >/dev/null 2>&1; then
  have_curl=1
fi

# ---------------------------------------------------------------------------
# Check 1: backend reachable
# ---------------------------------------------------------------------------
if [ "$have_curl" -eq 1 ]; then
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$CURL_MAX_TIME" "${LLM_BASE_URL%/}/v1/models" 2>/dev/null)"
  code="${code:-000}"
  if [ "$code" = "200" ]; then
    record "backend-reachable" "PASS" "GET ${LLM_BASE_URL%/}/v1/models -> 200"
  else
    # Fall back to a non-/v1 /models path some backends expose.
    code2="$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$CURL_MAX_TIME" "${LLM_BASE_URL%/}/models" 2>/dev/null)"
    code2="${code2:-000}"
    if [ "$code2" = "200" ]; then
      record "backend-reachable" "PASS" "GET ${LLM_BASE_URL%/}/models -> 200"
    else
      record "backend-reachable" "FAIL" "GET ${LLM_BASE_URL%/}/v1/models -> ${code}, /models -> ${code2}"
    fi
  fi
else
  record "backend-reachable" "FAIL" "curl not found on PATH"
fi

# ---------------------------------------------------------------------------
# Check 2: gateway health
# ---------------------------------------------------------------------------
if [ "$have_curl" -eq 1 ]; then
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$CURL_MAX_TIME" "$GATEWAY_HEALTH_URL" 2>/dev/null)"
  code="${code:-000}"
  if [ "$code" = "200" ]; then
    record "gateway-health" "PASS" "GET ${GATEWAY_HEALTH_URL} -> 200"
  elif [ "$code" = "000" ]; then
    # Unreachable/refused: this gateway build may not serve an HTTP health
    # endpoint. Not a failure — gateway-platform is the authoritative liveness
    # signal. A served-but-non-200 response (below) is a real failure.
    record "gateway-health" "SKIP" "GET ${GATEWAY_HEALTH_URL} -> unreachable (build may not serve /health; see gateway-platform)"
  else
    record "gateway-health" "FAIL" "GET ${GATEWAY_HEALTH_URL} -> ${code}"
  fi
else
  record "gateway-health" "SKIP" "curl not found on PATH"
fi

# ---------------------------------------------------------------------------
# Check 3: gateway platform connected
# ---------------------------------------------------------------------------
gateway_state_path="${HERMES_HOME%/}/profiles/${HERMES_PROFILE}/gateway_state.json"
if [ -f "$gateway_state_path" ]; then
  if command -v python3 >/dev/null 2>&1; then
    py_out="$(GATEWAY_STATE_PATH="$gateway_state_path" GATEWAY_PLATFORM="${GATEWAY_PLATFORM:-}" python3 - <<'PYEOF' 2>/dev/null
import json
import os
import sys

path = os.environ.get("GATEWAY_STATE_PATH", "")
want_platform = os.environ.get("GATEWAY_PLATFORM", "")

try:
    with open(path, "r") as f:
        data = json.load(f)
except Exception as exc:
    print("ERROR:" + str(exc))
    sys.exit(0)

# gateway_state.json schemas vary by build: some are a flat
# {platform: {state}} map, others nest under a top-level "platforms" key.
# Descend into "platforms" when it is present as a dict.
if isinstance(data, dict) and isinstance(data.get("platforms"), dict):
    platforms = data["platforms"]
elif isinstance(data, dict):
    platforms = data
else:
    platforms = {}
connected = []
for name, info in platforms.items():
    state = None
    if isinstance(info, dict):
        state = info.get("state") or info.get("status")
    elif isinstance(info, str):
        state = info
    if state == "connected":
        connected.append(name)

if want_platform:
    if want_platform in connected:
        print("PASS:" + want_platform)
    else:
        print("FAIL:platform '" + want_platform + "' not connected (connected=" + ",".join(connected) + ")")
else:
    if connected:
        print("PASS:" + ",".join(connected))
    else:
        print("FAIL:no platform connected")
PYEOF
)"
    case "$py_out" in
      PASS:*)
        record "gateway-platform" "PASS" "connected: ${py_out#PASS:}"
        ;;
      FAIL:*)
        record "gateway-platform" "FAIL" "${py_out#FAIL:} (${gateway_state_path})"
        ;;
      ERROR:*)
        record "gateway-platform" "FAIL" "could not parse ${gateway_state_path}: ${py_out#ERROR:}"
        ;;
      *)
        record "gateway-platform" "FAIL" "could not evaluate ${gateway_state_path}"
        ;;
    esac
  else
    record "gateway-platform" "FAIL" "python3 not found on PATH (needed to parse ${gateway_state_path})"
  fi
else
  record "gateway-platform" "FAIL" "not found: ${gateway_state_path}"
fi

# ---------------------------------------------------------------------------
# Check 4: container up (container target only)
# ---------------------------------------------------------------------------
if [ "$target" = "container" ]; then
  if command -v docker >/dev/null 2>&1; then
    ps_line="$(docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep -F -- "$CONTAINER_NAME" || true)"
    if [ -n "$ps_line" ] && printf '%s' "$ps_line" | grep -qE '(^| )Up( |$)'; then
      record "container-up" "PASS" "${ps_line}"
    else
      record "container-up" "FAIL" "container '${CONTAINER_NAME}' not found or not Up"
    fi
  else
    record "container-up" "FAIL" "docker not found on PATH"
  fi
fi

# ---------------------------------------------------------------------------
# Print PASS/FAIL table
# ---------------------------------------------------------------------------
overall=0
printf '%-22s %-6s %s\n' "CHECK" "RESULT" "DETAIL"
i=0
while [ "$i" -lt "${#check_names[@]}" ]; do
  name="${check_names[$i]}"
  result="${check_results[$i]}"
  detail="${check_details[$i]}"
  printf '%-22s %-6s %s\n' "$name" "$result" "$detail"
  # SKIP is not a failure (an inapplicable check, e.g. a build with no HTTP
  # health endpoint). Only FAIL fails the run.
  if [ "$result" = "FAIL" ]; then
    overall=1
  fi
  i=$((i + 1))
done

# ---------------------------------------------------------------------------
# Optional snapshot JSON
# ---------------------------------------------------------------------------
if [ -n "$snapshot" ]; then
  snap_dir="$(dirname "$snapshot")"
  if [ ! -d "$snap_dir" ]; then
    mkdir -p "$snap_dir" 2>/dev/null || true
  fi
  {
    printf '{\n'
    printf '  "target": "%s",\n' "$target"
    printf '  "checks": {\n'
    j=0
    n="${#check_names[@]}"
    while [ "$j" -lt "$n" ]; do
      sep=","
      if [ "$j" -eq $((n - 1)) ]; then
        sep=""
      fi
      printf '    "%s": "%s"%s\n' "${check_names[$j]}" "${check_results[$j]}" "$sep"
      j=$((j + 1))
    done
    printf '  }\n'
    printf '}\n'
  } > "$snapshot"
fi

exit "$overall"
