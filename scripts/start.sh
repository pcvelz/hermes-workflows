#!/usr/bin/env bash
#
# start.sh — launch the standalone Hermes gateway for one profile.
#
# This is the process your SECRETS LAYER wraps, so the backend API key is
# injected as an environment variable only at launch time (see docs/secrets.md):
#
#   seckit run --service hermes --names LLM_API_KEY -- ./scripts/start.sh    # Secrets-Kit
#   op run --env-file .env.op   -- ./scripts/start.sh                        # 1Password
#   ./scripts/start.sh                                                        # plain env / .env
#
# Requires the upstream NousResearch hermes-agent installed in a venv
# (see docs/getting-started.md). This is a POC launcher, not a service manager.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_PROFILE="${HERMES_PROFILE:-coder}"
HERMES_VENV="${HERMES_VENV:-$HERMES_HOME/venv}"

export HERMES_HOME

if [ ! -d "$HERMES_HOME" ]; then
  echo "ERROR: HERMES_HOME not found at $HERMES_HOME." >&2
  echo "       Run the getting-started setup (docs/getting-started.md) to initialise it." >&2
  echo "       Or set HERMES_HOME explicitly: HERMES_HOME=/path/to/dir ./scripts/start.sh" >&2
  exit 1
fi

# Prefer the venv python if present, else the system python3.
if [ -x "$HERMES_VENV/bin/python" ]; then
  PY="$HERMES_VENV/bin/python"
else
  PY="$(command -v python3 || true)"
fi
if [ -z "${PY:-}" ]; then
  echo "ERROR: no python found (looked in $HERMES_VENV/bin/python and on PATH)." >&2
  exit 1
fi

# The backend key should already be in the environment (injected by your secrets
# layer). Warn but do not block — some backends (a local proxy) need no key.
if [ -z "${LLM_API_KEY:-}" ]; then
  echo "WARN: no LLM_API_KEY in env (default \"local\") — is your secrets layer wrapping this?" >&2
fi

echo "Starting Hermes gateway: profile=${HERMES_PROFILE} HERMES_HOME=${HERMES_HOME}"
exec "$PY" -m hermes_cli.main --profile "$HERMES_PROFILE" gateway run
