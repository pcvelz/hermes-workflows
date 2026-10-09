# Services: launchd jobs without account-bound values in the repo

`services/` holds small launchd-backed tools and one wiring script. Each one runs
on your machine and may need values that belong to you: a secret, an account id,
a server, a topic, a Keychain item name. This page explains how those values are
kept out of the repo.

## The key-file pattern

Every account-bound value lives in one file at a fixed path:

    keys/<service>/<name>

relative to the repo root. Set `HERMES_KEYS_DIR` to replace the `keys/` directory
itself. `keys/` is git-ignored as a whole, so real values never reach git.

- Configs and plists carry only the **path**, never the value.
- `services/services.yaml.example` is usable as-is. Copy it to
  `services/services.yaml` unchanged, or leave it: the installer reads the
  `.example` when the real file is absent.
- Code reads a key at runtime (`services/_shared/keyfile.py`), strips whitespace,
  and fails with a message naming the missing path. It never logs a value.

Create a key:

```bash
mkdir -p keys/kanban-ntfy-notifier
printf '%s' '<value>' > keys/kanban-ntfy-notifier/topic
```

## The services

| Service | launchd | Key files |
|---------|---------|-----------|
| `cdp-chrome-service` | yes | none (`CDP_PORT` is a plain setting) |
| `config-guard` | yes | none (`watch_profiles` is plain config, rendered into `WatchPaths`) |
| `kanban-ntfy-notifier` | yes | `keys/kanban-ntfy-notifier/{server,topic,keychain-account,keychain-service}` |
| `composio-gmail-mcp` | no, run by hand | `keys/composio-gmail-mcp/{host,server-id,user-id,api-key}` |

Each service has its own README under `services/<name>/`.

## Install

The installer renders the plists from `launchd/service.plist.tmpl.example` and
registers them:

```bash
python3 scripts/install-services.py --dry-run                 # render into a temp dir only
python3 scripts/install-services.py                           # install and verify
python3 scripts/install-services.py --only config-guard       # one service
```

It copies each payload into `$HERMES_HOME/launchd-bin/` and each key into
`$HERMES_HOME/launchd-keys/` (mode 0600). Launchd jobs run from those copies because
macOS does not let launchd read files under `~/Documents`. After a key changes,
run the installer again. `--dry-run` never writes under the real `HERMES_HOME` or
LaunchAgents and never calls `launchctl`.

Labels are `<label_prefix>.<service>`. The default prefix is `org.example.hermes`;
change it in your `services/services.yaml`.

## Tests

```bash
bash tests/static/run.sh
bash tests/smoke/services-dry-run.sh      # renders against a throwaway HOME with dummy keys
```
