#!/usr/bin/env bash
# regen-container-config.sh — regenerate the container base_url overlay configs
# from the live host source (~/.hermes/**). These files are GENERATED, never
# hand-edited, or they silently drift on the next live config change.
#
# Only substitution: the loopback backend host -> the docker host-gateway.
#   http://127.0.0.1:8001  ->  http://host.docker.internal:8001
# (api.kimi.com, api.anthropic.com, and every other host are left untouched.)
#
# Usage:  bash regen-container-config.sh            # regenerate in place
#         bash regen-container-config.sh --check    # diff-only, non-zero if drifted
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
swap='s#http://127\.0\.0\.1:8001#http://host.docker.internal:8001#g'

# <profile-label> <live-source> <overlay-dest>
PROFILES=(
  "root|$HOME/.hermes/config.yaml|$here/config.root.yaml"
  "private|$HOME/.hermes/profiles/private/config.yaml|$here/config.private.yaml"
  "coding|$HOME/.hermes/profiles/coding/config.yaml|$here/config.coding.yaml"
)

check=0; [ "${1:-}" = "--check" ] && check=1
rc=0

gen() { # <label> <src> <dst>
  local label="$1" src="$2" dst="$3"
  if [ ! -f "$src" ]; then
    echo "  SKIP: $label — live source not found at $src" >&2
    return
  fi
  if [ "$check" -eq 1 ]; then
    if [ ! -f "$dst" ] || ! diff -q <(sed "$swap" "$src") "$dst" >/dev/null 2>&1; then
      echo "  DRIFT: $(basename "$dst") differs from sed($label) — run without --check" >&2
      rc=1
    else
      echo "  ok: $(basename "$dst") in sync with $label"
    fi
  else
    sed "$swap" "$src" > "$dst"
    echo "  regenerated $(basename "$dst")  <-  $label"
  fi
}

for entry in "${PROFILES[@]}"; do
  IFS='|' read -r label src dst <<< "$entry"
  gen "$label" "$src" "$dst"
done

if [ "$check" -eq 1 ]; then
  [ "$rc" -eq 0 ] && echo "PASS: container configs in sync."
  exit "$rc"
fi
echo "done — recreate the container to apply new/changed mounts:"
echo "  cd $here/.. && docker compose --env-file local-overlays/bringup.env -f docker/docker-compose.yml -f docker-compose.patches.yml up -d --no-deps --force-recreate agent"
