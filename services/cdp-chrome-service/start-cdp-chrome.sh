#!/bin/bash
# Launch the newest Playwright-cached "Google Chrome for Testing" headless, with
# CDP on 127.0.0.1:${CDP_PORT:-9333} (9222 is the Claude agent Chromium). Resolves the binary at start so Playwright
# cache bumps (chromium-NNNN) do not break the service.
#
# ON-DEMAND: the launchd job is not RunAtLoad/KeepAlive. Consumers start it via
# ensure-cdp-chrome.sh (launchctl kickstart). This script also owns the idle
# stop: Chrome runs as a child and a watchdog kills it, then exits 0, once
#   - the CDP target set (ids + urls) has not changed for CDP_IDLE_SECONDS, and
#   - $CDP_USE_MARKER (touched by ensure-cdp-chrome.sh on every REAL use) is
#     older than CDP_IDLE_SECONDS. This script never touches the marker itself:
#     a start is not a use, so a scheduled one-shot consumer does not hold the
#     browser up for the whole idle window. A missing marker counts from the start.
# A parked tab therefore does not keep it alive forever; a blank-only browser
# is trivially "unchanged". Unreadable endpoint = treated as in use (bias).
# SIGTERM (a consumer's release: `launchctl kill SIGTERM gui/<uid>/<label>`)
# stops Chrome and exits 0. Both stop paths (release and idle) go through
# stop_chrome: SIGTERM, up to CDP_STOP_GRACE_SECONDS (default 10) for Chrome to
# exit, then SIGKILL on Chrome and its children, so a Chrome that ignores SIGTERM
# can never leave this script (and the launchd job) hanging in `wait`.
#
# Runs from a COPY in $HERMES_HOME/launchd-bin (installed by
# scripts/install-services.py): launchd may not execute files under ~/Documents.
set -euo pipefail

PORT="${CDP_PORT:-9333}"
IDLE="${CDP_IDLE_SECONDS:-900}"
POLL="${CDP_POLL_SECONDS:-30}"
MARKER="${CDP_USE_MARKER:-$HOME/.hermes/state/cdp-chrome-last-use}"
CACHE="${CDP_CACHE_DIR:-$HOME/Library/Caches/ms-playwright}"
GRACE="${CDP_STOP_GRACE_SECONDS:-10}"

# The lookup must not kill the script under `set -o pipefail` when no
# chromium-* dir matches (ls exits 1, empty pipeline): fall through to the
# explicit error line below instead of dying silently.
BIN=$( { ls -d "$CACHE"/chromium-*/chrome-mac-arm64/"Google Chrome for Testing.app"/Contents/MacOS/"Google Chrome for Testing" 2>/dev/null || true; } | sort -V | tail -1 )
if [[ -z "$BIN" ]]; then
    echo "$(date '+%F %T') ERROR: no Playwright chromium binary under $CACHE — restore it with: npx playwright install chromium" >&2
    exit 1
fi

# Major version of the binary actually launched, so the UA matches it.
MAJOR=$("$BIN" --version 2>/dev/null | sed -E 's/.* ([0-9]+)\..*/\1/')
if [[ ! "$MAJOR" =~ ^[0-9]+$ ]]; then
    echo "could not read Chrome major version from $BIN" >&2
    exit 1
fi
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${MAJOR}.0.0.0 Safari/537.36"

STARTED=$(date +%s)

"$BIN" \
    --headless=new \
    --no-sandbox \
    --disable-gpu \
    --user-agent="$UA" \
    --disable-blink-features=AutomationControlled \
    --remote-debugging-port="$PORT" \
    --remote-debugging-address=127.0.0.1 \
    about:blank &
CHROME=$!
echo "$(date '+%F %T') started Chrome pid $CHROME on port $PORT"

# SIGTERM, wait up to $GRACE s, then SIGKILL Chrome and its children. The final
# `wait` cannot hang: the pid is dead by then.
STOPPING=0
stop_chrome() {
    (( STOPPING )) && return 0   # a second signal during the grace period must not re-enter
    STOPPING=1
    trap '' TERM INT
    kill "$CHROME" 2>/dev/null || true
    local i
    for ((i = 0; i < GRACE * 5; i++)); do
        kill -0 "$CHROME" 2>/dev/null || break
        sleep 0.2
    done
    if kill -0 "$CHROME" 2>/dev/null; then
        echo "$(date '+%F %T') Chrome pid $CHROME ignored SIGTERM for ${GRACE}s, sending SIGKILL"
        pkill -KILL -P "$CHROME" 2>/dev/null || true
        kill -KILL "$CHROME" 2>/dev/null || true
    fi
    wait "$CHROME" 2>/dev/null || true
}
trap 'stop_chrome; exit 0' TERM INT

sig_prev=""
sig_since=$(date +%s)
while kill -0 "$CHROME" 2>/dev/null; do
    sleep "$POLL" &
    wait $! || true
    now=$(date +%s)
    list=$(curl -s --max-time 5 "http://127.0.0.1:$PORT/json/list" 2>/dev/null || true)
    if [[ -z "$list" ]]; then
        sig_since=$now          # unreadable (e.g. still starting) = in use
        continue
    fi
    sig=$(printf '%s' "$list" | grep -E '"(id|url)"' | cksum)
    if [[ "$sig" != "$sig_prev" ]]; then
        sig_prev="$sig"
        sig_since=$now
    fi
    used=$(stat -f %m "$MARKER" 2>/dev/null || echo "$STARTED")
    (( used < STARTED )) && used=$STARTED  # an older use cannot predate this browser
    if (( now - sig_since >= IDLE && now - used >= IDLE )); then
        echo "$(date '+%F %T') idle for ${IDLE}s, stopping Chrome (pid $CHROME)"
        stop_chrome
        exit 0
    fi
done
# Chrome exited on its own: propagate its status.
wait "$CHROME"
