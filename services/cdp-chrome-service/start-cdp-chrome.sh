#!/bin/bash
# Launch the newest Playwright-cached "Google Chrome for Testing" headless, with
# CDP on 127.0.0.1:${CDP_PORT:-9222}. Resolves the binary at start so Playwright
# cache bumps (chromium-NNNN) do not break the service.
#
# Runs from a COPY in $HERMES_HOME/launchd-bin (installed by
# scripts/install-services.py): launchd may not execute files under ~/Documents.
set -euo pipefail

PORT="${CDP_PORT:-9222}"
CACHE="$HOME/Library/Caches/ms-playwright"

BIN=$(ls -d "$CACHE"/chromium-*/chrome-mac-arm64/"Google Chrome for Testing.app"/Contents/MacOS/"Google Chrome for Testing" 2>/dev/null | sort -V | tail -1)
if [[ -z "$BIN" ]]; then
    echo "no Playwright chromium found under $CACHE; run: npx playwright install chromium" >&2
    exit 1
fi

# Major version of the binary actually launched, so the UA matches it.
MAJOR=$("$BIN" --version 2>/dev/null | sed -E 's/.* ([0-9]+)\..*/\1/')
if [[ ! "$MAJOR" =~ ^[0-9]+$ ]]; then
    echo "could not read Chrome major version from $BIN" >&2
    exit 1
fi
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${MAJOR}.0.0.0 Safari/537.36"

exec "$BIN" \
    --headless=new \
    --no-sandbox \
    --disable-gpu \
    --user-agent="$UA" \
    --disable-blink-features=AutomationControlled \
    --remote-debugging-port="$PORT" \
    --remote-debugging-address=127.0.0.1 \
    about:blank
