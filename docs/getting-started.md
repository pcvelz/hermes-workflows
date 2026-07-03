# Getting Started

This guide walks a fresh user through standing up a Hermes-workflows scaffold from scratch.
It documents the five-step procedure from clone to smoke test. The config templates and
scripts are statically validated in CI (syntax, links, YAML/JSON validity). The full
clone → pip-install → smoke-test chain has not been exercised in a clean throwaway
environment; treat Steps 1–5 as the documented intended path, not an end-to-end
machine-verified one.

---

## Step 1 — Clone the repository

```bash
git clone <your-fork-url> hermes-workflows
cd hermes-workflows
```

Choose a runtime home directory. The scaffold uses `HERMES_HOME` throughout:

```bash
export HERMES_HOME=~/.hermes
mkdir -p "$HERMES_HOME"
```

Copy the config templates into your runtime home, then rename each `*.example` file to its
real name (strip the `.example` marker). Note the marker is sometimes a **suffix**
(`config.yaml.example`) and sometimes an **infix** (`hindsight.example.yaml`,
`llm-providers.example.yaml`), so the rename must handle both:

```bash
cp -r config/. "$HERMES_HOME/"
# Strip ".example" whether it is a trailing suffix OR sits in the middle of the name.
find "$HERMES_HOME" -name "*.example*" | while read -r f; do
    mv "$f" "${f/.example/}"
done

# Verify zero .example files remain:
find "$HERMES_HOME" -name "*.example*"   # should print nothing
```

Create the Python virtual environment and install the upstream **NousResearch
hermes-agent** (this provides the `hermes` CLI and the `hermes_cli` Python module):

> **Python version requirement: >=3.11,<3.14.**
> `hermes-agent` declares `python_requires = ">=3.11,<3.14"`. On macOS the default
> `python3` may already be Python 3.14+, which causes `pip install` to fail with
> *"requires a different Python: not in >=3.11,<3.14"*.
>
> Check your version: `python3 --version`
>
> If it is 3.14+, install Python 3.11 first, then use it explicitly:
> ```bash
> # Homebrew:
> brew install python@3.11
> # pyenv:
> pyenv install 3.11
> ```

```bash
# Use python3.11 explicitly — do NOT use the bare python3 if it is 3.14+.
python3.11 -m venv "$HERMES_HOME/venv"
"$HERMES_HOME/venv/bin/pip" install --upgrade pip

# Install the upstream hermes-agent. Pin to a tag for reproducibility
# (replace v2026.6.19 with the tag you want — check the repo's Releases/Tags):
# Tags are DATE-BASED (e.g. v2026.6.19); find latest at the repo Releases page or via: git ls-remote --tags https://github.com/NousResearch/hermes-agent.git
"$HERMES_HOME/venv/bin/pip" install \
  "git+https://github.com/NousResearch/hermes-agent.git@v2026.6.19"

# Or, to track the moving HEAD (not reproducible):
#   "$HERMES_HOME/venv/bin/pip" install "git+https://github.com/NousResearch/hermes-agent.git"
```

> **Do NOT `pip install hermes-cli` or `pip install hermes-agent` from PyPI.** Those PyPI
> names are unrelated third-party packages — they are NOT the NousResearch agent. Install
> only from the git URL above.

After install, the console scripts `hermes`, `hermes-agent`, and `hermes-acp` are all
available in the venv. Verify:

```bash
"$HERMES_HOME/venv/bin/hermes" --help
# Also available: "$HERMES_HOME/venv/bin/hermes-agent" and "$HERMES_HOME/venv/bin/hermes-acp"
```

---

## Step 2 — Pick a backend

Hermes-workflows is **backend-agnostic**: it works with any endpoint that speaks the
Anthropic Messages or OpenAI wire protocol. The shipped config templates default to a
**local llama-swap proxy** (recommended) as a concrete starting point.

### Worked example: local llama-swap proxy

A local llama-swap proxy speaks the native Anthropic Messages protocol in front of your
own inference engine (llama.cpp or LM Studio). Configuration goes in your profile's
`config.yaml`. This guide uses the **`coder`** profile as the first-run example
(`$HERMES_HOME/profiles/coder/config.yaml`):

```yaml
# --- model selection: local llama-swap proxy (native-Anthropic) ---
model:
  default: <your-model-alias>           # e.g. qwen3-35b, llama-3.1-8b
  provider: anthropic                   # transport: Anthropic Messages wire protocol
  base_url: http://127.0.0.1:<PORT>     # NO /v1 suffix — the SDK appends /v1/messages
  context_length: 131072

# --- provider registry ---
# Note: with provider: anthropic above, Hermes uses the built-in Anthropic
# Messages transport directly. The local registry entry below is for
# documentation / future use (e.g. if you switch model.provider to local).
# It is NOT referenced by the model block above, and can be omitted for a
# minimal config. See docs/backend.md for when to use a named provider entry.
providers:
  local:
    name: Local LLM proxy (llama-swap)
    transport: anthropic_messages
    base_url: http://127.0.0.1:<PORT>
    # key_env: LLM_API_KEY              # env-var name; only if your proxy requires a bearer token

fallback_providers: []
credential_pool_strategies: {}
toolsets:
  - hermes-cli
```

