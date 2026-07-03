# Hermes cron jobs

In-gateway scheduled work for the Hermes autonomous development workflow. The jobs in
`jobs.json.example` are **starter prompt templates** — a sensible default shape for a
software-development workflow. Adopt deliberately: enable only the jobs your workflow
actually needs.

## Where this file lives

Drop the jobs file at:

```
$HERMES_HOME/profiles/<profile>/cron/jobs.json
```

Each gateway profile has its own cron file. Example for an `orchestrator` profile:

```
~/.hermes/profiles/orchestrator/cron/jobs.json
```

```bash
mkdir -p "$HERMES_HOME/profiles/<profile>/cron"
cp cron/jobs.json.example "$HERMES_HOME/profiles/<profile>/cron/jobs.json"
```

## The tick model — why crons are in-gateway, not host crontab

The Hermes gateway runs a custom asyncio dispatcher that ticks approximately every **60 seconds**.
Each tick the dispatcher:

1. **Promotes tasks** — scans the kanban board and advances tasks from `todo` to `ready`
   when all their dependencies have been completed.

2. **Reaps stalled workers** — checks worker heartbeats and reaps any that are stale.
   Configurable thresholds (design targets):
   - Standard heartbeat grace: **15 minutes** (worker must have checked in within the last 15m).
   - Startup grace: **60 minutes** (new workers get a longer window while initializing).
   - Hard maximum runtime: **4 hours** (no worker runs longer than this regardless of heartbeat).
   Reaped tasks are returned to `ready` for re-dispatch.

3. **Dispatches tasks** — assigns at most **one task per profile** that does not already have
   an active worker. Spawn cooldowns throttle aggressive retry:
   - Normal: **10 minutes** between spawns for the same task.
   - Fast-retry: **2 minutes** (used after a transient failure).
   - Backoff: **30 minutes** after 3 or more consecutive failures on a task.

4. **Diffs board state** — compares the board snapshot to the previous tick and sends
   notifications only on changes, not on every tick.

**Why this matters for cron jobs:** cron jobs ride this same dispatcher loop. A job's
payload is a natural-language `prompt` that the gateway injects to the assigned agent on
each scheduled tick — not a raw shell command. Operations the agent cannot perform from
inside its context (git pulls, service restarts, log tailing) are delegated to the
host bridge over HTTP. See `scripts/bridge/hermes-bridge.py` for the full bridge rationale.

## Job schema

Each job in `jobs` is an object with these fields:

| Field     | Type    | Description |
|-----------|---------|-------------|
| `id`      | string  | Short unique slug. Used in logs and board references. |
| `schedule`| string  | Standard 5-field cron expression (`*/5 * * * *`, `0 3 * * *`, etc.). The gateway tick resolves these against wall-clock time each tick. |
| `profile` | string  | Which gateway profile owns this job. Replace `<PROFILE>` with your profile name. Only the **orchestrator** profile should own dispatch, watchdog, and audit crons — worker profiles should handle work tasks only. |
| `prompt`  | string  | The natural-language task text injected to the agent on each scheduled tick. |
| `enabled` | boolean | Set `false` to disable without deleting the job definition. |

After editing `jobs.json`, update `updated_at` to the current ISO 8601 timestamp and reload
the gateway (see Install below).

## The ten jobs

These are **starter prompt templates**. Enable the ones that match your workflow.

| Job id | Cadence | Profile role | What it does |
|--------|---------|--------------|--------------|
| `ram-watchdog` | every 5m | orchestrator | Monitor host RAM; restart heaviest worker if pressure is critical |
| `stale-worker-watchdog` | every 15m | orchestrator | Reap workers with stale heartbeats (15m standard / 60m startup / 4h Willow); return tasks to ready |
| `dispatcher-health` | every 15m | orchestrator | Verify the asyncio dispatcher tick is alive; alert + restart gateway if stalled |
| `vault-hindsight-sync` | every 30m | orchestrator | Sync Obsidian vault notes into Hindsight pgvector for semantic memory (proposed — requires Hindsight to be wired) |
| `board-hygiene` | every 30m | orchestrator | Close orphaned tasks, resolve dependencies, promote todo→ready |
| `post-work-audit` | every 30m | orchestrator | Review recently completed tasks for quality; file follow-ups |
| `redeploy-on-sha-drift` | every 1h | orchestrator | git-pull via bridge; if HEAD SHA changed, build + deploy |
| `inference-live-probe` | every 4h | orchestrator | Probe the configured LLM backend (`${LLM_BASE_URL}`); alert if unreachable |
| `planner-daily-note` | daily 22:00 | planner | Generate and write the daily summary note to the Obsidian vault |
| `kanban-db-backup` | daily 03:00 | orchestrator | Back up the kanban DB (disabled by default — use launchd plist instead) |

