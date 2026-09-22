# Profiles

Hermes runs as multiple independent **profiles**. Each profile is its own gateway
process with its own `config.yaml`, `state.db`, `MEMORY.md`, and cron set.
A profile is a role — it encodes what an agent is allowed to do and what it must
never do.

This scaffold ships four generalized roles designed to cover an autonomous
software-development workflow. Adopt them as-is or collapse / expand the set
to match your team size and tooling.

---

## The four roles

| Role | Purpose | Model tier | Key toolsets | Approvals |
|---|---|---|---|---|
| **orchestrator** | Dispatch tasks, run crons, notify humans. Never implements. | your configured backend | kanban, cronjob, messaging | auto (dispatch/notify only) |
| **coder** | Main implementation worker. Writes code, runs tests. | your configured backend | code_execution, file, terminal, lsp | manual |
| **planner** | Research (SearXNG), task decomposition, daily planning note. | your configured backend | web/search, kanban (decompose), file_read | auto |
| **qa-tester** | Playwright E2E verification. Reports pass/fail; never fixes code. | your configured backend | browser, terminal (launch only) | auto |

### Reading without writing: `file_read`

Upstream's `file` toolset grants reading and writing as one thing. A role that must
not edit is granted **`file_read`** instead: one tool of the same name, from the
[`file-read`](../plugins/file-read/) plugin, with three ops — `open`, `list`,
`search` — and no code path that writes, creates, moves or deletes. Any other op is
refused, not guessed at. It reads the formats agents plan in (markdown, text,
json/yaml/toml/csv/tsv, logs) and a named list of source formats; it refuses
scripts and executables by extension, executable bit and `#!` line, and refuses
wherever those signals disagree. Enable the plugin and list `file_read` (never
`file`) in the profile's `platform_toolsets`. The planner is its first consumer.

All profiles share the same backend endpoint by default. You can override `base_url` and
`model` per-profile if you want to route different roles to different endpoints or tiers.
See [docs/backend.md](backend.md) for backend configuration.

---

## Files per profile

Each profile lives at `${HERMES_HOME}/profiles/<name>/`.

| File | Purpose | Template |
|---|---|---|
| `config.yaml` | Profile-specific config overlay (merged on top of `${HERMES_HOME}/config.yaml`) | `config/profiles/<role>/config.yaml.example` |
| `state.db` | SQLite task/session state — auto-created by hermes | (auto-generated) |
| `MEMORY.md` | Behavioral rules injected every turn, hard-capped at ~2200 chars | `config/profiles/<role>/MEMORY.md.example` |
| `memories/` | Vault layer for overflow content (managed by curator) | (auto-created) |
| `cron` | Per-profile cron definitions | See `cron/README.md` |

Copy and fill the `.example` templates into the corresponding profile directory.
See `config/config.yaml.example` for the base config that all overlays extend.

---

## Deployment note

**Native launchd + venv** is the topology this scaffold is modeled on (macOS). Each
profile is launched by a separate launchd job:

```xml
<key>ProgramArguments</key>
<array>
  <string>${HERMES_HOME}/venv/bin/python</string>
  <string>-m</string>
  <string>hermes_cli.main</string>
  <string>--profile</string>
  <string>coder</string>
  <string>gateway</string>
  <string>run</string>
  <string>--replace</string>
</array>
```

Two launchd gateways load profiles via `venv/bin/python -m hermes_cli.main --profile
<PROFILE> gateway run --replace` with `RunAtLoad` + `KeepAlive` and `HERMES_HOME` set
per job.

The Docker stack is an **optional alternative topology**. See
[docs/architecture/topologies.md](architecture/topologies.md) for both topologies
in detail.

---

## MEMORY.md — behavioral rules, not logs

> **Hard cap:** `memory.memory_char_limit: 2200` chars — injected into **every
> turn**. The curator prunes the file weekly (`curator.interval_hours: 168`).

Keep `MEMORY.md` strictly behavioral:
- Rules about what the profile must/must not do.
- Style or workflow constraints specific to this role.
- Escalation thresholds.

Do **not** put logs, project notes, ticket numbers, or history here. Overflow
belongs in the vault layer. See [docs/memory.md](memory.md) for the full memory
architecture.

---

## Timeout reminder

> If your backend is self-hosted and has cold-start latency, set all
> `clarify_timeout` and `gateway_timeout` values to **>=120 s** — requests
> spuriously time out if the backend is still loading.
>
> Hosted cloud APIs do not have cold-start
> latency; the default 120 s is a safe conservative.

This is pre-filled in the config templates. Do not lower these timeouts without
a specific reason.
