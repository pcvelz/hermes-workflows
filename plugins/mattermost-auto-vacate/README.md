# mattermost-auto-vacate — the bot leaves channels nobody else is in

mattermost-auto-vacate keeps the bot out of Mattermost channels it has been left alone in. A daemon
thread inside the gateway sweeps the bot's channels every `interval_s` seconds. A channel where the bot
is the only non-bot member, for at least `grace_s` measured across consecutive sweeps, is left by the bot
itself (`DELETE /channels/{id}/members/{bot}`). Any human joining resets the timer. Direct and group
messages, and channels named in `exclude`, are never touched. Credentials are the gateway's own
`MATTERMOST_URL` / `MATTERMOST_TOKEN` from the process environment; the token is never logged.

On by default. Channels are never archived or deleted, posts are never touched, and other members are
never removed.

## Behaviour

- A channel is "alone" when no member other than the bot is a human. Other bots do not count as humans.
  A member id the server cannot describe counts as a human, so the plugin errs toward staying.
- The first sweep that sees a channel alone records a first-seen timestamp in
  `<HERMES_HOME>/mattermost-auto-vacate/state.json`. The bot leaves on the first sweep at which that
  channel has been alone for at least `grace_s`. A fresh state file therefore means the earliest leave is
  about `grace_s` after the plugin first sees the channel alone.
- One INFO line per leave: `auto-vacate: left channel <name> (team <team>), sole member for <n>s`.
- `dry_run: true` logs `auto-vacate (dry run): would leave channel …` and leaves nothing.
- REST errors are logged as warnings. The affected channel keeps its timer and is retried next sweep.
  If the team listing fails, state is not pruned.
- Missing `MATTERMOST_URL` or `MATTERMOST_TOKEN` logs one WARNING and stops the thread for that process.
- An unreadable profile config disables the plugin (fail-closed). A missing `mattermost_auto_vacate:`
  block means the defaults apply.

## Config

Profile `config.yaml`, block `mattermost_auto_vacate` (all keys optional):

**Enabled by default, on purpose.** The plugin does nothing until a profile lists it in `plugins.enabled`, so
that opt-in is the switch. Once it is opted in, it should just work without a second flag. The `enabled: false`
key is there to pause it without removing it from the plugin list.

```yaml
mattermost_auto_vacate:
  enabled: true          # default true
  interval_s: 300        # sweep period
  grace_s: 600           # alone this long, across sweeps, before leaving
  exclude: [town-square, off-topic]   # internal channel names never left
  dry_run: false         # true = log only
```

The gateway reads the config at start, so a change applies after a gateway restart.

## Files

| File | Role |
|------|------|
| `__init__.py` | `register()` starts the sweep thread once; `_cycle()` is one pass (config, credentials, sweep). |
| `config.py` | Defaults, the `mattermost_auto_vacate` block loader, the state-file path. |
| `vacate.py` | Mattermost REST client, the sweep (alone detection, grace, leave), state load/save, `run_once()`. |
| `plugin.yaml` | Plugin manifest. |
| `../../tests/smoke/test_mattermost_auto_vacate.py` | Mocked-HTTP tests against an in-memory fake server. |

## Install / Verify

Enable per profile: symlink this directory into the profile's `plugins/` home, then list the name under
`plugins.enabled` in that profile's config. The example in `config/profiles/<p>/config.yaml.example` shows the
defaults.

```bash
ln -sfn <repo>/plugins/mattermost-auto-vacate ~/.hermes/profiles/<p>/plugins/mattermost-auto-vacate
hermes -p <p> plugins list          # look for mattermost-auto-vacate · enabled
```

Verify the logic (no network; a Hermes venv python, since the config loader needs PyYAML):

```bash
cd <repo> && ~/.hermes/hermes-agent/venv/bin/python3 -m unittest tests/smoke/test_mattermost_auto_vacate.py
```

Live check: after a gateway restart, grep the profile's gateway log for `auto-vacate` lines. A dry run
(`dry_run: true`) shows what would be left before enabling real leaves.