**Job #2 (`stale-worker-watchdog`)** encodes the reaping thresholds: 15m heartbeat /
60m startup grace / 4h hard maximum. Keep these in sync with your gateway version.

**Job #7 (`redeploy-on-sha-drift`)** uses the bridge's `/git-pull` endpoint and bridges the
gap between the remote HEAD and the running deploy. The bridge returns the `head_sha` field;
compare it against your last-deployed SHA to decide whether to rebuild.

## LLM backend probe — job #8

Job `inference-live-probe` sends a minimal request to your configured LLM backend to confirm
availability. Configure the endpoint in your profile's `config.yaml`:

- **Example (local default):** `http://127.0.0.1:<PORT>` (a local llama-swap proxy, native Anthropic-compatible)
- **Model:** `<your-model-alias>` (fill in your own; see [docs/backend.md](../docs/backend.md))
- **API key:** `${LLM_API_KEY}` (default `local`) — never commit real keys

If your backend is self-hosted and has cold-start latency, set client read timeouts to
**at least 120 seconds** on any client or agent that calls it. A short timeout will produce
spurious failures when the backend is loading.

**Docker topology note:** if your backend is self-hosted and loopback-bound, containers
cannot reach it via `localhost`. See `docs/architecture/topologies.md` for the backend
networking options (bridge bind, tunnel, or `host-gateway`). The same LAN-exposure
caveat applies to the bridge (port 9876).

See [docs/backend.md](../docs/backend.md) for the complete backend configuration reference.

## Backups: launchd vs in-gateway — pick one

The kanban DB backup is available via two mechanisms:

| Mechanism | File | Schedule | Default |
|-----------|------|----------|---------|
| **launchd plist** (recommended) | `launchd/com.hermes-workflows.backup.plist.example` | 03:00 daily via `StartCalendarInterval` | **Enabled** |
| **In-gateway cron** | `cron/jobs.json.example` job `kanban-db-backup` | `0 3 * * *` | `enabled: false` |

Enable **exactly one**. The launchd plist is the recommended default because it fires
reliably even if the gateway is not running, and it runs the same
`scripts/backup/kanban-backup.example.sh` script that uses the SQLite Online Backup API
(safe for a live, WAL-mode database). See that script for details on why `sqlite3 .backup`
is used instead of `cp`.

## Install

```bash
# 1. Copy the example to the profile's cron directory.
mkdir -p "$HERMES_HOME/profiles/<profile>/cron"
cp cron/jobs.json.example "$HERMES_HOME/profiles/<profile>/cron/jobs.json"

# 2. Edit jobs.json:
#    - Replace <PROFILE> in each job's "profile" field.
#    - Replace <VAULT_PATH>, <threshold>, <HERMES_HOME>, etc.
#    - Set "enabled": false for jobs you don't need yet.
#    - Update "updated_at" to the current ISO 8601 timestamp.
$EDITOR "$HERMES_HOME/profiles/<profile>/cron/jobs.json"

# 3. Reload the gateway so it picks up the new cron file.
launchctl kickstart -k gui/$(id -u)/com.hermes-workflows.gateway

# 4. Confirm the jobs are registered (check gateway logs).
tail -n 50 "$HERMES_HOME/logs/gateway.log" | grep -i cron
```

The gateway reads `jobs.json` on startup and re-evaluates schedules each dispatcher tick.
When you edit the file, restart the gateway to apply changes.

## Optional Docker topology note

In the Docker compose stack, these cron jobs run identically inside the agent container.
The only operational differences are:

- **Bridge URL:** use `http://host.docker.internal:9876` instead of `http://127.0.0.1:9876`
  (bridge must also be relaunched with `HERMES_BRIDGE_BIND=0.0.0.0`).
- **LLM backend URL:** if self-hosted and loopback-bound, use `http://host.docker.internal:<port>`
  instead of `http://localhost:<port>` (and expose the backend on the Docker bridge first).

Both require the host-side security mitigations described above (firewall rules and/or
`HERMES_BRIDGE_TOKEN`). See `docs/architecture/topologies.md` for the full Docker topology.