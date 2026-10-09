# cdp-chrome-service

A standing headless Chrome that owns a CDP endpoint on `127.0.0.1:9222`, so a
browser-automation agent (for example the Hermes `browser` toolset with
`browser.cdp_url` pointed at that port) does not depend on a Chrome process
that dies with the session. launchd keeps it alive across reboots.

## Architecture

`start-cdp-chrome.sh` finds the newest Playwright-cached
`Google Chrome for Testing` binary at start time (so cache bumps do not break
it) and execs it with `--remote-debugging-port`. The installer copies the
script into `$HERMES_HOME/launchd-bin/` and runs it with `/bin/bash`, because
macOS TCC denies launchd reads of files under `~/Documents`.

No account-bound values are involved, so this service has no key files. The
port is a plain setting: `CDP_PORT` in `services/services.yaml`.

## Files

| File | Role |
|------|------|
| `start-cdp-chrome.sh` | Launcher. Resolves the Playwright Chrome binary and execs it headless. |
| `README.md` | This file. |

## Prerequisite

```bash
npx playwright install chromium   # populates ~/Library/Caches/ms-playwright
```

## Install

Through the shared installer (see `docs/services.md`):

```bash
python3 scripts/install-services.py --only cdp-chrome-service
```

## Verify

```bash
curl -s http://127.0.0.1:9222/json/version   # Chrome/... and webSocketDebuggerUrl
launchctl print gui/$(id -u)/<label-prefix>.cdp-chrome-service | grep -E 'state|last exit'
```

Headless Chrome is detectable by sophisticated anti-bot systems. That is a
known limit of this approach, not a bug in the service.
