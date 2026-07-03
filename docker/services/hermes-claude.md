# hermes-claude (Claude Code worker — OPTIONAL)

**Purpose:** A `node:20-slim` worker that wraps
[`@anthropic-ai/claude-code`](https://www.npmjs.com/package/@anthropic-ai/claude-code)
(the `claude` CLI). In this scaffold it is an **optional** companion pointed at
the SAME configured LLM backend as `agent` and `hermes-pi`, via the shared
`x-llm-env` anchor in `docker-compose.yml` — same models, same
OpenAI-/Anthropic-compatible endpoint. Claude Code speaks the Anthropic Messages
protocol, so it is driven entirely through `ANTHROPIC_BASE_URL` /
`ANTHROPIC_API_KEY` inherited from that anchor.

**Image:** `node:20-slim` (standard Node.js LTS slim image). The CLI is
installed and launched via `npx` on container start.

**Port:** None. `hermes-claude` is a **worker**, not a server — it does not
expose any listening port. There is no healthcheck.

**Configuration:**

`hermes-claude` inherits the `x-llm-env` anchor from `docker-compose.yml` and
adds Claude-Code-specific model routing and offline-enforcement variables:

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `${LLM_BASE_URL}` | Routes Anthropic-protocol calls to your backend (a local llama-swap proxy by default; NO `/v1` suffix — the SDK appends `/v1/messages`) |
| `ANTHROPIC_API_KEY` | `${LLM_API_KEY}` | Bearer for the backend (injected at launch via secrets layer). For a local proxy `local` — or empty — is fine |
| `OPENAI_BASE_URL` | `${OPENAI_BASE_URL}` | Routes OpenAI-compatible calls (leave empty if not needed) |
| `OPENAI_API_KEY` | `${OPENAI_API_KEY}` | OpenAI key (leave empty if using the Anthropic path) |
| `HERMES_MODEL` | `${HERMES_MODEL}` | Model id as the backend expects it (the alias your proxy exposes) |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `${HERMES_MODEL}` | Maps the opus tier to your single backend model |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `${HERMES_MODEL}` | Maps the sonnet tier to your single backend model |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `${HERMES_MODEL}` | Maps the haiku tier to your single backend model |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `${CLAUDE_OFFLINE}` (`1`) | Stops non-essential outbound calls (telemetry, auto-update, error reporting) |
| `CLAUDE_CONFIG_DIR` | `/app/data` | Persists Claude Code config/sessions in the bind mount below |
| `CLAUDE_ARGS` | *(empty)* | Optional extra flags for the `claude` CLI, e.g. `--model <your-model-alias>` |

**Model selection:** Claude Code has no single `ANTHROPIC_MODEL` variable — it
picks a model per tier (opus/sonnet/haiku) or via `--model`. Because a local
proxy typically serves a single alias, all three tier variables are mapped to
`HERMES_MODEL` so preflight requests don't 404 against your backend. Override
per run with `CLAUDE_ARGS=--model <your-model-alias>`.

**Offline enforcement:** Set `CLAUDE_OFFLINE=1` (the default) so
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` inside the container. Combined with
an `ANTHROPIC_BASE_URL` pointed at your own backend and no cloud
`ANTHROPIC_API_KEY`, this keeps the worker on your backend only.

**Persistence:**

- `${HERMES_HOME}/claude:/app/data` — bind mount for Claude Code config and
  session state (`CLAUDE_CONFIG_DIR`); persists across container restarts.
- `claude-npm:/app/node_modules` — named volume that caches the `npx` install so
  subsequent container starts skip the npm pull.

**Backend reachability:**

`hermes-claude` uses `extra_hosts: host-gateway` to resolve
`host.docker.internal`. If your LLM backend is self-hosted and loopback-bound,
see [../README.md](../README.md) → "Backend reachability" for how to expose it to
the Docker bridge.

**Version pinning:**

The default `command` uses `npx --yes @anthropic-ai/claude-code` without a
version pin, which trades reproducibility for first-run simplicity (npx always
fetches the latest published version). For stable use, pin the version:

```yaml
command: ["sh", "-c", "npx --yes @anthropic-ai/claude-code@<version> ${CLAUDE_ARGS:-}"]
```

The `claude-npm` named volume caches the downloaded package so the pin does not
incur a network fetch on every restart.

**Read timeouts:**

If your backend has cold-start latency (e.g. a self-hosted model loading into
VRAM on first request), set `HERMES_HTTP_TIMEOUT` generously. A hosted cloud API
typically responds in under 60 seconds; a cold self-hosted model may need
120 seconds or more.

**Verifying offline mode:**

If Claude Code attempts to contact an external API you did not intend:

1. Confirm `CLAUDE_OFFLINE=1` is set (so
   `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` inside the container).
2. Verify any cloud `ANTHROPIC_API_KEY` env var is empty — for a local proxy the
   bearer is a throwaway value (`local`) or empty.
3. Confirm `ANTHROPIC_BASE_URL` (and `OPENAI_BASE_URL`, if used) point at your
   intended backend, not a public API.
4. Verify the backend is reachable from inside the container (see
   [../README.md](../README.md) for the test curl command).
