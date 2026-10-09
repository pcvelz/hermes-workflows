# config-guard

A deterministic tripwire for profile config corruption. A hand edit that breaks
the YAML of a profile `config.yaml` makes the runtime fall back to built-in
defaults silently, so the plugins in that file run as inert no-ops. This
service makes the failure loud (a macOS notification) and deterministic (a
sentinel file that other tooling can check) within seconds of the save.

## Architecture

launchd runs `check-configs.py` on every save to a watched config
(`WatchPaths`) and at load. The script `yaml.safe_load()`s the root config and
every `profiles/*/config.yaml`. A parse failure writes
`$HERMES_HOME/CONFIG_BROKEN` and fires a notification. A clean run clears a
stale sentinel. It always exits 0, so the agent never thrashes.

The tripwire cannot stop a bad edit. It only makes the failure visible.

**Watched profiles are config, not code.** `watch_profiles` in
`services/services.yaml` is rendered into the plist's `WatchPaths` by the
installer. launchd cannot glob, so a new profile needs an entry there and a
re-run of the installer. The checker itself still scans every profile on each
run.

No account-bound values are involved, so this service has no key files.

## Files

| File | Role |
|------|------|
| `check-configs.py` | Stdlib checker (plus PyYAML). Writes or clears the sentinel, fires the notification, always exits 0. Honors `HERMES_HOME`. |
| `README.md` | This file. |

## Install

Through the shared installer (see `docs/services.md`). The installer copies the
checker into `$HERMES_HOME/launchd-bin/` and runs it with the Hermes venv python:

```bash
python3 scripts/install-services.py --only config-guard
```

## Verify

```bash
# Manual run against a throwaway tree, never the real config:
tmp=$(mktemp -d)
mkdir -p "$tmp/profiles/demo"
printf 'model:\n  default: x\nbad line no colon\n' > "$tmp/profiles/demo/config.yaml"
HERMES_HOME="$tmp" python3 services/config-guard/check-configs.py
cat "$tmp/CONFIG_BROKEN"      # names the failing file and the YAML exception
rm -rf "$tmp"
```

Verify the launchd path, not only the manual run:
`launchctl kickstart -k gui/$(id -u)/<label-prefix>.config-guard`, then read the
log under `$HERMES_HOME/logs/`. It must show `config-guard: OK`.
