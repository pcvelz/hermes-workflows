# Testing the hermes-workflows scaffold

This repo ships a three-layer test harness. Every layer is **portable**
(macOS + Linux, no machine-specific paths) and every endpoint, venv, and home
directory is overridable via environment variables. Run everything through the
single entry point:

```bash
bash scripts/test.sh static   # offline syntax/validity checks
bash scripts/test.sh smoke    # LLM backend + bridge + dispatcher logic
bash scripts/test.sh e2e      # GATED isolated one-shot (needs HERMES_WF_E2E=1)
bash scripts/test.sh all      # all three (e2e self-gates)
```

Output is a stream of `[PASS]` / `[FAIL]` / `[SKIP]` lines plus a grand summary.
**Exit status is non-zero if any check FAILs.** Missing *optional* tools
(`docker`, `plutil`, `jq`, `PyYAML`) are reported as `[SKIP]`, never `[FAIL]`,
so the suite is green on a minimal box.

---

## Layer 1 — `static` (offline, always safe)

`tests/static/run.sh`. No network, no model, no live install touched. Proves
every committed artifact is syntactically valid and internally consistent:

| # | Check | Tool | Proves |
|---|-------|------|--------|
| 1 | `bash -n` on every `*.sh` / `*.example.sh` | bash | no shell syntax errors |
| 2 | `python -m py_compile` on every `*.py` | python | no python syntax errors |
| 3 | import `hooks/per-profile-dispatcher/handler.py` | python | the dispatcher module loads and exports its pure functions |
| 4 | `yaml.safe_load_all` on every `*.yaml` / `*.yaml.example` | PyYAML | config templates parse |
| 5 | `json.load` on `cron/jobs.json.example` (and any `*.json`) | python | cron job defs are valid JSON |
| 6 | `plutil -lint` on launchd plists | plutil | plists are well-formed (**SKIP on Linux / no plutil**) |
| 7 | `docker compose config -q` on `docker/docker-compose.yml` | docker | compose file is valid (**SKIP if docker absent**) |
| 8 | relative-markdown-link existence check across all docs | python | no broken `[text](path)` links between docs |

**What it does NOT prove:** that the model answers, that the bridge boots, or
that the agent runs. Those are the smoke and e2e layers.

---

## Layer 2 — `smoke` (live but non-destructive)

Three independent checks. None of them write to `~/.hermes` or signal the live
gateway.

### 2a. LLM backend reachability — `tests/smoke/llm.sh`

**Default path (`LLM_TRANSPORT=anthropic_messages`, the Anthropic Messages protocol):**

1. **Reachability + model check** — `POST ${LLM_BASE_URL}/v1/messages` with
   `x-api-key` and `anthropic-version` headers, confirming the endpoint is live
   and echoes your model id back in the response body. (The Anthropic Messages
   protocol has **no** `/v1/models` listing endpoint — reachability is proven by
   a real tiny request, not a model list.)
2. **Completion** — a second `POST ${LLM_BASE_URL}/v1/messages` with
   `"Reply with exactly the single word: PONG"`, asserting `PONG` is in the
   response.

**Alternate path (`LLM_TRANSPORT=openai`):**

1. `GET ${LLM_BASE_URL}/models` — endpoint up, exact model id listed.
2. `POST ${LLM_BASE_URL}/chat/completions` with
   `"Reply with exactly the single word: PONG"`, asserting `PONG` is in the
   response.

Read timeout defaults to **60 s** (raise via `LLM_TIMEOUT` for cold-start
backends). If the endpoint is **unreachable** the check **SKIPs** (model
offline is an environment condition, not a repo defect). If the endpoint
answers but generation fails, that is a **FAIL**.

Overrides: `LLM_BASE_URL` (default `http://127.0.0.1:<PORT>`),
`LLM_MODEL` (default `<your-model-alias>`), `LLM_TRANSPORT` (default `anthropic_messages`),
`LLM_API_KEY` (default `$LLM_API_KEY` → `local`), `LLM_TIMEOUT` (default `60`).

### 2b. bridge `/health` — `tests/smoke/bridge.sh`
1. Allocates a free ephemeral `127.0.0.1` port (via python `bind(0)`).
2. Starts `scripts/bridge/hermes-bridge.py` with **`HERMES_HOME` pointed at a
   fresh temp dir** so its log file never lands in `~/.hermes`.
3. Polls `GET /health` until HTTP 200 (or ~10 s timeout).
4. Asserts the JSON body reports `status=ok` and `service=hermes-bridge`.
5. **Always** stops the bridge process and removes the temp home (trap EXIT).

Proves the bridge boots and serves its no-auth liveness probe. It binds only
loopback on an unprivileged port and runs against an isolated home.

### 2c. dispatcher pure-function unit tests — `tests/smoke/dispatcher.sh`
Runs `tests/smoke/test_dispatcher.py`, which imports
`hooks/per-profile-dispatcher/handler.py` and asserts the I/O-free logic
behaves on synthetic inputs:

- **promote-when-deps-resolve** (`deps_resolved`): all-deps-DONE promotes;
  one-not-done or a dangling reference does not.
- **stale-worker detection** (`worker_is_stale`): heartbeat / startup / runtime
  windows, plus check priority (heartbeat beats runtime).
- **cooldown math** (`cooldown_remaining`): normal (10 m) vs. fast-retry (2 m)
  vs. after-3-failures (30 m), and old-failure fallback to normal.
- **idle profiles / task picking** (`idle_profiles`, `pick_task_for`):
  busy-exclusion, order preservation, assignee-preference FIFO.
- **state fingerprint** (`state_fingerprint`): order-insensitivity, change on
  task-state change, change on worker change, stable 32-hex output.

The backend adapter stubs (`fetch_tasks`, `dispatch_task`, …) are **never**
called — they raise `NotImplementedError` by design — so there is zero risk to
the live install.

---

## Layer 3 — `e2e` (gated, hard-isolated)

`tests/e2e/run.sh`. The only layer that runs the **real** hermes-agent binary.
It is **double-gated and hard-isolated**:

- **Gate:** runs ONLY when `HERMES_WF_E2E=1`. Otherwise it SKIPs and exits 0.
- **Isolation:** a fresh temp `HERMES_HOME` under `$TMPDIR`. The script resolves
  both that path and the real `~/.hermes` to absolute, symlink-free paths and
  **hard-refuses (exit 3)** if the temp home would resolve to — or inside —
  `~/.hermes`, or if it is not under a recognized temp root.
- **Venv reuse (read-only):** it reuses the real hermes-agent venv via
  `HERMES_AGENT_VENV` (default `~/.hermes/hermes-agent/venv`) — it only
  *executes* the interpreter, it never writes into the venv.
- **Config:** copies the coder profile template into the temp home as
  `config.yaml` (renamed from `.example`), substituting the model name and
  `base_url` from `LLM_BASE_URL`.
- **One-shot:** runs exactly one non-interactive query
  (`hermes chat -q "<prompt>" -Q -m <model> --ignore-rules`) instructing the
  model to reply with the token `E2E_OK`, and asserts the output contains it.
- **Cleanup:** the temp home is **always** removed (trap EXIT/INT/TERM).
- **Timeout:** the one-shot is wrapped in a hard ~180 s ceiling
  (`timeout`/`gtimeout`, with a pure-bash watchdog fallback).

It does **not** use `--replace`, does **not** start a gateway, does **not**
touch launchd, and never reads or writes the live `kanban.db` / `state.db` /
`config.yaml`.

Overrides: `HERMES_WF_E2E` (gate), `HERMES_AGENT_VENV`, `LLM_MODEL`,
`LLM_BASE_URL_HOST` (your backend base URL),
`HERMES_WF_E2E_TIMEOUT` (default `180`).

```bash
HERMES_WF_E2E=1 bash scripts/test.sh e2e
```

---

## Isolation & safety model

| Concern | Guarantee |
|---------|-----------|
| Live `~/.hermes` writes | Never. Smoke-bridge and e2e both override `HERMES_HOME` to a temp dir; e2e hard-refuses if that path resolves to `~/.hermes`. |
| Live gateway process | Never signalled. No `launchctl`, no `--replace`, no gateway start in any test. |
| Live `kanban.db` / `state.db` / `config.yaml` | Never read or written. The dispatcher unit tests use synthetic objects; e2e seeds its own throwaway `config.yaml`. |
| hermes-agent venv | Reused **read-only** (interpreter execution only), overridable via `HERMES_AGENT_VENV`. |
| Portability | No `/Users/*` paths. Repo root derived from script location; endpoints/venv/home all env-overridable. |
| Temp cleanup | `trap EXIT/INT/TERM` removes every temp dir, with a path-pattern guard before any `rm -rf`. |

---

## CI notes

- **static** and **smoke (dispatcher + bridge)** run on any runner with
  python3 + bash + curl. They need no GPU and no model — safe for GitHub
  Actions. (The LLM backend sub-check SKIPs when the endpoint is unreachable, so a
  cloud CI runner stays green.)
- **e2e** requires your LLM backend and the hermes-agent venv, so it is
  intended for a **self-hosted runner** on the machine that can reach the backend.
  Keep it gated (`HERMES_WF_E2E=1`) so it never runs accidentally in a hosted CI
  job that lacks the backend.
- Suggested gate: run `bash scripts/test.sh static` and the offline smoke checks
  on every PR; run `HERMES_WF_E2E=1 bash scripts/test.sh all` on a nightly
  self-hosted schedule.
- Optional tools missing (`docker`, `plutil`, `jq`, `PyYAML`) downgrade their
  checks to SKIP, so the exit code reflects only genuine failures.
- **GitHub Actions:** `.github/workflows/ci.yml` runs `bash scripts/test.sh static` on every push and pull_request to `main`; smoke and e2e are local-only (they require your LLM backend and hermes-agent runtime).
