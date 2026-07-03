# Hermes Workflows — Optional Docker Deployment

> **STATUS: OPTIONAL / UNVALIDATED SCAFFOLD**
>
> This Docker topology is optional and has not been validated end-to-end.
> The primary install path is the **native launchd + Python venv install**
> — see the [repository root README](../README.md) and
> [docs/architecture/topologies.md](../docs/architecture/topologies.md).
>
> Treat this compose file as a convenience scaffold, not an authoritative
> deployment guide. In particular:
> - `ghcr.io/nousresearch/hermes-agent:latest` is **not a known published image** —
>   it will almost certainly fail to pull. Hermes is distributed as a Python package
>   installed from source, not as a Docker image. To use the Docker path, clone the
>   upstream repo and uncomment the `build:` stanza in `docker-compose.yml`:
>   ```bash
>   git clone https://github.com/NousResearch/hermes-agent.git ../.hermes-agent
>   ```
>   **Do NOT** `pip install hermes-cli` or `pip install hermes-agent` from PyPI —
>   those are unrelated third-party packages.
> - The `hindsight` container is a **bare pgvector/pgvector Postgres substitute**
>   — not the full Hindsight implementation. No ingest API is wired; semantic
>   recall from this store is proposed design, not yet implemented.
> - Secrets are **host-side** (Secrets-Kit or 1Password), not a container.
>   See [docs/secrets.md](../docs/secrets.md).

---

## What this stack runs

| Service | Container | Host port (127.0.0.1 only) | Purpose | Notes |
|---|---|---|---|---|
| agent | `hermes-agent` | 9876 | Hermes gateway / orchestrator | image unverified — prefer building from source |
| searxng | `hermes-searxng` | 8888 | Private web-search engine | [services/searxng.md](services/searxng.md) |
| hindsight | `hermes-hindsight` | 8889 | pgvector substrate (bare) | [services/hindsight.md](services/hindsight.md) — no ingest API wired |
| hermes-pi | `hermes-pi` | *(none — worker)* | pi-coding-agent, optional | [services/hermes-pi.md](services/hermes-pi.md) |
| hermes-claude | `hermes-claude` | *(none — worker)* | Claude Code (`claude` CLI), optional | [services/hermes-claude.md](services/hermes-claude.md) |

All published ports are bound to `127.0.0.1` only. Nothing is exposed on your
LAN by default.

---

## Prerequisites

- **Docker Engine + Compose v2** (`docker compose` command, not the legacy `docker-compose`).
- **A configured LLM backend** — see the Backend reachability section below.
- **`HERMES_HOME` populated** with a working Hermes config: `config.yaml`, `kanban.db`, `profiles/`, and any skill files. For a fresh start, point `HERMES_HOME` at an empty directory and let the agent self-initialize on first run.
- **API key injected at launch time** via Secrets-Kit or 1Password — see [docs/secrets.md](../docs/secrets.md). Do not put real keys in `.env`.

---

## Backend reachability

The agent needs a reachable LLM endpoint. Set `LLM_BASE_URL` and `HERMES_MODEL`
in `.env` to point at your backend.

**Hosted API (any Anthropic-compatible cloud endpoint):**

No special networking is required — a cloud endpoint is reachable from inside
containers via the internet. Set `LLM_BASE_URL` to the provider's base URL and
inject your key at launch time (see Secrets below). The recommended default,
though, is the local llama-swap proxy described next.

**Self-hosted backend (local model server):**

If your inference backend binds to loopback only (`127.0.0.1`), containers
cannot reach it — they are not on the host loopback interface.

*Option 1 — expose to the Docker bridge (recommended):* Configure your backend
to listen on `0.0.0.0` (or the docker bridge address). The compose file includes
an `extra_hosts: host-gateway` entry so `host.docker.internal` resolves from
inside containers.

> **SECURITY CALLOUT — LAN exposure**
>
> Binding a model server to `0.0.0.0` exposes the inference endpoint to every
> device on your local network, potentially without authentication. Mitigate
> with a host-level firewall rule restricting inbound traffic to loopback and
> the Docker bridge only, or keep the host on a trusted isolated network.

*Option 2 — tunnel / proxy:* Set up a port-forwarding proxy between the Docker
bridge gateway and your backend port (e.g. `socat`, `nginx`, or an SSH tunnel).

Set `LLM_BASE_URL=http://host.docker.internal:<port>` (and omit `/v1` if your
backend expects the Anthropic Messages path — the SDK appends `/v1/messages`
automatically).

**Verify reachability after configuring:**

```bash
# Spin up just the agent and verify the backend is visible from inside:
docker compose up -d agent
docker compose exec agent curl -fsS ${LLM_BASE_URL}/v1/models
# Or the native Anthropic path (adjust URL to your backend):
docker compose exec agent curl -fsS ${LLM_BASE_URL} \
  -H 'content-type: application/json' \
  -d '{"model":"<your-model-alias>","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}'
```

---

## Bring it up

