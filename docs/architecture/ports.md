# Port Map

All ports used by the hermes-workflows stack, their bind scope, and how to reach them from Docker containers.

> The `hermes` binary referenced below (which serves the dashboard on `:9119`) comes from
> the upstream **NousResearch `hermes-agent`** install — see
> [getting-started.md](../getting-started.md). The localhost control endpoint, by
> contrast, is served by the separate **Hermes Desktop** app, which is **not part of this
> repository** (see the caveat in the table).

---

## Service port table

| Port | Service | Topology | Default bind | Docker reachability | Notes |
|---|---|---|---|---|---|
| — | **LLM backend (self-hosted, if applicable)** | Native + Docker | Your chosen bind (commonly loopback if self-hosted) | Not reachable from containers if loopback-only. Expose on the Docker bridge or use host.docker.internal with `host-gateway`. See security caveat below. | Set as `base_url` in your profile config. The example stub targets a local llama-swap proxy (recommended default) at `http://127.0.0.1:<PORT>` — no special port or binding needed for hosted cloud APIs. |
| 8888 | SearXNG (web search) | Docker | Container network | Reachable via compose service name (`http://searxng:8888`) or `host.docker.internal:8888` from the host | **Docker-optional** in this scaffold. Provides the agent with live web search capability. |
| 8889 | Hindsight (pgvector semantic memory) | Docker | Container network | Reachable via compose service name (`http://hindsight:8889`) | **Docker-optional.** Long-term semantic memory store; vault sync is currently a dry-run stub — see [data-flow.md](data-flow.md). |
| 8310 | Secrets store | Docker | Container network | Reachable via compose service name (`http://secrets-store:8310`) | **Docker-optional** secrets backend. See [docs/backend.md](../backend.md) for the secrets approach used in this scaffold. |
| 9876 | hermes-bridge (host bridge) | Native (host process) | `127.0.0.1` (loopback — strongly recommended) | Host process — reachable from containers via `host.docker.internal:9876` if you explicitly expose it. Only expose on trusted networks. | Performs privileged operations: build, deploy, `git pull`, service restart, log tail. **Never expose beyond loopback without careful firewall rules.** Runs under launchd in the native topology. |
| 8300 | Gitea (self-hosted git) | Docker | Container network / host-exposed | Reachable via compose service name or `host.docker.internal:8300` | **Docker-only**; optional. Self-hosted git server used as source-of-truth; bridge queries it on the redeploy-on-SHA-drift cron. |
| 9119 | Hermes web dashboard | Native (host process) | `127.0.0.1` (loopback) | Not reachable from containers unless explicitly exposed | Served by the `hermes` CLI (`hermes dashboard`) from the **upstream `hermes-agent` install**. Loopback-only read view of sessions/board; no inbound exposure needed. See [operating.md — Watch it think](../operating.md#2-watch-it-think--and-catch-errors-live). |
| *(dynamic)* | Hermes Desktop control endpoint | Native (host process) | `127.0.0.1` (loopback) | Not reachable from containers unless explicitly exposed | ⚠️ **Requires the separate Hermes Desktop app — NOT in this repo.** Bearer-token HTTP control API for synchronous observe/steer. Port + token are written to `~/.hermes-desktop/control.json` (port is assigned dynamically by Desktop, not fixed). Reference client: [`scripts/observe/control-client.sh`](../../scripts/observe/control-client.sh). See [operating.md — the optional cockpit](../operating.md#7-the-optional-cockpit--hermes-desktop-not-in-this-repo). |
| — | Chat gateway (Telegram / Mattermost / webhook) | Native + Docker | Outbound connections only — no inbound port | Not applicable | The gateway connects **outbound** to the chat service. No inbound listening port is opened on this host. |

---

## Port quick-reference

For copy-paste use when writing config files or firewall rules:

```
[your backend port]  LLM backend (self-hosted, if applicable)  your chosen bind
8888                 SearXNG              container network              Docker-optional
8889                 Hindsight            container network              Docker-optional
8310                 Secrets store        container network              Docker-optional
9876                 hermes-bridge        loopback only (127.0.0.1)     native host process
8300                 Gitea                container network              Docker-only, optional
9119                 Hermes dashboard     loopback only (127.0.0.1)     native (hermes-agent CLI)
(dynamic)            Desktop control API  loopback only (127.0.0.1)     external — Hermes Desktop, NOT in this repo
```

---

## Bind-scope cheat sheet

Understanding bind scope is essential for both security and getting Docker networking right.

- **`127.0.0.1` (loopback)** — Host-only access. The safest default. No process outside the current machine can reach this port, including Docker containers (which run in a separate network namespace). Use this for any self-hosted backend, the bridge, and any service that does not need external access.

- **`0.0.0.0` (all interfaces)** — Accessible from all network interfaces, including your LAN, Wi-Fi, and any attached Docker bridge network. Only use this when Docker containers need to reach a self-hosted local endpoint — and only with a host firewall rule in place.

- **Container network (compose internal)** — Reachable between Docker compose services by their service name via Docker's internal DNS (e.g., `http://hindsight:8889` from inside another container). NOT directly accessible from the host or external machines without a `ports:` mapping in the compose file.

- **`host.docker.internal`** — A special DNS name that Docker resolves to the host machine's IP from inside a container. Use this when a container needs to reach a host-side service. On Linux you may need `--add-host host.docker.internal:host-gateway` in your compose file.

---

## Security note

> **The bridge (`:9876`) must stay loopback-only. A self-hosted backend port is the only service you might need to expose beyond loopback — and only when running Docker containers that need inference access. Hosted cloud APIs require no special port exposure.**

### Self-hosted LLM backend (if applicable)

If your backend is self-hosted and loopback-bound, prefer one of these over a raw `0.0.0.0` bind:

1. **Tunnel (most secure):** Use `socat` or an SSH local-forward to make the port reachable inside Docker without binding to all interfaces:
   ```bash
   socat TCP-LISTEN:<port>,bind=172.17.0.1,fork TCP:127.0.0.1:<port>
   ```
   This binds only to the Docker bridge subnet (`172.17.0.1`), not the LAN.

2. **Docker bridge bind:** Reconfigure your backend to listen on `172.17.0.1:<port>` (Docker's default bridge IP) rather than `0.0.0.0` — containers reach it but your LAN cannot.

3. **`host-gateway` mapping:** Add `--add-host host.docker.internal:host-gateway` in compose, then keep the backend bound to `127.0.0.1` and have containers call `http://host.docker.internal:<port>`. Works on Linux; macOS Docker Desktop provides this automatically.

4. **If you must use `0.0.0.0`:** Immediately add a firewall rule allowing only the Docker bridge subnet. On macOS with `pf`, on Linux with `ufw` or `iptables`. **Never leave an inference endpoint open on an untrusted or public network.**

### hermes-bridge (:9876)

**Never expose the bridge beyond loopback.** The bridge executes privileged `git pull`, build, deploy, and service restart commands on the host. Unrestricted network access to `:9876` is equivalent to remote code execution. Keep it at `127.0.0.1:9876` always. If Docker containers need bridge access, use `host.docker.internal:9876` with a careful firewall allowlist.

### Docker-stack services (:8888, :8889, :8310)

SearXNG, Hindsight, and the secrets store run on the internal container network and do not need host-port exposure for normal operation. Only add `ports:` mappings in your compose file if you need direct host or external access for debugging — and remove them again afterward.

### Hermes dashboard (:9119) and Desktop control endpoint (dynamic)

Both are **loopback-only by default** and should stay that way. The dashboard (`:9119`) is a read-only web view served by the `hermes` CLI from the upstream `hermes-agent` install. The Desktop control endpoint is a **bearer-token-protected** HTTP API served by the separate **Hermes Desktop** app (not in this repo); its port is assigned dynamically and recorded, with the token, in `~/.hermes-desktop/control.json` (mode 0600). Do not expose either beyond `127.0.0.1`: the control endpoint can *steer* a live agent, so LAN/Docker exposure of it is equivalent to remote control of the agent. If you don't run Desktop, the control endpoint simply does not exist — the native surfaces (`hermes logs`, `hermes sessions`, the kanban, raw `state.db`) cover observation on their own.