> **Round-trip status:** the config above is authored and statically validated.
> A live round-trip needs your proxy actually running and serving the model id you set.
> On a machine also logged into Claude Code, the OAuth token can override any env key in
> the `anthropic` credential pool — pass `--ignore-user-config` so it does not
> (see [Known gotcha in docs/backend.md](backend.md#known-gotcha-claude-code-oauth-token-override)).
> The local path is config-authored; an actual round-trip has not been verified in this session.

**Important URL note:** the endpoint is `http://127.0.0.1:<PORT>` with **no `/v1` suffix**.
The Anthropic SDK internally appends `/v1/messages`. Adding `/v1` yourself causes a 404.

For the companion `.env` in your profile directory:

```bash
# $HERMES_HOME/profiles/coder/.env
LLM_API_KEY=${LLM_API_KEY}   # only if your proxy needs a bearer token; injected by your secrets layer
```

A local proxy typically needs no API key. If yours requires a bearer token, set
`LLM_API_KEY` (default `local`) and inject it via your secrets layer — see
[docs/secrets.md](secrets.md). Never put a literal key here.

### Swapping to a different backend

Only the `model:` block (and a matching `providers:` entry + env var) changes:

**Local llama-swap / llama.cpp / LM Studio** (if speaking Anthropic Messages):
```yaml
model:
  default: <your-local-model-alias>
  provider: anthropic
  base_url: http://127.0.0.1:<port>
  context_length: 131072
```

**Anthropic (Claude cloud)**:
```yaml
model:
  default: claude-opus-4-5
  provider: anthropic
  base_url: https://api.anthropic.com
  context_length: 131072
providers:
  anthropic:
    name: Anthropic
    transport: anthropic_messages
    base_url: https://api.anthropic.com
    key_env: ANTHROPIC_API_KEY
```

**OpenAI or OpenAI-compatible** (OpenRouter, etc.):
```yaml
model:
  default: gpt-4o
  provider: openai
  base_url: https://api.openai.com/v1
  context_length: 128000
providers:
  openai:
    name: OpenAI
    transport: openai
    base_url: https://api.openai.com/v1
    key_env: OPENAI_API_KEY
```

See [docs/backend.md](backend.md) for the full backend integration guide.

---

## Step 3 — Provision secrets

API keys must never live as plain text in YAML files, `.env` files committed to git, or
shell history. The scaffold provides a **generic secure secrets stack** built from three
composable layers. Full details are in [docs/secrets.md](secrets.md); this step gives you
the quick-start for each layer.

### Layer 1 — Vault: pick one (both are equal first-class options)

**Option A — Secrets-Kit (`seckit`, macOS)**
[Secrets-Kit](https://github.com/unixwzrd/Secrets-Kit) stores secrets in the macOS login
Keychain. Pin to a tagged release; it is macOS-only.

```bash
# Install (pin to a tagged release)
pip install secrets-kit==1.2.3

# Store the key once — value never committed, never in shell history
printf '%s' "<your-api-key>" | seckit set \
  --name LLM_API_KEY \
  --stdin \
  --kind api_key \
  --service hermes \
  --account local-dev \
  --source-label "Local LLM proxy (llama-swap)" \
  --source-url "http://127.0.0.1:<PORT>/" \
  --rotation-days 90
```

**Option B — 1Password CLI (`op`, cross-platform)**
If you already use 1Password, keep all secrets in your existing vault. Works on macOS,
Linux, Windows, and CI.

```bash
# Store the key in a vault item (run once)
op item create --category="API Credential" --title="Hermes LLM" \
  --field "credential[password]=REPLACE-ME" \
  --vault="Hermes"
```

Create an untracked `.env.op` that holds **references only** (no real values):

```dotenv
# .env.op — never commit this file with real values substituted
LLM_API_KEY=op://Hermes/Hermes LLM/credential
```

### Layer 2 — Injection: wrap your start command

**With Secrets-Kit:**

```bash
seckit run --service hermes --account local-dev --names LLM_API_KEY -- ./scripts/start.sh
```

**With 1Password:**

```bash
op run --env-file .env.op -- ./scripts/start.sh
```

Both tools inject the resolved value into the child process environment at launch time.
The real key never appears in the repo, in tracked files, or in your shell history.

### Layer 3 — Sandbox: keep the key out of the container

Once you switch to the docker terminal backend (recommended once the agent can ingest
untrusted content), set `docker_forward_env: []` in your config — this is the default.
The inference key stays in the agent process; nothing is forwarded into the container
shell:

```yaml
terminal:
  backend: docker
  docker_image: python:3.12-slim
  docker_mount_cwd_to_workspace: true
  docker_forward_env: []   # ← inference key stays in-process, not in container
```

**Honest framing:** both `seckit run` and `op run` are at-rest stores and launch-time
injectors. The launched Hermes process does inherit the key in its environment — that is
the designed injection mechanism. Per-request credential isolation (the key never in the
process at all) requires a local egress proxy — a future roadmap item. See
[docs/secrets.md — Honest framing](secrets.md#honest-framing--roadmap).

### Plain `.env` (fallback, not recommended for long-term use)

If you are just exploring locally:

```bash
# $HERMES_HOME/profiles/coder/.env  — keep this file 0600 and never commit it
LLM_API_KEY=local   # local proxy bearer (default); replace if your backend needs a real key
```

See [docs/secrets.md](secrets.md) for the full four-layer guide: vault provisioning,
injection commands, sandbox config, three-tier isolation model, and the roadmap for
per-request isolation.

> **Secure runs use the docker terminal backend.** The default `backend: local`
> runs shell commands directly on your host with full host privileges. Once the
> agent can ingest content you did not write — open-web pages, inbound messages,
> untrusted MCP results — switch to `backend: docker` to get a real OS boundary
> around shell execution. `config/config.yaml.example` contains a commented-out
> drop-in `terminal:` block with the recommended settings (pinned image,
> workspace-only mount, `docker_forward_env: []` to keep the inference key out
> of the sandbox). See [docs/security-hardening.md](security-hardening.md) for
> the full layered hardening guide and the tiered secrets model.

---

## Step 4 — Run the smoke test

Verify that the configured backend is reachable before starting the full agent stack.
The smoke test auto-detects the wire protocol: for a local proxy (and any
`anthropic_messages` backend) it POSTs to `/v1/messages` with the `x-api-key` and
`anthropic-version` headers; for OpenAI-style backends it uses `/v1/chat/completions`.

```bash
# Anthropic-Messages backend (the default local llama-swap proxy).
# Pass the base URL WITHOUT a /v1 suffix — the smoke test appends /v1/messages itself.
LLM_BASE_URL=http://127.0.0.1:<PORT> \
LLM_MODEL=<your-model-alias> \
LLM_TRANSPORT=anthropic_messages \
bash scripts/llm/llm-smoke-test.sh
# (LLM_PROTOCOL is accepted as a back-compat alias for LLM_TRANSPORT.)
```

A passing run prints `ALL CHECKS PASSED` and exits 0. If you see a connection error,
check that:

1. `LLM_API_KEY` is set if your proxy requires one (a local proxy usually needs none; a
   cloud backend needs its own key, injected via your secrets layer).
2. Your backend is reachable at the URL above. If using a hosted backend behind a VPN,
   ensure the VPN is active.
3. The `base_url` in your profile `config.yaml` matches the URL you used above
   (`http://127.0.0.1:<PORT>`, no `/v1`).

### Worked one-shot: hermes CLI against a local backend

Once the smoke test passes (or if you are wiring a different backend), you can
send a single prompt through the hermes agent CLI with `-z` (one-shot mode). This is a
useful sanity check before starting the full agent loop.

**Prerequisites:** the config template must define a `local` model entry pointing at your
inference backend. Uncomment and fill in the `local` block in
`$HERMES_HOME/config.yaml` (added by the config template under `model_aliases`):

```yaml
# Uncomment and edit — then use -m local to select it:
# local:
#   model: <your-local-model-id>     # e.g. qwen3-35b, llama-3.3-70b
#   provider: anthropic               # anthropic | openai
#   base_url: http://localhost:<PORT> # your local backend port
```

Once that entry is defined, run the one-shot:

```bash
"$HERMES_HOME/venv/bin/hermes" \
  -m local \
  --ignore-user-config \
  -z "Reply with exactly the single word: PONG"
```

**Why `--ignore-user-config`?**
Without it, the agent scans for ambient credential files — including the Claude Code OAuth
token at `~/.claude/.credentials.json` if Claude Code is installed on the machine. That
token gets loaded into the Anthropic credential pool and is then sent to your local
backend's URL, which does not accept it, producing an HTTP 401. `--ignore-user-config`
prevents the agent from picking up any host-level credential files, so the request reaches
your backend unauthenticated (as expected for a local endpoint) and succeeds.

**Why `-m local` rather than the default?**
The agent reads its backend from `config.yaml` (`model.provider` + `model.base_url`), not
from the `ANTHROPIC_BASE_URL` environment variable. Without an explicit `-m local`,
the agent falls back to `model.default` (your configured backend) or, if no Anthropic API key is
found in the env, ultimately latches the host OAuth token as described above. Defining a
named `local` entry in `model_aliases` and passing `-m local` is the reliable way to
target a local backend.

**Expected output (one-shot success):**
```
PONG
```

---

## Step 5 — Run an end-to-end workflow example

The `examples/` directory contains self-contained workflow examples that exercise the
dispatcher, the bridge, and the LLM backend together.

**Start with the autonomous-loop example** — it is the one example *demonstrated
end-to-end* (on a local backend): a task on the kanban board is autonomously claimed,
executed, and verdicted by a spawned worker with no human in the loop
(`board → worker → verdict → done`). It runs in a throwaway `HERMES_HOME`, so your live
`~/.hermes` is never touched.

**`HERMES_LOOP_MODEL` and `HERMES_LOOP_BASE_URL` are required** — the script exits with a
friendly guide if they are not set. Supply your own Anthropic-Messages (or
OpenAI-compatible) backend:

```bash
HERMES_LOOP_MODEL=your-dotless-alias \
HERMES_LOOP_BASE_URL=http://your-backend:port \
  bash examples/autonomous-loop/run-loop-proof.sh
```

> **Model id must be DOTLESS.** hermes-agent normalises `.` → `-` in model ids before
> sending the request (e.g. `Foo3.6-Bar` becomes `Foo3-6-Bar` on the wire). Proxies such
> as llama-swap return HTTP 404 if the normalised form has no matching alias. Use a dotless
> alias your backend actually exposes. See [docs/backend.md](backend.md) for details.

See [examples/autonomous-loop/README.md](../examples/autonomous-loop/README.md) for the
full walkthrough and the real-shaped "tail a CI pipeline to a verdict" task you grow into.

The other shipped example is the **Home Assistant healthcheck** — a dog-food workflow that
is a design target (the dispatcher/HA adapter are not wired), useful for the script
scaffolding even if you do not run Home Assistant.

> **Dog-food note:** the Home Assistant example documents the specific use-case we run
> ourselves. It is one example, not the core pattern. Point it at your own service or
> adapt it to a different workflow.

Start with the **offline mock path** — it needs no Home Assistant instance and no network.
This is the default first run; it exercises the shell scripts (broker + mock healthcheck)
end-to-end without touching a live service. Note: the dispatcher and HA adapter are
design targets (not wired); the mock run demonstrates the script scaffolding, not a
running agent workflow:

```bash
# Offline proof mode (default first run — no HA, no network needed):
HASS_MOCK=1 bash examples/home-assistant-healthcheck/run.sh
```

Once that passes, you can run it **live** against a real Home Assistant instance. Two
prerequisites must be in place before dropping `HASS_MOCK`:

1. **`HASS_TOKEN`** — a long-lived access token created in Home Assistant under
   **Profile → Security → Long-lived access tokens**. The token must be provisioned
   into your secrets vault and injected (via `seckit run` or `op run`) before launch.
   Without it the HA API returns HTTP 401 and the run aborts.

2. **`seckit` or `op`** — the broker (`ha-broker.sh`) reads the token from your vault.
   `seckit` is [Secrets-Kit](https://github.com/unixwzrd/Secrets-Kit), a separate
   macOS package (not bundled in this repo); `op` is the 1Password CLI. One of the
   two must be installed. See [examples/home-assistant-healthcheck/SECURE-SECRETS.md](../examples/home-assistant-healthcheck/SECURE-SECRETS.md)
   for the full provisioning walkthrough.

```bash
# Live mode — requires a reachable HA instance, a provisioned HASS_TOKEN, and
# either seckit (macOS) or op (1Password CLI) available on PATH:
export HASS_BASE_URL="http://homeassistant.local:8123"
HASS_VAULT=seckit bash examples/home-assistant-healthcheck/run.sh
# or 1Password: HASS_VAULT=op HASS_OP_REF="op://Hermes/HA Token/credential" bash ...
```

See [examples/home-assistant-healthcheck/README.md](../examples/home-assistant-healthcheck/README.md)
for the full walkthrough, including how to point the workflow at your own Home Assistant
instance (or adapt it to a different endpoint) and interpret the output.

---

## Next steps

- **Understand the architecture** — [docs/architecture/](architecture/README.md)
- **Configure additional profiles** — [docs/profiles.md](profiles.md)
- **Set up the Docker stack** — [docker/README.md](../docker/README.md)
- **Memory layers 1 and 2** — [docs/memory.md](memory.md) (layers 1–2 are file-based and
  ready; layer 3 semantic store is a design target, not yet implemented)
- **Secrets in depth** — [docs/secrets.md](secrets.md)
- **Backend options in depth** — [docs/backend.md](backend.md)
