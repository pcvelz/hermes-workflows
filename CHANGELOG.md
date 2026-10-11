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
- **Claude Code bridge, chat via hooks** (opt-in, `chat_hooks`): a bridged session runs like a plain
  Claude Code session in its repo. Chat enters only through a send command with refuse patterns, a
  Stop hook that blocks a turn that sent nothing, an AskUserQuestion denial, and a context line on
  each message. All texts live in one JSON file (`config/chat-hooks.json.example`).
- **Claude Code bridge, delivery:** replies go over REST when no gateway reference exists yet, with
  hold and backoff, in-flight resume, and delivery of self-initiated turns.
- **Claude Code bridge, reply guard** (opt-in): blocks a final reply that matches configured give-up
  or promise patterns.
- **Claude Code bridge, blocked prompts** (opt-in, `blocked_prompt.mode`): detects a dialog an
  unattended session cannot answer and handles it.
- **Claude Code bridge, browser guard** and a browsing playbook; the CDP Chrome service starts in a
  less detectable headless mode.
- **Claude Code bridge, operator commands:** `!debrief <question>` (the answer is filed in the
  channel spool) and `!reset clear` (also deletes the conversation since the previous reset).
- **mattermost-auto-vacate plugin:** the bot leaves channels where it is the only member, once the
  plugin is listed in `plugins.enabled`.

### Changed

- The `coder` profile is renamed to `coding`.
- Plugin manifests are versioned `1.0.0`.
- Investigation triggers, public examples and test texts are English only. Add trigger words for
  other languages with `claude_code_bridge.passthrough_patterns`.
- Private and machine-specific names are replaced by generic ones (for example,
  `examples/smart-home-healthcheck`).

### Fixed

- The static `check-backends` case that loaded the checker script as the backends file now
  checks the real `backends.yaml` against the profiles.
- The `kanban-harness` tests use a neutral assignee name in fixtures and docs.

### Security

- `keys/` is ignored by a generic rule that also matches the symlink. Real secrets never
  enter git.
- **Team boundary:** anything the bridge posts about a channel (debrief answers, blocked-prompt and
  failure notices) goes only to a channel in the same team. Cross-team or unknown means nothing is
  posted.
- **Write guards** (opt-in): a repo-scope guard keeps edits inside the session's repo plus configured
  companions, and a public-first guard blocks a private copy of a public file. Both fail open and are
  not a security boundary.
