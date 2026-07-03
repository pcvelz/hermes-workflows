# hermes-pi (pi-coding-agent worker — OPTIONAL)

**Purpose:** A `node:20-slim` worker that wraps
[`@mariozechner/pi-coding-agent`](https://www.npmjs.com/package/@mariozechner/pi-coding-agent).
In this scaffold it is an **optional** companion pointed at your configured LLM
backend via the shared `x-llm-env` anchor in `docker-compose.yml`.

**Image:** `node:20-slim` (standard Node.js LTS slim image). The agent is
installed and launched via `npx` on container start.

**Port:** None. `hermes-pi` is a **worker**, not a server — it does not expose
any listening port. There is no healthcheck.

**Configuration:**

`hermes-pi` inherits the `x-llm-env` anchor from `docker-compose.yml` and
adds offline-enforcement variables:

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `${LLM_BASE_URL}` | Routes Anthropic-protocol calls to your backend |
| `ANTHROPIC_API_KEY` | `${LLM_API_KEY}` | API key for the backend (injected at launch via secrets layer) |
| `OPENAI_BASE_URL` | `${OPENAI_BASE_URL}` | Routes OpenAI-compatible calls (leave empty if not needed) |
| `OPENAI_API_KEY` | `${OPENAI_API_KEY}` | OpenAI key (leave empty if using the Anthropic path) |
| `HERMES_MODEL` | `${HERMES_MODEL}` | Model id as the backend expects it |
| `PI_OFFLINE` | `1` | Prevents pi-coding-agent from contacting external APIs |
| `PI_ARGS` | *(empty)* | Optional extra flags for `pi-coding-agent` |

Set `PI_OFFLINE=1` combined with an empty API key for any provider you don't
want pi to contact — this prevents unintended cloud routing.

**Persistence:**

- `${HERMES_HOME}/pi:/app/data` — bind mount for pi workspace and state;
  persists across container restarts.
- `pi-npm:/app/node_modules` — named volume that caches the `npx` install so
  subsequent container starts skip the npm pull.

**Backend reachability:**

`hermes-pi` uses `extra_hosts: host-gateway` to resolve `host.docker.internal`.
If your LLM backend is self-hosted and loopback-bound, see
[../README.md](../README.md) → "Backend reachability" for how to expose it to
the Docker bridge.

**Version pinning:**

The default `command` uses `npx --yes @mariozechner/pi-coding-agent` without a
version pin, which trades reproducibility for first-run simplicity (npx always
fetches the latest published version). For stable use, pin the version:

```yaml
command: ["sh", "-c", "npx --yes @mariozechner/pi-coding-agent@<version> ${PI_ARGS:-}"]
```

The `pi-npm` named volume caches the downloaded package so the pin does not
incur a network fetch on every restart.

**Read timeouts:**

If your backend has cold-start latency (e.g. a self-hosted model loading into
VRAM on first request), set `HERMES_HTTP_TIMEOUT` generously. A hosted cloud API
typically responds in under 60 seconds; a cold self-hosted model may need
120 seconds or more.

**Verifying offline mode:**

If pi attempts to contact an external API you did not intend:

1. Verify the relevant API key env var is empty (not just a fake value — some
   clients check non-emptiness before attempting a request).
2. Confirm `PI_OFFLINE=1` is set.
3. Confirm `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` point at your intended
   backend, not a public API.
4. Verify the backend is reachable from inside the container (see
   [../README.md](../README.md) for the test curl command).
