# Backend Integration

Hermes is backend-agnostic: it speaks the **Anthropic Messages** wire protocol (and optionally
OpenAI-compatible) and works with any endpoint that implements either. You bring your own
inference backend and fill in the config — no specific local model or cloud provider is required.

> **Config file is authoritative — env vars are not enough.**
> The agent reads its backend from `config.yaml` (`model.provider`, `model.base_url`).
> The `ANTHROPIC_BASE_URL` environment variable is **not** honored by the agent chat path —
> only the smoke script (`scripts/llm/llm-smoke-test.sh`) reads it from the environment.
> To point the agent at a different backend you must edit `config.yaml` (or a profile overlay),
> not just set an env var.

---

## Worked example: local llama-swap proxy (recommended default)

The shipped config templates default to a **local llama-swap proxy** — the recommended
starting point. It speaks the Anthropic Messages protocol in front of your own inference
engine, so no cloud account or API key is required. Replace it with any other endpoint and
key later.

llama-swap and the inference engine behind it (llama.cpp or LM Studio) are independent
pieces — run them separately, together, or swap either out. This scaffold targets the
macOS-extended fork [`pcvelz/llama-swap-macos-extended`](https://github.com/pcvelz/llama-swap),
but any Anthropic-Messages-compatible proxy works.

> **Verification status:** the local config is authored and statically validated. A live
> round-trip needs your proxy actually running and serving the model id you set. On a
> machine also logged into Claude Code, pass `--ignore-user-config` so the host OAuth token
> does not override your backend (see the
> [Known gotcha](#known-gotcha-claude-code-oauth-token-override) section below).

| Property       | Value                                                       |
|----------------|-------------------------------------------------------------|
| Provider name  | Local LLM proxy (llama-swap)                                |
| Model id       | `<your-model-alias>` (e.g. `qwen3-35b`, `llama-3.1-8b`)     |
| Endpoint       | `http://127.0.0.1:<PORT>` (llama-swap default port 8080)    |
| Protocol       | Anthropic Messages (native — no `/v1` suffix needed)        |
| API key env    | `LLM_API_KEY` (optional — only if your proxy requires one)  |

> **Important — no trailing `/v1`:** the base URL speaks the Anthropic Messages protocol
> directly. The Anthropic SDK appends `/v1/messages` internally. Adding `/v1` yourself causes
> a double-path 404 (`/v1/v1/messages`).

### Minimal config block

```yaml
# profiles/<role>/config.yaml
model:
  default: <your-model-alias>
  provider: anthropic          # Anthropic Messages wire protocol (built-in transport)
  base_url: http://127.0.0.1:<PORT>   # NO /v1 suffix; replace <PORT> (llama-swap default 8080)
  context_length: 131072

# The local entry below is an optional named registry entry.
# With provider: anthropic above, Hermes uses the built-in transport and does NOT
# reference it. Include this block for a fallback chain or documentation; it can be
# omitted for a minimal config.
providers:
  local:
    name: Local LLM proxy (llama-swap)
    transport: anthropic_messages
    base_url: http://127.0.0.1:<PORT>
    # key_env: LLM_API_KEY     # env-var NAME only; only if your proxy requires a bearer token
```

No API key is required for a local proxy. If your proxy demands a bearer token, set
`key_env` and inject the value out-of-band — see [Secrets](#secrets) below. Never paste a
real key into a yaml file.

---

## Swapping in your own backend

Only the `model:` block (and a matching `providers:` entry) needs to change. Three common
scenarios:

### 1. Local proxy (llama-swap, llama.cpp, LM Studio — Anthropic-compatible)

The shipped config templates include a ready-to-use `local` model alias. To activate it:

1. Uncomment the `local:` block under `providers:` in `config/config.yaml.example` (or your
   profile overlay) and set `base_url: http://127.0.0.1:<PORT>` to your proxy's actual port.
2. Uncomment the `local:` entry under `model_aliases:` and set `model:` to the model id your
   proxy registers (e.g. `qwen3-35b`, `llama-3.1-8b`).
3. Run with `-m local` and `--ignore-user-config` (see the gotcha section below):

```bash
hermes -m local --ignore-user-config -z "Reply with exactly: PONG"
```

The minimal config block for a local proxy:

```yaml
model:
  default: local                      # selects the model_aliases.local entry below
  provider: anthropic
  base_url: http://127.0.0.1:8001     # your proxy port; NO /v1 suffix
  context_length: 131072

# Named alias — selected by -m local on the command line:
model_aliases:
  local:
    model: qwen3-35b                  # model id your proxy expects
    provider: anthropic
    base_url: http://127.0.0.1:8001   # NO /v1 suffix; SDK appends /v1/messages

# Optional named provider (needed only if you use fallback_providers):
providers:
  local:
    name: Local LLM proxy
    transport: anthropic_messages
    base_url: http://127.0.0.1:8001
    # key_env: MY_DUMMY_KEY           # only if your proxy requires a bearer token
```

No API key is required for a local server (omit `key_env` or set a dummy env var if your
proxy requires one).

> **Container networking note:** if your backend binds loopback-only (`127.0.0.1`), containers
> cannot reach it via `localhost`. Expose it on the docker bridge or use
> `host.docker.internal`; on Linux add
> `extra_hosts: ["host.docker.internal:host-gateway"]` in your compose file.

> **Note on model id normalization:** some proxies or gateway layers normalize dotted model
> ids (e.g. rewrite `.` to `-`). If you hit 404 errors with a dotted model name, prefer a
> simple dotless alias registered in your proxy config.

#### Caller identity on a shared local backend

When several agents, Claude Code sessions and scripts share one local proxy, its queue
shows anonymous rows unless each request says who sent it. The convention is one request
header:

```
X-Caller-Purpose: hermes:<profile>[:<board>/<task_id>]
```

- `<profile>` is the Hermes profile (`HERMES_PROFILE`). Kanban workers add their board
  and task (`HERMES_KANBAN_BOARD`, `HERMES_KANBAN_TASK`), so a queue row maps straight to
  a card on the board.
- The value has at most 48 characters, from `[A-Za-z0-9._:/-]`. If board and task don't
  fit, the board is dropped: `hermes:<profile>:<task_id>`.
- Send it only to non-Anthropic endpoints; `api.anthropic.com` never needs it.
- Other scripts use their own slug, e.g. `X-Caller-Purpose: commit-subject`.

Upstream hermes-agent does not send this header. Add it with a local patch to
`agent/anthropic_adapter.py` (see [patches.md](patches.md)): in `build_anthropic_kwargs`,
for third-party endpoints, merge the header into `kwargs["extra_headers"]`. The llama-swap
fork used in development records it as the request's `metadata.purpose`. Any proxy that
logs request headers can use it the same way.

### 2. Anthropic Cloud (Claude)

```yaml
model:
  default: claude-opus-4-5   # or any available model id
  provider: anthropic
  base_url: https://api.anthropic.com
  context_length: 131072

providers:
  anthropic:
    name: Anthropic Cloud
    transport: anthropic_messages
    base_url: https://api.anthropic.com
    key_env: ANTHROPIC_API_KEY
```

Any hosted endpoint that speaks the Anthropic Messages protocol slots in here the same
way — swap `base_url` and `key_env` for the provider's values: its base URL (no `/v1`
suffix for an Anthropic-Messages endpoint) and its own `*_API_KEY` env var.

### 3. OpenAI or OpenAI-compatible (OpenRouter, etc.)

```yaml
model:
  default: gpt-4o
  provider: openai
  base_url: https://api.openai.com/v1
  context_length: 128000

providers:
  openai:
    name: OpenAI
    transport: openai          # NOT anthropic_messages
    base_url: https://api.openai.com/v1
    key_env: OPENAI_API_KEY
```

**Rule of thumb:**
- `provider: anthropic` / `transport: anthropic_messages` for any endpoint that speaks the
  Anthropic Messages protocol (local llama-swap proxy, Claude, or any Anthropic-compatible cloud endpoint)
- `provider: openai` / `transport: openai` for genuine OpenAI-style APIs

---

## Secrets

Never paste API keys into yaml files. Keys are referenced by env-var name via `key_env` and
injected at launch time. A local proxy typically needs no key at all — this section applies
to any backend that does require one (a cloud provider, or a local proxy configured with a
bearer token).

**macOS — Secrets-Kit (recommended):**

[Secrets-Kit](https://github.com/unixwzrd/Secrets-Kit) stores secrets in the macOS login
Keychain and injects them as env vars when launching Hermes. It replaces `.env` files and
shell-history exposure.

```bash
# Store the key once (operator runs; value never committed):
printf '%s' "sk-YOUR_KEY_HERE" | seckit set \
  --name ANTHROPIC_API_KEY --stdin --kind api_key \
  --service hermes --account local-dev \
  --source-label "Anthropic-compatible cloud endpoint" \
  --source-url "https://api.your-provider.example/" \
  --rotation-days 90

# Launch Hermes with the key injected (least-privilege: only ANTHROPIC_API_KEY):
seckit run --service hermes --account local-dev --names ANTHROPIC_API_KEY -- ./scripts/start.sh
```

Hermes reads `ANTHROPIC_API_KEY` from its environment via `key_env`. The key is held in the
Keychain; it is injected into Hermes's process env at launch. This keeps keys out of
`.env` files, shell history, and yaml.

**Honest framing:** Secrets-Kit is an at-rest store and launch-time injector — not a
per-request credential broker. Hermes (and its child processes) inherit the real key in
their environment. Per-request isolation would require a local egress proxy that holds the
key and forwards requests; that is a roadmap item, not implemented here.

**Alternative — 1Password `op run`:**

```bash
# With a Service Account or item reference:
op run --env-file=.env.op -- ./scripts/start.sh
```

`op run` is equally valid and cross-platform. Use whichever fits your setup.

**Plain env (simplest, less secure):**

```bash
export ANTHROPIC_API_KEY="sk-REPLACE_ME"
./scripts/start.sh
```

Acceptable for a local throw-away POC; do not use in shared or persistent environments.

---

## Known gotcha: Claude Code OAuth token override

If you point Hermes at a third-party Anthropic-Messages-compatible backend (any
non-`api.anthropic.com` endpoint) **while Claude Code is also logged in on the same
machine**, the credential pool seeds the Claude Code OAuth token — from
`~/.claude/.credentials.json` and the macOS Keychain entry `Claude Code-credentials` — into
the `anthropic` provider pool at **higher priority** than any `ANTHROPIC_API_KEY` you have
set. Hermes then sends the Claude OAuth token to your backend, which rejects it with
**HTTP 401** (`Auth method: Bearer (OAuth/setup-token), token prefix sk-ant-oat01…`).

**Important:** setting `ANTHROPIC_API_KEY` (via env or `.env`) is **not** enough to work
around this — the pool re-seeds the OAuth token from the Keychain on every load,
overriding the env key.

**Fixes (three options — pick one):**

**Option 1 — per-run flag (simplest, no side effects):**

Pass `--ignore-user-config` on every hermes invocation. This stops the agent from loading
`~/.claude/.credentials.json` and the macOS Keychain entry, so the OAuth token never enters
the credential pool. Your backend key (from `key_env`) is then the only credential:

```bash
hermes -m local --ignore-user-config chat
hermes -m local --ignore-user-config run "your task"
```

This is the recommended approach when running a local or third-party backend on a machine
that is also logged into Claude Code.

**Option 2 — remove the credential from the pool permanently:**

```bash
# Find the claude_code entry (note its index N):
hermes auth list

# Remove it from the anthropic provider pool:
hermes auth remove anthropic <N>
```

**Option 3 — clean machine:**

Run Hermes from a machine or user profile that is **not** logged into Claude Code — a clean
install with no Claude Code credentials never hits this path; your backend key is then the
only `anthropic` credential.

---

## Smoke test

Verify your configured endpoint is reachable before starting Hermes:

```bash
bash scripts/llm/llm-smoke-test.sh
```

By **default** the script runs the Anthropic Messages transport (`LLM_TRANSPORT=anthropic_messages`).
It does **two** real round-trips against your endpoint:

1. **Reachability + model check** — `POST {BASE_URL}/v1/messages` with `x-api-key` and
   `anthropic-version` headers, confirming the endpoint is live and echoes your model id.
   (The Anthropic Messages protocol has **no** `/v1/models` listing endpoint — reachability
   is proven by a real tiny request, not a model list.)
2. **Completion** — a second `POST {BASE_URL}/v1/messages` asking the model to
   `"Reply with exactly the single word: PONG"`.

Override defaults via env:

```bash
# Test the default local proxy (Anthropic Messages, NO /v1 suffix on the base URL):
bash scripts/llm/llm-smoke-test.sh

# Test a local Anthropic-Messages backend (e.g. llama-swap with Anthropic compat).
# Base URL has NO /v1 suffix — the script appends /v1/messages itself
# (a trailing /v1 here would double to /v1/v1/messages and 404):
LLM_BASE_URL=http://127.0.0.1:8001 LLM_MODEL=my-model-alias \
  bash scripts/llm/llm-smoke-test.sh

# Test a local OpenAI-compatible backend — set LLM_TRANSPORT=openai.
# Here the /v1 suffix IS correct: the script calls /v1/models then /v1/chat/completions:
LLM_TRANSPORT=openai LLM_BASE_URL=http://127.0.0.1:8001/v1 LLM_MODEL=my-model-alias \
  bash scripts/llm/llm-smoke-test.sh
```

---

## Provider chain (fallback)

Define multiple backends in `providers:` and order them in `fallback_providers`. Hermes
walks the list on error or rate-limit:

```
1. local       → http://127.0.0.1:<PORT>      (local proxy default, Anthropic Messages)
2. anthropic   → https://api.anthropic.com    (cloud fallback)
```

Leave `fallback_providers: []` to use a single backend.

---

## See also

- [`config/config.yaml.example`](../config/config.yaml.example) — base config template
- [`config/profiles/`](../config/profiles/) — per-role overlays
- [`config/providers/llm-providers.example.yaml`](../config/providers/llm-providers.example.yaml) — provider-chain template
- [`scripts/llm/llm-smoke-test.sh`](../scripts/llm/llm-smoke-test.sh) — endpoint smoke test
- [`docs/architecture/topologies.md`](architecture/topologies.md) — native vs Docker deployment
