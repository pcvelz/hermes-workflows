# Deployment Topologies

hermes-workflows supports two ways to run the stack. **Topology A (Native launchd + venv)** is the design target for macOS hosts and the setup this scaffold is modeled on. **Topology B (Docker compose)** is optional — useful for Linux servers, clean dependency isolation, or deploying the full multi-service stack including SearXNG, Hindsight, and a secrets store. Pick the topology that fits your host and skill set; both can run the same hermes-agent and point at the same configured LLM backend.

---

## Topology A — Native (launchd + venv)

The agent runs as a **native process directly on the host** — no Docker, no containers. The upstream NousResearch hermes-agent is installed into a Python virtualenv at a pinned upstream tag. The runtime home directory is `${HERMES_HOME}` (default: `~/.hermes/`), which contains:

```
${HERMES_HOME}/
├── config.yaml          # root config
├── kanban.db            # task board SQLite database
├── state.db             # agent runtime state
├── sessions/            # session transcripts
├── skills/              # whitelisted skill prompts injected per-turn
└── profiles/            # per-role profile directories
    ├── orchestrator/
    ├── coding/
    ├── planner/
    └── qa-tester/
```

### Per-profile isolation

Each role profile lives in its own subdirectory under `${HERMES_HOME}/profiles/<role>/` with its own complete state:

```
${HERMES_HOME}/profiles/<role>/
├── config.yaml      # (or profile.yaml) — role-specific agent config
├── state.db         # per-profile runtime state and conversation history
├── memories/        # MEMORY.md and associated per-turn injected rules
├── cron/            # scheduled jobs owned by this profile
└── plans/           # task plans produced by this profile
```

This scaffold ships four generalized role names: `orchestrator`, `coding`, `planner`, and `qa-tester`. Rename them to fit your workflow.

### launchd gateways

Two launchd jobs run the agent gateways on macOS and keep them alive across reboots and crashes:

- `com.hermes-workflows.gateway.plist` — primary profiles
- `com.hermes-workflows.gateway-isolated.plist` — isolated profile (optional; for a separate gateway with its own boundary)

Both are installed under `~/Library/LaunchAgents/`. Each plist uses `RunAtLoad: true` + `KeepAlive: true` so the gateway starts at login and automatically restarts on exit.

