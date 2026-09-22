# hermes-workflows

> A community-ready scaffold for running a Hermes-style autonomous software-development workflow on your own machine.

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Status](https://img.shields.io/badge/status-proof--of--concept-orange.svg)
![Built on](https://img.shields.io/badge/built%20on-NousResearch%20Hermes-7c3aed.svg)
![Backend](https://img.shields.io/badge/backend-bring--your--own-6b7280.svg)

```
                                       hermes-workflows
                    agents that hand work to each other, and never to themselves

        scout ────────▶ planner ────────▶ coding ────────▶ qa ────────▶ user (you)   
        surveys         splits the       does the         checks it     accepts, or sends
        the code        work up          work             by another    it back. Nobody
                                                          method        else may.

 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │  refinement ──▶ waiting ──▶ to do ──▶ in progress ──▶ user review ──▶ done ──▶ archived      │
 │        ▲              │                          │                │           │               │
 │        │              │ leaves by itself when    │ question       │ only you  │ policy        │
 │        │              │ the card it waits on     │ (work cannot   │ leave     │ driven        │
 │        │              │ moves                    │  continue)     │ this      │               │
 │        └──────────────┴──────────────────────────┴────────────────┘ column    │               │
 │                        rework always carries the reason it failed             │               │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘

   A worker cannot finish its own card: it hands over, with evidence.
   A card that gets stuck reaches a human once, not every minute.
   Columns, roles and the moves each role may make are data: config/board.yaml
```

```
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │                                    THE AGENTS, AS BOXES                                      │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘

   ┌───────────────────────────────────┐            ┌───────────────────────────────────┐
   │  SCOUT                            │            │  PLANNER                          │
   │  reads the ground, never edits    │──survey───▶│  turns the survey into steps      │
   ├───────────────────────────────────┤   file     ├───────────────────────────────────┤
   │  out: the files that need touching│            │  out: a work list, one session of │
   │       the open questions          │            │       work per item               │
   │       the tests that already exist│            │                                   │
   │  may not: edit code, create cards │            │  may not: edit code               │
   └───────────────────────────────────┘            └─────────────────┬─────────────────┘
                                                                      │ work list
                                                                      ▼
   ┌──────────────────────────────────────────────────────────────────────────────────────────┐
   │  CODING                                                                                  │
   ├──────────────────────────────────────────────────────────────────────────────────────────┤
   │  does the work, and proves it:   visual comparison → screenshots of both sides           │
   │                                  debugging         → verbose output and the command      │
   │                                  data              → counts, the query, the raw output   │
   │                                                                                          │
   │  may not: set done · set user review · archive · grade its own work                      │
   └────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                            │ hand-off: a verdict per acceptance criterion,
                                            │           each with the path to its evidence
                                            ▼
   ┌──────────────────────────────────────────────────────────────────────────────────────────┐
   │  QA                                                                                      │
   ├──────────────────────────────────────────────────────────────────────────────────────────┤
   │  re-checks the work by a DIFFERENT method than the one that produced it                  │
   │                                                                                          │
   │  two exits, and no third:   evidence holds up → user, in user review                     │
   │                             evidence is weak  → back to to do, naming what failed        │
   │                                                                                          │
   │  may not: set done · archive                                                             │
   └────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                            │ hand-off: the verdict, the disputes,
                                            │           and what to look at first
                                            ▼
   ┌──────────────────────────────────────────────────────────────────────────────────────────┐
   │  USER        (you: a human, with no agent profile behind the name)                       │
   ├──────────────────────────────────────────────────────────────────────────────────────────┤
   │  the only actor that may set done or archived                                            │
   │  receives:  user review (the work is finished and wants a verdict)                       │
   │             question (the work cannot continue until a person decides)                   │
   │  returns:   done, or back to to do with feedback                                         │
   └──────────────────────────────────────────────────────────────────────────────────────────┘

   ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ outside the hand-off chain ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

   ┌──────────────────────────────────────────────────────────────────────────────────────────┐
   │  ESCALATOR   (not an agent: a watcher on a timer)                                        │
   ├──────────────────────────────────────────────────────────────────────────────────────────┤
   │  a card that is stuck, stalled or given up reaches a human ONCE, never as a stream       │
   │  a failure the machine caused costs the card none of its lives                           │
   └──────────────────────────────────────────────────────────────────────────────────────────┘
```

## What is this?

**hermes-workflows** is a generalized, portable foundation for orchestrating multiple
autonomous agent *profiles* — `orchestrator`, `coder`, `planner`, `qa-tester` — that
collaborate on real software tasks through a shared kanban board, a three-layer memory
system, a periodic dispatcher, and a host-side bridge for git and service operations.

It is built on the upstream [NousResearch **hermes-agent**](https://github.com/NousResearch/hermes-agent)
("the agent that grows with you").
You bring your own inference backend: any endpoint that speaks the Anthropic Messages or
OpenAI wire protocol. The shipped config templates default to a local backend
(llama-swap, llama.cpp, or any compatible proxy) — you point it at your own models.
Cloud providers (Anthropic, OpenAI, OpenRouter) are supported as primary or fallback
backends. See [docs/getting-started.md](docs/getting-started.md).

> **This is a proof-of-concept scaffold** — documentation, config templates, and the key
> moving scripts. You wire it to your own machine, your own model endpoint, and your own
> project. For a precise breakdown of what runs today versus what is reference-logic versus
> what is not built yet, see the [Status table](#status--whats-real-whats-reference-whats-not-built).

## Features

- **Multi-profile orchestration** — `orchestrator`, `coder`, `planner`, `qa-tester` roles,
  each with its own gateway, config, memory, and skill set.
- **Autonomous kanban loop (demonstrated, native)** — the built-in hermes-agent dispatcher
  claims `ready` tasks off the board, spawns a worker that executes in an isolated
  workspace, and writes a verdict back — `board → worker → verdict → done`, no human in
  the loop. Demonstrated end-to-end; driven by config keys, no custom code. See
  [`examples/autonomous-loop/`](examples/autonomous-loop/README.md).
- **Kanban workflow harness** — enforces a per-role transition matrix (coding → QA →
  user → done). Agents cannot complete their own tasks or reach the board
  through the CLI, sqlite or the dashboard; their only exit is `kanban_handoff`. See
  [docs/kanban-harness.md](docs/kanban-harness.md).
- **Three channels an agent learns from** — what belongs where, and what goes wrong
  when a rule is put in the wrong one: [the card](docs/context-card.md) is long-haul
  memory, [the system prompt](docs/context-prompt.md) is knowledge and outranks a
  brief, [the harness](docs/context-harness.md) is what is true right now.
- **Hand-over as a contract** — one form per transition, checked by the harness: the
  `Files` line is verified against what the run actually touched, and "could not
  check" is a first-class outcome. See [docs/handover.md](docs/handover.md).
- **Limits are tools, never words** — a profile's rights are its toolsets, so a
  planner that cannot edit or spawn is enforced by what it holds rather than by what
  it is told. See [docs/enforcement.md](docs/enforcement.md).
- **Retry once, then re-plan** — a card that exhausts its budget blocks on the first
  failure and produces a card for the planner carrying the failed run's hand-over
  verbatim; a busy model is a queue, never a failure. See
  [docs/splitting.md](docs/splitting.md).
- **Resilience — wait instead of failing, and always tell a human** — a worker that
  cannot get model capacity waits against a time budget (`agent.model_wait_budget`)
  instead of burning a retry count; machine-caused failures are kept off the card's
  circuit breaker (`kanban.count_toward_breaker`); and no card is ever left silently
  blocked, given up, or stranded — a human gets a message over Mattermost, Telegram,
  or ntfy with the card, the error, the resume command, and the worker log. See
  [docs/resilience.md](docs/resilience.md).
- **Optional custom dispatcher (reference logic)** — for when you outgrow the native one:
  per-profile cooldowns, fast-retry / failure-escalation windows, notify-only-on-change
  (see [`hooks/`](hooks/per-profile-dispatcher/handler.py)). Pure logic is real; backend
  adapters raise `NotImplementedError` and bind to the same `hermes kanban` commands.
- **Three-layer memory** — per-profile `MEMORY.md` (layer 1) and an Obsidian-style
  markdown vault (layer 2) are file-based and ready to use. Layer 3 (pgvector semantic
  store) ships as a bare container in the Docker stack; the ingest API and sync are not
  yet implemented — see [docs/memory.md](docs/memory.md).
- **Backend-agnostic inference** — point at any OpenAI- or Anthropic-compatible endpoint.
  The shipped templates default to a local proxy (llama-swap / llama.cpp), with cloud
  providers as optional primary or fallback backends.
- **Host bridge** — a dependency-free Python service for git-pull, log-tail, and
  service-restart operations that the agent should not perform from inside a sandbox.
  Build and deploy operations are present as stubs (see [`scripts/bridge/`](scripts/bridge/hermes-bridge.py)).
- **Skills whitelist** — an include-list keeps each profile's system prompt lean
  (see [docs/skills-whitelist.md](docs/skills-whitelist.md)).
- **Maintenance crons** — starter prompt templates for watchdogs, memory sync, board
  hygiene, and backups. These are template prompts, not a running schedule
  (see [cron/README.md](cron/README.md)).
- **Dual topologies** — native launchd + venv, or an optional Docker stack
  (see [docs/architecture/topologies.md](docs/architecture/topologies.md)).
- **Generic secure secrets stack** — choose a vault (**Secrets-Kit** macOS Keychain or
  **1Password `op`**, equal first-class options), inject at launch time (`seckit run` /
  `op run`), and keep the inference key out of the docker sandbox via
  `docker_forward_env: []`. Three tiers of isolation; both vaults fully supported.
  See [docs/secrets.md](docs/secrets.md).

## Architecture at a glance

Hermes talks to you through **channels** — a pluggable I/O layer. A channel is either a **chat channel** (two-way, human-in-the-loop messaging the agent listens and replies on) or a **notification sink** (one-way outbound push for alerts and run summaries). The gateway is channel-agnostic: platforms plug in by **config, not code**, and every channel connects *outbound* — no inbound port is opened on the host.

```
             ┌──────────────── channels (operator I/O) ─────────────────┐
             │  chat    ◀▶  Mattermost · Telegram · Matrix   (two-way)   │
             │  notify   ▶  ntfy · hermes send · webhook     (push-only) │
             └───────────────────────────┬──────────────────────────────┘
                                         ▼
                       ┌──────────────────────────┐
                       │   orchestrator profile   │  dispatch · crons · review
                       └────────────┬─────────────┘
                                    │  promotes todo→ready, 1 task per idle profile
            ┌───────────────────────┼───────────────────────┐
            ▼                       ▼                       ▼
      ┌──────────┐            ┌──────────┐            ┌────────────┐
      │  coder   │            │ planner  │            │ qa-tester  │
      └────┬─────┘            └────┬─────┘            └─────┬──────┘
           └──────────────┬───────┴─────────────┬──────────┘
                          ▼                     ▼
                 ┌──────────────────┐   ┌──────────────────┐
                 │  Hermes agent    │   │   host bridge    │  :9876
                 │  core (gateway)  │   │  git·log·restart │
                 └────────┬─────────┘   └──────────────────┘
                          │
      ┌───────────────────┼─────────────────────┬──────────────────┐
      ▼                   ▼                     ▼                  ▼
  kanban.db          LLM backend            web search        semantic memory
  state.db        (your endpoint)            SearXNG          pgvector store
  sessions/         <your model>              :8888              :8889
  skills/
  MEMORY.md
```

**Channel kinds.**

| Kind | Direction | Purpose | Plugs in via | Examples |
|---|---|---|---|---|
| **Chat channel** | two-way | Human-in-the-loop: send tasks, watch tool progress stream back, approve/deny, get replies in-thread | Built-in gateway adapter — per-profile `platform_toolsets` + a settings block; bot token in the secret/env layer | Mattermost, Telegram, Matrix (any webhook-capable chat) |
| **Notification sink** | outbound push | Fire-and-forget alerts, cron/watchdog summaries, "job done" pings — no reply loop | `hermes send` to a chat channel, or a small outbound bridge forwarding agent events to a push service | ntfy, generic webhook |

**Adding a channel** is a natural extension of the same abstraction:

- **Another chat channel Hermes supports** → pure config. Add its surface to the profile's `platform_toolsets` (e.g. `telegram: [hermes-telegram]`), add its settings block (`require_mention`, allow-list), put the token in the env/secret layer, and restart the gateway. No code. See the orchestrator overlay in [`config/profiles/orchestrator/`](config/profiles/orchestrator/config.yaml.example) and [docs/operating.md](docs/operating.md).
- **Another notification sink** → point the notify step at it (a webhook URL or CLI), or run a tiny outbound bridge beside the gateway that forwards agent events to the service (the ntfy pattern). The gateway core is unchanged.

> The scaffold ships **chat-channel** config templates (Telegram/Mattermost) as built-in adapters; **notification sinks** (ntfy, webhooks) are a documented extension pattern — an outbound sidecar — not a bundled adapter.

The diagram maps to the **native** topology; the same logical components run under the optional Docker stack, where a chat channel is a per-profile config change on the bind-mounted home and a notification sink is an extra sidecar service. See [docs/architecture/](docs/architecture/README.md) for the full breakdown.

## Quickstart

See **[docs/getting-started.md](docs/getting-started.md)** for the full onboarding
walkthrough: backend selection, provisioning the default local proxy backend, secrets setup
(vault + injection + sandbox), smoke test, and a dog-food workflow example.

```bash
# 1. Clone
git clone <your-fork-url> hermes-workflows && cd hermes-workflows

# 2. Copy config templates into your runtime home, then strip the ".example" marker
#    (it appears as both a suffix and an infix — the substitution handles both)
export HERMES_HOME=~/.hermes
mkdir -p "$HERMES_HOME"
cp -r config/. "$HERMES_HOME/"
find "$HERMES_HOME" -name "*.example*" | while read -r f; do mv "$f" "${f/.example/}"; done
find "$HERMES_HOME" -name "*.example*"   # verify: prints nothing

# 3. Create a venv and install the upstream NousResearch hermes-agent (pin a tag).
#    DO NOT pip install hermes-cli / hermes-agent from PyPI — wrong packages.
#
#    PYTHON VERSION: hermes-agent requires Python >=3.11,<3.14.
#    On macOS, the system `python3` may be 3.14+ and will FAIL the install.
#    Use python3.11 explicitly. Install it first if needed:
#      Homebrew: brew install python@3.11
#      pyenv:    pyenv install 3.11
python3.11 -m venv "$HERMES_HOME/venv"
"$HERMES_HOME/venv/bin/pip" install --upgrade pip
"$HERMES_HOME/venv/bin/pip" install "git+https://github.com/NousResearch/hermes-agent.git@v2026.6.19"
# Tags are DATE-BASED (e.g. v2026.6.19); find latest at the repo Releases page or via: git ls-remote --tags https://github.com/NousResearch/hermes-agent.git
# After install, the console scripts hermes, hermes-agent, and hermes-acp exist in the venv.

# 4. Pick a backend and provision secrets — see docs/getting-started.md
#    Default: a local llama-swap proxy at http://127.0.0.1:<PORT> (Anthropic Messages,
#    no /v1 — SDK appends it). A local proxy needs no API key.
#    For a cloud backend that requires a key:
#    Vault: Secrets-Kit (macOS) OR 1Password op — see docs/secrets.md
#    Inject at launch: seckit run --names LLM_API_KEY -- ./scripts/start.sh
#                   OR op run --env-file .env.op -- ./scripts/start.sh

# 5. Run the smoke test (Anthropic-Messages protocol against your backend)
LLM_BASE_URL=http://127.0.0.1:<PORT> LLM_MODEL=<your-model-alias> LLM_TRANSPORT=anthropic_messages \
  bash scripts/llm/llm-smoke-test.sh   # prints "ALL CHECKS PASSED" on success
# (LLM_PROTOCOL is accepted as an alias for LLM_TRANSPORT.)

# 6. Start the loop: ONE command registers the gateway (the motor) and the
#    escalator with launchd, reads each loaded job back, and names every job it
#    registered, left unchanged, reloaded or skipped. Safe to re-run.
python3 scripts/install.py            # --check to report only; --with backup / --with bridge
#    Docker instead: cd docker && cp .env.example .env && docker compose up -d
```

> **Docker networking note:** if your inference backend binds to loopback only, containers
> cannot reach it. Expose the backend on the docker bridge or use `host.docker.internal`.
> See [docker/README.md](docker/README.md).

## What Hermes gives you, and what this project adds

Two projects, one loop. If something is not running, this tells you whose it is.

**Hermes (upstream [hermes-agent](https://github.com/NousResearch/hermes-agent)) gives you:**
- **the board**: the kanban database, its cards, columns and CLI;
- **the dispatcher**: the loop that claims a ready card and starts a worker for it. It
  runs *inside the gateway process* when `kanban.dispatch_in_gateway: true`, ticks every
  60 seconds and holds `.dispatcher.lock`. There is no separate dispatcher daemon, and
  there must never be a second one: two processes on the same claim path race each other;
- **the workers**: the agent profiles that do the work;
- **the tools**: what an agent can call.

**This project adds:**
- **the transition gate** (`plugins/kanban-harness`): which moves an agent may make on the
  board, enforced as a refusal rather than a request;
- **the board spec** (`config/board.yaml`): the columns, roles and who may move what;
- **the hand-over form**: what a finished card must carry ([docs/handover.md](docs/handover.md));
- **the resilience watchers**: the escalator, which gives back lives the machine took and
  pages a person about stuck cards ([docs/resilience.md](docs/resilience.md));
- **`file_read`** (`plugins/file-read`): reading without the right to write;
- **the installer** (`scripts/install.py`): starts Hermes's gateway, and with it the
  dispatcher, under launchd. Hermes ships the motor; nothing starts it on its own.

If the board has ready cards and nothing moves, the motor is not running: run
`python3 scripts/install.py --check`.

## What you actually get when it runs

The first thing that works end-to-end, with only this repo and a backend key, is the LLM
smoke test. It is a real reachability + completion check against your endpoint:

```console
$ LLM_BASE_URL=http://127.0.0.1:8080 LLM_MODEL=qwen3-35b LLM_TRANSPORT=anthropic_messages \
    bash scripts/llm/llm-smoke-test.sh
[INFO] BASE_URL  : http://127.0.0.1:8080
[INFO] MODEL     : qwen3-35b
[INFO] TRANSPORT : anthropic_messages
[INFO] TIMEOUT   : 60s

[INFO] Test 1 — POST http://127.0.0.1:8080/v1/messages (Anthropic Messages — reachability + model check)
[PASS] Test 1 - Reachability: endpoint live (HTTP 200), model 'qwen3-35b' confirmed in response

[INFO] Test 2 — POST http://127.0.0.1:8080/v1/messages ("Reply with exactly the single word: PONG")
[INFO] Test 2 - Completion: elapsed 2s (fast)
[PASS] Test 2 - Completion: response contains PONG (content: 'PONG')

ALL CHECKS PASSED
```

That is a genuine round-trip to your configured model. From there you copy the config
templates into `~/.hermes`, install the upstream `hermes` CLI into the venv, and drive the
file-based pieces (`MEMORY.md` per profile, the markdown vault, the dispatcher reference
logic, the host bridge). The multi-profile gateway orchestration is wired through config
and launchd/Docker templates that you point at your own machine — see the honesty table
below for exactly which parts are live versus reference-only.

## Repository layout

| Path | Purpose |
|------|---------|
| `config/` | Portable Hermes config templates (config.yaml, profiles/, skills include-list) — copy into `HERMES_HOME`. |
| `docs/` | Documentation: architecture, deployment topologies, memory model, operations, secrets. |
| `docker/` | Optional Docker Compose stack (agent + web-search + semantic-memory + cloud-inference wrapper). |
| `scripts/` | Host helper scripts: health checks, the bridge, backups, memory sync, LLM smoke test. |
| `hooks/` | Agent lifecycle hooks — notably the dispatcher hook (reference logic). |
| `.githooks/` | Tracked git hooks — `pre-commit` runs CI's static checks locally before every commit. Enable with `git config core.hooksPath .githooks` (see [docs/testing.md](docs/testing.md)). |
| `cron/` | Scheduled-maintenance prompt templates (watchdogs, memory sync, board hygiene, backups). |
| `launchd/` | macOS launchd plist templates for the native gateway processes. |
| `vault/` | Obsidian-style markdown memory vault scaffold (Architecture/, Operations/, Research/, Project/, Meta/, Security/, Strategy/). |
| `patches/` | Optional patch-overlay templates for the upstream agent tree (advanced; see warnings). |
| `examples/` | Workflows — `examples/autonomous-loop/` (the board→worker→verdict loop, demonstrated end-to-end on a local backend), plus dog-food examples (`home-assistant-healthcheck/`, `gmail-processing/`). |
| `.github/` | Issue and PR templates, community health files. |

## Profiles

The scaffold defines four roles — `orchestrator`, `coder`, `planner`, `qa-tester` —
documented in [docs/profiles.md](docs/profiles.md). Each is a conceptual role; give them
names that make sense for your setup.

## Two things your board does, in plain words

- The harness always refuses an agent's forbidden move — finishing its own card, for example — and logs it to `~/.hermes/logs/kanban-harness.log`; there is no mode to turn that off.
- **`user_review`** is the column where finished, checked work waits for you. On the board it is a card assigned to `user`; underneath, it sits on a database status (`scheduled`) that no agent is ever started for, so nothing moves it until you run `board_cli.py accept <card>` (done) or `rework <card> --comment "<why>"` (back to the worker).

## Status — what's real, what's reference, what's not built

> **Proof of concept / scaffold.** One honest table, no overclaiming. "Runs" = works today
> with only this repo + a backend key. "Reference logic" = real code whose pure logic runs,
> but with backend adapters you must wire (some raise `NotImplementedError`). "Not built
> yet" = documented design target with no working implementation here.

| What runs | What is reference-logic | What is not built yet |
|-----------|-------------------------|-----------------------|
| Native autonomous kanban loop — `board→worker→verdict→done`, demonstrated end-to-end via the built-in dispatcher on a local backend (`examples/autonomous-loop/`, config keys only). To reproduce, point `HERMES_LOOP_BASE_URL` / `HERMES_LOOP_MODEL` at your own Anthropic-Messages endpoint (the proof config hardcodes the author's local backend) | Optional custom dispatcher: `todo→ready` promotion, stalled-worker reaping, one-task-per-idle-profile (`hooks/per-profile-dispatcher/`) — adapters raise `NotImplementedError`, for when you outgrow the native loop | Layer-3 semantic memory: pgvector ingest API + sync (container ships bare) |
| Config templates → `~/.hermes` (copy + rename) | Host bridge: git-pull / log-tail / restart logic present; build & deploy operations are stubs (`scripts/bridge/`) | The localhost control endpoint (needs the separate Hermes Desktop app — not in this repo) |
| Upstream `hermes` CLI install (venv, from git) | launchd plist + Docker Compose templates you point at your machine | A turnkey running multi-profile gateway (you wire profiles to your backend) |
| Layer-1 + Layer-2 memory: per-profile `MEMORY.md` + markdown vault (file-based) | Maintenance crons: prompt templates, not a running schedule (`cron/`) | Per-request credential isolation (key stays in-process today; needs egress proxy) |
| Skills include-list / whitelist | HA healthcheck dog-food example (`examples/home-assistant-healthcheck/`) | — |
| Escalator: a stuck, stalled or given-up card reaches a human once, over Mattermost / Telegram / ntfy (`scripts/resilience/`). **Open defect:** machine-caused failures are kept off the card's breaker only when the backend error reaches the board — a worker that gives up on a dead backend exits `1`, and that is still charged to the card ([docs/resilience.md](docs/resilience.md)) | Wait budget + error taxonomy (`agent.model_wait_budget`, `backend_busy`): real and tested, but the agent's retry loop does not consult them yet — that needs a patch to the installed agent ([docs/resilience.md](docs/resilience.md)) | — |
| Kanban harness: agents cannot finish their own card; hand-off with evidence is the only exit. With `config/board.yaml` deployed it also enforces the board's edges and its `requires` / `requires_to_leave` on every agent hand-off ([docs/kanban-harness.md](docs/kanban-harness.md)) | Board specification `config/board.yaml`: loaded, linted (zero warnings), and composed with the harness — but a policy layer over the runtime's statuses; the CLI and dashboard still show the raw status names, not these columns | **Specification only:** `scout` and `planner` roles — profile templates exist; no dispatch, hand-off or evidence rule enforces their contract yet |
| user_review and the only door to done: QA's pass waits for you on a status nothing dispatches from; `board_cli.py accept` (also `--children`) is the single way to done, `rework` sends it back with the reason ([docs/board-design.md](docs/board-design.md)) | — | Refusing a *human* who closes a card with the runtime's own `complete` — detected and paged instead, by design, since prevention would need a fork of the agent |
| Board moves no agent makes: a waiting card leaves by itself when what it waits on arrives, and finished cards are archived on a clock — opt-in, `escalator.py --board-file` | — | — |

See [`docs/`](docs/architecture/README.md) for the per-component breakdown.

## Built on

[NousResearch hermes-agent](https://github.com/NousResearch/hermes-agent) — the upstream
agent runtime this scaffold targets. Install it into your venv with:

```bash
# hermes-agent requires Python >=3.11,<3.14.
# Your system python3 may be 3.14+ (check: python3 --version).
# Use python3.11 to create the venv; install it first if needed (brew install python@3.11).
python3.11 -m venv "$HERMES_HOME/venv"
"$HERMES_HOME/venv/bin/pip" install "git+https://github.com/NousResearch/hermes-agent.git@v2026.6.19"
# Tags are DATE-BASED (e.g. v2026.6.19); find latest at the repo Releases page or via: git ls-remote --tags https://github.com/NousResearch/hermes-agent.git
```

After install the console scripts `hermes`, `hermes-agent`, and `hermes-acp` exist in the
venv. Pin to a tag (replace `v2026.6.19` with a real tag from the repo's Releases) for
reproducible installs. The tag `v2026.6.19` installs clean on Python 3.11 (hermes-agent
0.17.0, verified in a clean venv). **Do not** `pip install hermes-cli` / `hermes-agent`
from PyPI — those are unrelated third-party packages, not the NousResearch agent.

## Contributing

Contributions that flesh out the scaffold are especially welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) and our [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © 2026 the hermes-workflows contributors.