```bash
# 1. Copy and edit the environment file
cp .env.example .env
#    At minimum set:
#      HERMES_UID / HERMES_GID (match host owner of HERMES_HOME)
#      LLM_BASE_URL / HERMES_MODEL (your backend endpoint + model id)
#      HINDSIGHT_DB_PASSWORD (change from "changeme")
#      SEARXNG_SECRET (openssl rand -hex 32)
#    Inject the API key via Secrets-Kit or 1Password (see docs/secrets.md).

# 2. Start the full stack (inject secrets at launch time)
#    macOS with Secrets-Kit:
seckit run --service hermes --names LLM_API_KEY -- \
  env HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose --env-file .env up -d

#    1Password:
op run --env-file=<(echo LLM_API_KEY=op://vault/item/field) -- \
  env HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose --env-file .env up -d

#    Plain env var (less secure — only for quick local testing):
HERMES_UID=$(id -u) HERMES_GID=$(id -g) LLM_API_KEY=<key> docker compose --env-file .env up -d

# 3. Verify all services came up healthy
docker compose ps

# 4. Tail agent logs
docker compose logs -f agent
```

The `HERMES_UID=$(id -u) HERMES_GID=$(id -g)` prefix ensures bind-mounted
files under `HERMES_HOME` are created and owned by your host user, not root.

---

## Selective services

All companion services are optional. To run only the agent and web search:

```bash
docker compose up -d agent searxng
```

If you omit services that are listed in the agent's `depends_on`, remove or
comment out those entries in `docker-compose.yml`. The agent tolerates missing
companions at runtime.

---

## Secrets

API keys and other secrets are managed **host-side**, not in a container.
See [docs/secrets.md](../docs/secrets.md) for the recommended approach
(Secrets-Kit on macOS, or 1Password `op run` as an alternative).

This stack deliberately does not run a Bitwarden/Vaultwarden secrets container;
secrets are injected host-side instead.

---

## Persistence and volumes

**Bind mount** — `${HERMES_HOME}:/opt/data`
: The source of truth for `kanban.db`, `state.db`, `sessions/`, `skills/`,
and `profiles/`. Back this directory up as you would any project directory.

**Named volumes** (per-companion service state):

| Volume | Service | Contents | Backup method |
|---|---|---|---|
| `searxng-config` | searxng | `settings.yml`, engine config | copy or `docker cp` |
| `hindsight-data` | hindsight | Postgres + pgvector data | `pg_dump` (preferred) |
| `pi-npm` | hermes-pi | Cached `node_modules` | disposable — safe to delete |
| `claude-npm` | hermes-claude | Cached `node_modules` | disposable — safe to delete |

**Generic volume backup one-liner:**
```bash
docker run --rm \
  -v <volume-name>:/v \
  -v $PWD:/backup \
  alpine tar czf /backup/<volume-name>.tgz -C /v .
```

**Hindsight (Postgres) — use pg_dump for portability:**
```bash
docker compose exec hindsight pg_dump -U hindsight hindsight > hindsight-backup.sql
```

---

## How this differs from the native path

- The native path uses **launchd + Python venv** (no containers). Profile
  names are user-defined; this compose file uses the conceptual roles
  (`orchestrator`, `coder`, `planner`, `qa-tester`) via `HERMES_PROFILE`.
- The native path applies patches via `reapply-patches.sh.example` over a live
  install; Docker here uses a pinned image — patches are baked in at image
  build time (if using the build: stanza).
- The upstream Hermes compose uses `network_mode: host`. This scaffold uses a
  **bridge network** (`hermes-net`) so companion services get isolated DNS and
  resolve each other by service name. If the official image requires host
  networking, switch back by replacing the `networks` block with
  `network_mode: host` in the agent service and removing `extra_hosts`.

See [../README.md](../README.md) for the native launchd + venv path.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| LLM request timeout | Backend slow or unreachable | Check `LLM_BASE_URL` is reachable from inside the container; increase `HERMES_HTTP_TIMEOUT` for backends with cold-start latency |
| `host.docker.internal` fails on Linux | Missing `extra_hosts` entry | Verify Docker >= 20.10 and the `extra_hosts: host-gateway` line is in the service |
| Connection refused to self-hosted backend | Backend bound to loopback only | Expose the backend to the Docker bridge; see "Backend reachability" above |
| pi contacts external API | `PI_OFFLINE` not set or key not empty | Ensure `PI_OFFLINE=1` and the relevant API key env var is empty in `.env` |
| Claude Code contacts external API | `CLAUDE_OFFLINE` not set or a cloud key present | Ensure `CLAUDE_OFFLINE=1`, point `ANTHROPIC_BASE_URL` at your backend, and leave any cloud `ANTHROPIC_API_KEY` empty |
| Permission errors on `/opt/data` | UID/GID mismatch | Set `HERMES_UID=$(id -u)` and `HERMES_GID=$(id -g)` matching the host owner |
| Agent unhealthy at startup | Init takes longer than `start_period` | Check agent logs; increase `start_period` in the healthcheck if needed |
| `pull access denied` for hermes-agent image | Image is not published on ghcr.io | Clone upstream: `git clone https://github.com/NousResearch/hermes-agent.git ../.hermes-agent` then uncomment the `build:` stanza |

---

## License / Contributions

Docker assets are community-maintained. PRs are welcome. When contributing,
keep all template files placeholder-only — no real secrets, tokens, API keys,
or personal paths should be committed to any file in `docker/`.