Illustrative plist skeleton (portable — substitute real paths for `<HERMES_HOME>`, `<VENV>`, `<PROFILE>`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes-workflows.gateway</string>
    <key>ProgramArguments</key>
    <array>
        <string><VENV>/bin/python</string>
        <string>-m</string>
        <string>hermes_cli.main</string>
        <string>--profile</string>
        <string><PROFILE></string>
        <string>gateway</string>
        <string>run</string>
        <string>--replace</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HERMES_HOME</key>
        <string><HERMES_HOME></string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string><HERMES_HOME>/logs/gateway.log</string>
    <key>StandardErrorPath</key>
    <string><HERMES_HOME>/logs/gateway.err</string>
</dict>
</plist>
```

Full plist templates live under `launchd/` in this repository (e.g. `launchd/com.hermes-workflows.gateway.plist.example`).

### Inference

The agent is configured to call a **bring-your-own LLM backend** — any OpenAI- or Anthropic-compatible endpoint. Set `base_url` and `model` in your profile config. The shipped example stub targets a local llama-swap proxy (recommended default) at `http://127.0.0.1:<PORT>` with your `${LLM_API_KEY}`; swap in your preferred backend. See [docs/backend.md](../backend.md).

> **Timeout note:** If your backend is self-hosted and has cold-start latency (e.g. a local model that loads into VRAM on first request), set your agent and HTTP client read timeouts generously — at least 120 seconds. The default 30 s timeout will produce spurious failures during cold starts. Hosted APIs do not have this issue.

**Pros of Topology A:**
- Low latency to your backend if it runs on the same host (direct loopback, no container networking hop)
- No Docker required; minimal dependencies
- Native launchd lifecycle — automatic startup, crash recovery, logging
- Simple security posture: local backend stays loopback-bound by default

**Cons of Topology A:**
- macOS / launchd-specific; Linux hosts need an alternative init system
- Host-coupled — agent shares the host environment
- Manual virtualenv and dependency management
- Tool services (SearXNG, Hindsight, secrets store) are NOT bundled — you add them separately if needed

---

## Topology B — Docker (compose) [OPTIONAL]

The Docker topology brings up the full suite of services as containers:

| Container | Purpose |
|---|---|
| `hermes-agent` | The NousResearch hermes-agent, containerized |
| `searxng` | Web search tool (`:8888`) |
| `hindsight` | pgvector semantic memory (`:8889`) — proposed; not yet wired |
| `secrets-store` | Secrets backend (`:8310`) |
| `hermes-pi` | Cloud-inference wrapper — node:20-slim running `@mariozechner/pi-coding-agent` (optional) |

The **host bridge process** (`:9876`) may still run natively on the host even in the Docker topology — it needs host privileges for git/build/deploy/restart operations. Containers reach it via `host.docker.internal:9876`.

### LLM backend and Docker networking

The LLM backend is external to this compose stack — you point the agent's `base_url` at whatever endpoint you use. If your backend is self-hosted and **loopback-bound** (binding only to `127.0.0.1`), Docker containers in their own network namespace cannot reach it via `localhost`.

To connect containers to a loopback-bound backend you have two options:

**Option 1 — Expose on the Docker bridge (simpler, see security caveat):**
Reconfigure your backend to accept connections on `0.0.0.0` or on the Docker bridge interface (e.g. `172.17.0.1`), then point containers at `http://host.docker.internal:<port>`.

**Option 2 — Tunnel the port (more secure):**
Use `socat`, an SSH tunnel, or Docker's `host-gateway` feature to make the port reachable inside the compose network without exposing it on all interfaces.

> **SECURITY CAVEAT — LAN exposure:** Binding a self-hosted backend to `0.0.0.0` exposes the inference endpoint on all network interfaces, including your LAN. Mitigate this by:
> - Restricting with a host firewall (`pf` on macOS, `ufw`/`iptables` on Linux)
> - Binding to the Docker bridge interface only
> - Using a tunnel instead of a raw `0.0.0.0` bind
>
> **Do not leave a self-hosted inference endpoint open on untrusted networks.**

If you use a hosted cloud API, no special networking configuration is needed — containers reach it over standard HTTPS.

**Pros of Topology B:**
- Portable across macOS and Linux (anywhere Docker runs)
- Full tool suite bundled: SearXNG, Hindsight, secrets store
- Easy teardown (`docker compose down`)
- Isolated dependencies — each service in its own container

**Cons of Topology B:**
- Extra network hop to reach a self-hosted local backend — may require bridging, with associated security tradeoff
- Container resource overhead (memory, startup time)
- hermes-pi and the four tool services are inherently Docker-stack pieces; the native topology treats them as optional add-ons

---

## Comparison table

| Aspect | A: Native (launchd+venv) | B: Docker (compose) \[optional\] |
|---|---|---|
| Install method | Python venv + launchd plists | `docker compose up` |
| Process lifecycle | launchd `KeepAlive: true` — restarts on crash/login | compose `restart: unless-stopped` (or `always`) |
| LLM backend | Point `base_url` at any endpoint; loopback works directly | Same; self-hosted loopback backends need bridging |
| Timeout guidance | Set read timeouts >=120 s if backend has cold-start latency | Same requirement |
| Tool services | Optional; installed separately if desired | Bundled: SearXNG, Hindsight, secrets store |
| hermes-pi | Not applicable | Included as optional container |
| Isolation | Host-coupled (shared env) | Containerized (isolated deps per service) |
| Best host | macOS development machine | Linux server or any Docker host |
| Security posture | Self-hosted backend loopback-only by default (safe) | Loopback-bound backends need bridging — LAN-exposure tradeoff |
| Teardown | `launchctl unload` + deactivate venv | `docker compose down` |

---

## When to use which

**Choose Native (Topology A) if…**
- You are on a macOS development machine
- You want the lowest possible inference latency to a self-hosted backend (direct loopback)
- You are comfortable with launchd and virtualenvs
- You are a single operator and do not need the full tool service suite running persistently

**Choose Docker (Topology B) if…**
- You are deploying on a Linux server
- You want full isolation and reproducibility across machines
- You want to deploy the complete multi-service stack (SearXNG, Hindsight, secrets store, hermes-pi) with a single `docker compose up`
- You are running multiple hosts and want consistent dependency management
- You use a hosted API backend (no special networking needed)
