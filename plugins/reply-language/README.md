# reply-language

A Hermes **plugin** that tells the model, every turn, which language to answer in — chosen by the
Mattermost team the channel belongs to.

## Architecture (one paragraph)

A chat model answers in whatever language the user's message or the quoted sources happen to use,
which is wrong for a team that has a fixed reply language. This plugin registers a **`pre_llm_call`**
hook. For a Mattermost turn it resolves the channel's team once (REST `GET /channels/<id>` then
`GET /teams/<team_id>`, cached per channel in-process) and looks the team name up in the profile
config map `reply_language.teams` (`{<team>: <Language>}`). A mapped language returns
`{"context": <instruction>}`, which Hermes injects into the current turn's user message (ephemeral,
never persisted, never in the cached system-prompt prefix). Unmapped teams, non-Mattermost platforms
and any lookup error return `None` (fail-open). The public code carries no team names; the map is
private config.

## Files

| File | Role |
|---|---|
| `plugin.yaml` | Manifest: name, version, `pre_llm_call` hook |
| `__init__.py` | Hook implementation: team lookup, cache, language text |
| `README.md` | This file |
| `tests/smoke/test_reply_language.py` | Unit tests (mocked lookups), in the hermes-workflows repo root |

## Install

Symlink the plugin dir into each profile that needs it, then opt in and add the map to that profile's
config:

```yaml
plugins:
  enabled:
    - reply-language
reply_language:
  teams:
    <team-name>: English   # or Spanish; only English and Spanish are defined in _TEXT
```

```sh
ln -s "<path-to-repo>/plugins/reply-language" ~/.hermes/profiles/<profile>/plugins/reply-language
```

The Mattermost connection comes from the gateway process environment (`MATTERMOST_URL`,
`MATTERMOST_TOKEN`); nothing else is needed.

## Verify

```sh
cd "<path-to-repo>"
PYTHONDONTWRITEBYTECODE=1 ~/.hermes/hermes-agent/venv/bin/python3 -m unittest tests/smoke/test_reply_language.py
hermes -p <profile> plugins list   # reply-language should show enabled
```
