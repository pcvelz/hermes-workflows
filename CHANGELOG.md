# Changelog

All notable changes to hermes-workflows. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0]

First stable release. Covers everything since `v0.3.0`.

### Added

- **claude-code-bridge plugin:** a chat front-end that answers allowed channels through one
  persistent Claude Code session per channel (tmux). Timestamps on replies, `!reset` for a
  fresh conversation per channel, thread rules, investigation passthrough to Hermes with
  `delegate_task` fan-out, and per-channel MCP via `keyfile:` references. Config changes
  restart the bridge.
- **`chat` role:** a configurable chat profile. The bridge is optional.
- **`config/backends.yaml`** (plus `.example`) and `scripts/check-backends.py`: one source of
  truth for which backend each profile uses, checked against the profile configs.
- **Key-file pattern:** `keys/<service>/<name>`, one file per account-bound value. The
  `.example` files are the config. `docs/secrets.md` documents the convention, including the
  user-run setup for unattended services.
- **Services:** `services/` holds cdp-chrome, config-guard, kanban-ntfy-notifier and
  composio-gmail-mcp, installed by `scripts/install-services.py`.
- **Docs:** browser-capability, roles, task-authoring, services, claude-code-bridge and
  virgin-voyage-results.
- **Planner:** the planner role can create cards.

### Changed

- The `coder` profile is renamed to `coding`.
- Plugin manifests are versioned `1.0.0`.

### Fixed

- The static `check-backends` case that loaded the checker script as the backends file now
  checks the real `backends.yaml` against the profiles.
- The `kanban-harness` tests use a neutral assignee name in fixtures and docs.

### Security

- `keys/` is ignored by a generic rule that also matches the symlink. Real secrets never
  enter git.
