# Example: Home Assistant health check (dog-food workflow)

> **Status: proof-of-concept / design target — not a running system.**
> This example demonstrates *how* a standalone Hermes would point a scheduled
> agent at a [Home Assistant](https://www.home-assistant.io/) instance, run a
> read-only health check, and retain what it learned between runs. The cron job
> here is a **starter prompt template**, not an active schedule, and the agent
> adapters it would call are unimplemented in this scaffold (see the repo
> [`README.md`](../../README.md) and [`docs/backend.md`](../../docs/backend.md)).
> Nothing here writes to your Home Assistant.

This is the "eat our own dog food" target: the most boring useful thing an
always-on agent can do is notice when your home automation is quietly broken —
an integration that went unavailable overnight, a sensor stuck on `unknown`, a
flood of errors in the log — and tell you before you trip over it.

---

## What it does (and does not do)

**Does (read-only):**

- `GET /api/config` — confirm HA is up, note version and reported problems.
- `GET /api/error_log` — surface recent errors and warnings, **grouped by
  integration**, plus the system log where it is exposed.
- `GET /api/states` — find entities that are `unavailable` or `unknown`.
- **Dashboard cross-check** — flag dashboard cards / templates that reference
  entities that no longer exist ("Entity not found").
- **Suggests structural improvements** to the setup (e.g. an availability
  alerting automation, pruning dead dashboard cards) — as recommendations only.
- Reports a short findings summary through whatever channel you wire up.

**Does not:**

- Never calls a service, never changes a state, never writes to HA. This is a
  **non-destructive** check. It only issues HTTP `GET` requests.
- Does not restart, reconfigure, or "fix" anything. It reports; you decide.

---

## Point it at your own Home Assistant

Everything is driven by two environment variables. There are **no real URLs or
tokens in this repo** — fill in your own.

| Variable        | Example                       | Meaning                                                        |
| --------------- | ----------------------------- | -------------------------------------------------------------- |
| `HASS_BASE_URL` | `http://homeassistant.local:8123` | Base URL of your HA instance (no trailing `/api`).         |
| `HASS_TOKEN`    | `${HASS_TOKEN}`               | A **long-lived access token** (placeholder — never commit it). |

Create a long-lived access token in Home Assistant under
**Profile → Security → Long-lived access tokens → Create token**. Treat it like
a password. Store it in a secrets manager — see
[`SECURE-SECRETS.md`](SECURE-SECRETS.md) in this directory, which walks our HA
token through the repo's generic vault + broker model (Secrets-Kit **or**
1Password, equal footing). **Preferred: the broker** (`ha-broker.sh`) so the
agent never holds the token at all.

```bash
export HASS_BASE_URL="http://homeassistant.local:8123"
export HASS_TOKEN="…"   # injected from your secrets store, never hard-coded
```

---

## Files in this example

| File                          | What it is                                                                 |
| ----------------------------- | -------------------------------------------------------------------------- |
| `run.sh`                      | **Entrypoint** — orchestrates the example: checks prerequisites, runs the connectivity probe and broker. Start here. |
| `check-hass-connectivity.sh`  | Connectivity probe (GET /api/config, /api/error_log, /api/states). Called by `run.sh`; can also be run standalone. |
| `ha-broker.sh`                | **Broker**: reads the token from the vault, runs the read-only health check, never prints the token. Has a `HASS_MOCK=1` offline proof mode. |
| `SECURE-SECRETS.md`           | Dog-food walkthrough: our HA token through the generic vault + **broker** model. |
| `jobs.json.example`           | Cron job definition (repo cron shape) — a **starter prompt template**.      |
| `healthcheck-skill.md`        | The workflow prompt / skill the agent runs: a read-only HA health check.    |
| `MEMORY.seed.md.example`      | Memory-seed example — what the agent should retain between runs.             |

---

## Secure secrets: use the broker (preferred)

The recommended way to give this check a token is the **broker** — the host
reads the token from your vault and performs the HA call itself, so the agent
(and the docker sandbox) never holds it. Full walkthrough:
[`SECURE-SECRETS.md`](SECURE-SECRETS.md). Quick version:

```bash
export HASS_BASE_URL="http://homeassistant.local:8123"
HASS_VAULT=seckit bash examples/home-assistant-healthcheck/ha-broker.sh
# or 1Password:
HASS_VAULT=op HASS_OP_REF="op://Hermes/HA Token/credential" \
  bash examples/home-assistant-healthcheck/ha-broker.sh
```

**Prove it offline (placeholder token, no network):**

```bash
HASS_MOCK=1 HASS_TOKEN="placeholder-not-a-real-token" \
  bash examples/home-assistant-healthcheck/ha-broker.sh | tee /tmp/proof.txt
grep -F "placeholder-not-a-real-token" /tmp/proof.txt && echo LEAK || echo "no leak (pass)"
```

The broker scrubs every output line (bearer headers, `op://` refs, JWT-shaped
strings) so the token can never reach stdout, the agent, or a log.

---

## Run the example

The canonical entrypoint is `run.sh`. It verifies prerequisites, runs the
connectivity probe, and then invokes the broker — all in one go.

**Offline proof (no real HA or token needed):**

```bash
HASS_MOCK=1 bash examples/home-assistant-healthcheck/run.sh
```

**Live run — broker resolves the token from your vault (preferred):**

```bash
export HASS_BASE_URL="http://homeassistant.local:8123"
HASS_VAULT=seckit bash examples/home-assistant-healthcheck/run.sh
# or 1Password:
HASS_VAULT=op HASS_OP_REF="op://Hermes/HA Token/credential" \
  bash examples/home-assistant-healthcheck/run.sh
```

**Live run — direct token injection (fallback):**

```bash
export HASS_BASE_URL="http://homeassistant.local:8123"
export HASS_TOKEN="…"   # long-lived access token — never commit it
bash examples/home-assistant-healthcheck/run.sh
```

If you prefer to run the connectivity probe in isolation (e.g. to debug
network/auth separately from the broker), it can be called directly:

```bash
export HASS_BASE_URL="http://homeassistant.local:8123"
export HASS_TOKEN="…"
bash examples/home-assistant-healthcheck/check-hass-connectivity.sh
```

If that prints your HA version and a count of unavailable entities, the agent
would have everything it needs.

---

## How this would fire in a real deployment

This example is wired to demonstrate the two scaffold mechanisms working
together — **honestly, as a design target, not a measured run**:

1. **Cron** (`jobs.json.example`) schedules the check on an interval. In this
   scaffold, cron entries are starter prompt templates; a real deployment would
   load them into the gateway's dispatcher (`docs/architecture/data-flow.md`).
2. **Memory** (`MEMORY.seed.md.example`) is the per-run context the agent keeps.
   On each run it reads its known-noisy-entity list so it does not re-alert on
   things you have already told it to ignore, and appends genuinely new findings.

When the dispatcher and HA adapter are implemented, this same job + memory pair
would run unattended. Until then, the connectivity helper is the part you can
actually run today.
