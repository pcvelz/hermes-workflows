---
name: home-assistant-healthcheck
description: "Read-only Home Assistant health check — surface errors and stuck entities, never write to HA."
---

# Home Assistant health check (read-only)

> **POC / design target.** This skill describes the procedure a standalone
> Hermes would follow. It is non-destructive: **GET requests only**. It never
> calls a service, changes a state, or writes to Home Assistant. If a step would
> require anything other than a `GET`, stop and report instead.

## Inputs (environment)

- `HASS_BASE_URL` — base URL of the Home Assistant instance, e.g.
  `http://homeassistant.local:8123` (no trailing `/api`).
- `HASS_TOKEN` — long-lived access token, sent as `Authorization: Bearer ${HASS_TOKEN}`.

> **Preferred: do not hold the token at all.** Invoke the **broker**
> (`ha-broker.sh`, see [`SECURE-SECRETS.md`](SECURE-SECRETS.md)) instead. The
> broker reads the token from the vault (`seckit`/`op`), performs the read-only
> calls on the host, and returns only the findings — the token never enters the
> agent or the docker sandbox (`docker_forward_env: []`). If you must hold the
> token directly (scoped-forward fallback), it is a placeholder injected from a
> secrets store at launch, never hard-coded.

## Memory (read at start, append at end)

Read the known-noisy-entity list from your memory seed
(`MEMORY.seed.md.example` is the starting shape). Use it to **suppress**
findings you have already classified as expected noise so you do not re-alert on
them every run.

## Procedure

All requests carry the header `Authorization: Bearer ${HASS_TOKEN}` and
`Content-Type: application/json`.

### 1. Confirm HA is up — `GET ${HASS_BASE_URL}/api/config`

- A `200` with a JSON body means HA is reachable. Note `version` and
  `location_name`.
- Inspect the `components` list and, if present, any `errors` field.
- A non-`200`, a timeout, or a connection refusal is itself the top finding:
  **HA is unreachable** — report and stop the remaining steps.

### 2. Scan the error log **and system log** — `GET ${HASS_BASE_URL}/api/error_log`

- This returns plain text (not JSON). Collect lines containing `ERROR` and
  `WARNING`.
- Group repeated errors by their integration / component (the `[component]`
  prefix) so a single broken integration is **one finding**, not a wall of
  noise. Report the top offending components with counts.
- Where the deployment exposes the broader **system log** (Supervisor / host
  logs via the websocket or a host-bridge read-allowlist), scan it too for
  recurring restarts, OOM kills, or disk/database warnings — these are the
  errors that never reach `error_log`.
- Summarize the distinct problems, newest first. Suppress any that match a
  known-noisy pattern from memory.

### 3. Find stuck entities — `GET ${HASS_BASE_URL}/api/states`

- This returns an array of entity-state objects.
- Flag every entity whose `state` is exactly `unavailable` or `unknown`.
- For each, keep `entity_id` and `last_changed` so the report shows how long it
  has been stuck.
- Keep the full set of valid `entity_id`s — step 4 needs it.
- Suppress entities listed as known-noisy in memory.

### 4. Dashboard "entity not found" cross-check

- A common silent breakage: a dashboard (Lovelace) card or a template still
  references an entity that was renamed or deleted, producing an **"Entity not
  found"** card the human only sees when they happen to open that view.
- Where the Lovelace config is reachable read-only (websocket `lovelace/config`,
  or YAML-mode dashboard files via a host-bridge read-allowlist), extract every
  `entity:` / `entities:` reference and diff it against the valid `entity_id`
  set from step 3. Report any referenced entity that no longer exists.
- Storage-mode Lovelace is often **not** exposed over plain REST. If you cannot
  read it, say so honestly and report only what you can verify (e.g. template
  sensors referencing missing entities) rather than guessing.

## Reporting

- **All clear:** if nothing new is wrong, report a single line, e.g.
  `HA OK — version <x>, 0 new errors, 0 newly-unavailable entities.`
- **Findings:** otherwise report a short, skimmable summary:
  - HA reachability + version.
  - Top new errors/warnings (grouped by component, with counts).
  - Newly `unavailable` / `unknown` entities (entity_id + how long).
  - Dashboard references to missing entities ("Entity not found").
- **Structural suggestions (advisory, occasional — not every run):** when a
  pattern recurs, recommend a structural improvement rather than just re-listing
  symptoms. Examples: an automation that alerts when any entity is unavailable
  for > 1h; pruning dashboard cards that point at deleted entities; a template
  "health" sensor aggregating unavailable counts; splitting a flapping
  integration onto its own poll interval. These are **recommendations for the
  human only** — never act on them (see Hard boundaries).
- Keep it terse. This runs on a schedule; a wall of text trains the human to
  ignore it.

## Memory update (end of run)

- If an entity or error has now appeared as a problem across multiple
  consecutive runs and the human has not acted, propose adding it to the
  known-noisy list (so future runs suppress it) — but only **append** a note;
  do not silently drop genuinely new failures.
- Never remove a finding from the report just because it is inconvenient. The
  noisy-list is for things explicitly known to be expected, not for hiding
  unresolved problems.

## Hard boundaries

- **GET only.** No `POST` to `/api/services/...`, no `/api/states/<entity>`
  writes, no config changes. If a fix seems warranted, report it as a
  recommendation for the human — do not perform it.
