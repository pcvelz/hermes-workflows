# Secrets

How to set up a **generic, portable secure secrets stack** for Hermes-workflows.
**No real secret value ever lives in this repository** — only the *names* of
environment variables and placeholder examples.

The worked example throughout is the recommended default backend: a local
llama-swap proxy speaking the Anthropic Messages protocol, whose bearer token
(only if the proxy requires one) is read from `LLM_API_KEY`. Substitute your own
provider's key name where appropriate — `ANTHROPIC_API_KEY` for Anthropic Cloud,
`OPENAI_API_KEY` for an OpenAI-compatible endpoint, or the matching key for any
hosted Anthropic-compatible endpoint — the mechanics are
identical.

> **Honesty note (read this first).** Both vault options below do two things:
> **at-rest storage** of the secret (in the macOS Keychain or your 1Password
> vault) and **launch-time injection** of it into Hermes's environment. Neither
> gives you per-request isolation: once Hermes is launched, the process — and any
> subprocess it spawns — can read the real key from its environment. "Hermes
> never sees the secret" is **not** what these tools provide. See
> [Honest framing & roadmap](#honest-framing--roadmap).

---

## The secure stack in four layers

A complete deployment has four layers that compose. Skim the picture, then
follow the section for each layer.

```
┌─────────────────────────────────────────────────┐
│  1. VAULT       choose one                      │
│     Secrets-Kit  OR  1Password (op)             │
│     at-rest storage; neither option is primary  │
├─────────────────────────────────────────────────┤
│  2. INJECTION   launch-time                     │
│     seckit run  OR  op run  wraps start.sh      │
│     value never touches the repo / shell history│
├─────────────────────────────────────────────────┤
│  3. SANDBOX     docker terminal backend         │
│     docker_forward_env: []  keeps the key       │
│     out of every container shell — proven       │
├─────────────────────────────────────────────────┤
│  4. TIERS       three isolation levels          │
│     Tier 1: inference key in-process only       │
│     Tier 2: task secrets via broker / scoped fwd│
│     Tier 3: egress proxy — roadmap              │
└─────────────────────────────────────────────────┘
```

---

## How Hermes consumes a secret

Hermes profiles never embed a key. A provider entry names an env var via
`key_env`, and Hermes reads that variable at runtime:

```yaml
providers:
  local:
    name: Local LLM proxy (llama-swap)
    transport: anthropic_messages
    base_url: http://127.0.0.1:<PORT>
    key_env: LLM_API_KEY    # env-var NAME only — the value is never in yaml
```

The only job of a secrets layer is: **make `LLM_API_KEY` present in Hermes's
environment when it starts, without that value ever touching the repo, your shell
history, or a tracked `.env` file.**

---

## Layer 1 — Vault: choose Secrets-Kit or 1Password (equal footing)

Both options are fully supported, portable, and production-ready for this use
case. Pick the one that already fits your workflow.

| | Secrets-Kit (`seckit`) | 1Password (`op`) |
|---|---|---|
| **Platform** | macOS (login Keychain) | macOS, Linux, Windows, CI |
| **Pre-requisite** | pip install | 1Password account + CLI |
| **At-rest store** | macOS login Keychain | 1Password vault |
| **Secret reference** | service / account / name triple | `op://Vault/Item/field` |
| **Headless / CI** | manual encrypted export | Service Account token |
| **Best for** | macOS-only local dev; no extra account | existing 1Password users; cross-platform |

### Option A — Secrets-Kit (`seckit`)

[Secrets-Kit](https://github.com/unixwzrd/Secrets-Kit) is a small, zero-infra,
macOS-only CLI that stores secret values in the **login Keychain** and launches
a child process with selected secrets injected as environment variables.

> Pin to a tagged release. Secrets-Kit is young and single-maintainer; review
> before depending on it long-term.

#### Provision the key (once, never committed)

A secret is identified by the triple **(service, account, name)**. Store secrets
under service `hermes`:

```bash
# Read the key from stdin so it never lands in your shell history or argv.
printf '%s' 'sk-REPLACE-ME' | seckit set \
  --name LLM_API_KEY \
  --stdin \
  --kind api_key \
  --service hermes \
  --account local-dev \
  --source-label "Local llama-swap proxy" \
  --source-url "http://127.0.0.1:<PORT>" \
  --rotation-days 90
```

`sk-REPLACE-ME` is a placeholder. Substitute your real key — nothing about this
command is committed to the repository.

Verify it landed (value stays redacted by default):

```bash
seckit list --service hermes
seckit doctor          # reports index/Keychain drift and rotation warnings
```

#### Inspect or rotate

```bash
seckit get LLM_API_KEY --raw    # prints the value (redacted unless --raw)
seckit doctor                    # rotation/expiry warnings
# rotate: re-run the `seckit set` from above with the new value
```

Cross-host transfer is a manual encrypted `seckit export` → copy the file →
`seckit import`. No live sync; no phone-home.

---

### Option B — 1Password CLI (`op`)

If you already run 1Password, keep Hermes's secrets in your existing vault and
inject them at launch with `op run` — no second tool to manage.

#### Provision the key (once, never committed)

Add an **API Credential** item to a vault named `Hermes`, e.g. an item called
`Hermes Local` with the key in a field named `credential`. Its secret reference
is:

```
op://Hermes/Hermes Local/credential
```

Or create it from the CLI:

```bash
op item create --category="API Credential" --title="Hermes Local" \
  --field "credential[password]=sk-REPLACE-ME" \
  --vault="Hermes"
```

#### Reference it from an env file (no value committed)

Create an untracked `.env.op` that holds **references, not values**:

```dotenv
# .env.op — secret references only; never commit this file with real values
LLM_API_KEY=op://Hermes/Hermes Local/credential
```

#### Inspect or rotate

```bash
op item get "Hermes Local" --vault Hermes --fields credential
# rotate: edit the item in the 1Password app or via `op item edit`
```

#### Headless / CI

For unattended runs, use a **1Password Service Account** (non-human,
vault-scoped, revocable token):

```bash
export OP_SERVICE_ACCOUNT_TOKEN="ops_..."   # supplied by your own secrets layer
op run --env-file .env.op -- ./scripts/start.sh
```

---

## Layer 2 — Injection: launch-time env population

Whichever vault you chose, wrap `./scripts/start.sh` at launch time so the value
is injected into Hermes's process without touching the repo, `.env` files, or
shell history:

**With Secrets-Kit:**

```bash
seckit run \
  --service hermes \
  --account local-dev \
  --names LLM_API_KEY \
  -- ./scripts/start.sh
```

`seckit run` resolves the named secret in the parent process and `exec`s the
child. **Always pass `--names`** (least privilege) — without a selection flag,
`seckit run` injects *every* secret in the scope into the child.

To drop the `--service`/`--account` flags for contributors, ship a defaults file
(see [`../secrets/seckit-defaults.example.json`](../secrets/seckit-defaults.example.json)
→ copy to `~/.config/seckit/defaults.json`). Then:

```bash
seckit run --names LLM_API_KEY -- ./scripts/start.sh
```

**With 1Password:**

```bash
op run --env-file .env.op -- ./scripts/start.sh
```

`op run` resolves each `op://` reference at launch and injects the real value
into the child process's environment for its lifetime.

One-off read (useful for testing):

```bash
export LLM_API_KEY="$(op read 'op://Hermes/Hermes Local/credential')"
./scripts/start.sh
```

---

## Layer 3 — Sandbox: `docker_forward_env: []`

When you run the docker terminal backend, a third dimension opens: **what flows
into the container**. The agent holds the inference key in its environment after
launch-time injection; `docker_forward_env` controls which of those env vars are
exported into the container shell. With an empty list — the default — **nothing
is forwarded**: a shell command inside the sandbox cannot read `$LLM_API_KEY`
via `env` or `printenv`, because the variable is simply not present in that
process.

```yaml
# config/config.yaml.example — secure terminal block
terminal:
  backend: docker
  docker_image: python:3.12-slim
  docker_mount_cwd_to_workspace: true
  docker_forward_env: []   # ← nothing forwarded; inference key stays in-process
  docker_run_as_host_user: false
```

**Proven:** this has been verified against both the raw docker terminal backend
and the real hermes-agent `DockerEnvironment`. Host-env variables including the
inference key do not appear inside the container shell with this config.

This is an accident-prevention control (T1/T3 in the
[threat model](security-hardening.md#1-threat-model-on-one-screen)). A
prompt-injected model that routes calls through the agent's own inference tools
can still use the key — that is what Tier 3 (egress proxy) closes.

---

## Layer 4 — Tiers: three isolation levels

**Tier 1 — Inference key: in-process, not in sandbox.**
`LLM_API_KEY` (or your provider's key) lives in the hermes-agent process,
injected by `seckit run` / `op run`. `docker_forward_env: []` keeps it out of
every container shell. _What it stops:_ casual env-read by a cooperative model or
a naively injected command. _What it does not stop:_ an injected command that
routes through the agent's own inference tools.

**Tier 2 — Task secrets: broker pattern (preferred) or scoped forward (fallback).**
Some tasks need a credential at execution time — a deploy key, a webhook token, a
read-only API token for a third-party service. The preferred approach is the
**host-bridge broker**: the host-side bridge (see
[security-hardening.md §7](security-hardening.md#7-privileged-helpers-the-host-bridge-pattern))
holds the credential and performs the privileged action. The agent sends a named
operation; the bridge authenticates. The agent never holds the token.

If a broker is impractical, forward exactly one name via `docker_forward_env`:

```yaml
terminal:
  backend: docker
  docker_forward_env: [TASK_API_TOKEN]   # one task-scoped secret; revocable, read-only preferred
```

Never forward more than the task strictly requires. Rotate after use.

**Tier 3 — Gold standard: egress proxy (roadmap).**
A local sidecar holds the inference key and attaches the auth header on each
outbound call. Hermes is pointed at `http://127.0.0.1:<PORT>` and never holds
the real credential. This is the only approach where "the agent cannot read the
key" is literally true. Not implemented in this scaffold; documented as a future
direction.

### Cross-reference

The concrete docker config block, the tiered model, and the broker pattern are
also described in [`security-hardening.md` §6](security-hardening.md#6-secrets-isolation--the-tiered-model-full-story-in-secretsmd).
Read both together: `secrets.md` is the vault + injection + sandbox + tiering
reference; `security-hardening.md` is the threat model + layered controls +
copy-pasteable config.

---

## Example: eating our own dog food (Home Assistant)

> This subsection documents the **specific instance** we run. It is not the core
> pattern — the core is the four-layer generic stack above. If you are running
> your own use-case, substitute your own service tokens below.

Our orchestrator checks a Home Assistant instance (healthcheck, failed entities)
via the host-bridge. The HA long-lived access token never enters the agent or the
sandbox — Tier 2 broker pattern:

1. Store the token in your chosen vault under the `hermes` service / `hass-local`
   account (Secrets-Kit) or as `op://Hermes/Home Assistant/token` (1Password).
2. The host-bridge holds the resolved `HASS_TOKEN` and exposes a named
   `hass_healthcheck` operation on loopback — the agent calls the operation, never
   the token directly.
3. If you want to try the scoped-forward fallback instead:

```yaml
terminal:
  backend: docker
  docker_forward_env: [HASS_TOKEN]
```

The full walkthrough lives in
[examples/home-assistant-healthcheck/README.md](../examples/home-assistant-healthcheck/README.md).

---

## Honest framing & roadmap

Both `seckit run` and `op run` perform **at-rest storage + launch-time
injection**. They keep keys out of the repo, out of tracked `.env` files, out of
your shell history, and out of process argv. That is a real and worthwhile
improvement over pasting keys into a committed `.env`.

What they do **not** do is per-request credential isolation. Once Hermes is
launched:

- the real `LLM_API_KEY` is in Hermes's environment, and
- Hermes (and any subprocess it spawns) can read it.

Secrets-Kit's own security model states this plainly: a child launched with
`seckit run` can read every variable it inherits. `op run` behaves the same way.
So **"Hermes never holds the secret" is not achievable with either tool alone.**

**Roadmap (not implemented in this scaffold):** true per-request isolation
requires a small local **egress proxy / sidecar** that holds the credential and
adds the auth header on outbound calls. Hermes would be pointed at
`http://127.0.0.1:PORT` instead of the provider and would only ever hold a
localhost URL — never the real key. The secrets layer (Secrets-Kit or 1Password)
would then secure the *proxy's* key. A Vault Agent with dynamic, short-lived
leased credentials is the heavier, server-based version of the same idea. Neither
is wired up here; this is a documented future direction, not a feature of this POC.

---

## What is out of scope (and why)

- **Bitwarden / Vaultwarden secrets container** — a self-hosted vault container
  plus `bw get` tends to return the raw secret into shell/argv/env and needs an
  unlock session; `seckit run` / `op run` are cleaner on a single machine and
  need no running service.
- **HashiCorp Vault / Infisical** — capable (Vault genuinely does dynamic,
  short-lived credentials) but heavy for a single-machine POC. Note them as the
  managed/dynamic option if you outgrow this scaffold.
- **Plain `.env` files committed to git** — forbidden. If you need a quick local
  test, write an *untracked* file with mode `0600` and delete it after.

See [`SECURITY.md`](../SECURITY.md) for the no-secrets-in-repo rules and network
exposure caveats.
