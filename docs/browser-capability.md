# Browser Capability for Agents

> How the `qa-tester` role gets a real, interactive browser — not just text search — and why
> that browser is a persistent, supervised process rather than something spawned per task.

This scaffold's first real end-to-end task run (see
[docs/virgin-voyage-results.md](virgin-voyage-results.md)) needed a worker that could load an
actual page, wait for a dev server, click through a flow, and verify what it saw — not just
read text snippets about a topic. This page documents the candidates that were on the table,
which one was selected, and the portable how-to for standing it up yourself.

---

## Search vs. browser — two different capabilities

It's easy to conflate "the agent can use the internet" into one bucket. This scaffold treats
it as two genuinely different tools with no overlap:

| | Web search (SearXNG) | Browser (this page) |
|---|---|---|
| Answers | "What's out there about X?" | "What does this exact page/app do right now?" |
| Output | Links + text snippets | A real rendered page: DOM, JS-executed state, screenshots |
| Can it click a button, fill a form, or run your app's login flow? | No | Yes |
| Can it verify your own dev server / localhost app? | No | Yes |
| Docker/topology status in this scaffold | Docker-only tool service (`:8888`) | Toolset ships with the agent itself; see below for the always-on Chrome it talks to |

See [docker/services/searxng.md](../docker/services/searxng.md) for the search side. The rest
of this page is the browser side — they are complementary, not substitutes for each other, and
a `qa-tester` task will typically want both (search to find your own app's acceptance criteria
or a library's docs, browser to actually drive the verification).

---

## The candidate matrix

Five candidates were on the table. Only one was adopted as the default; the rest are recorded
here so you don't have to re-derive the tradeoffs.

| Candidate | What it is | Verdict |
|---|---|---|
| **Built-in browser toolset + persistent CDP Chrome** | Hermes' own `browser` toolset (Playwright-backed — see the `browser:` block in [`config/profiles/qa-tester/config.yaml.example`](../config/profiles/qa-tester/config.yaml.example)), pointed at an always-on headless **Chrome-for-Testing** process over the Chrome DevTools Protocol (CDP) instead of launching/killing a fresh browser per task. | **Selected.** No new dependency — the toolset is already what every profile config in this repo assumes. Connecting it to a long-lived CDP endpoint avoids paying Chrome's cold-launch cost on every task and lets cookies/session/login state survive across a multi-step QA run. See [The persistent-CDP-chrome pattern](#the-persistent-cdp-chrome-pattern) below. |
| **Camoufox** | A hardened Firefox fork purpose-built to defeat headless-browser fingerprinting (patches `navigator.webdriver`, canvas/WebGL fingerprint surfaces, automation-detection quirks, etc.). | **Escalation path, not the default.** It has **no official Docker image** — only community-maintained ones — which makes it an unverified supply-chain dependency for a default install. Reach for it only once you've actually hit an anti-bot wall with the built-in toolset; see [Anti-bot note](#anti-bot-note-headless-chrome-is-fingerprintable) below. |
| **SearXNG** | Self-hosted text metasearch aggregating multiple upstream search engines. Already documented — see [docker/services/searxng.md](../docker/services/searxng.md). | **Complementary, not a substitute.** It is not a browser at all: no page rendering, no JS execution, no clicking. Keep both — search for research, browser for interaction/verification. |
| **Self-hosted Firecrawl** | An open-source "web scraping/crawling for LLMs" service — turns pages into clean markdown or structured data, with an optional headless-browser rendering mode for JS-heavy pages. | **Heavier than this job needs.** Ships as its own multi-service compose stack (API + worker(s) + a headless-browser service + a queue + a database). A reasonable choice if your workflow is bulk content extraction at scale; disproportionate for "let `qa-tester` click through one flow and report pass/fail." Not adopted because of that shape, not because it doesn't work. |
| **Generic headless-Chromium-in-Docker** | Roll-your-own container running plain Chromium with `--headless --remote-debugging-port`, no packaged image. | **Viable — it's effectively the selected pattern, containerized.** This is the same persistent-CDP approach described below, just without an off-the-shelf image to start from. If you're on the Docker topology, this is the shape to build (a `browser` service in your compose file reachable by service name) rather than trying to reach a host-loopback CDP port — see [the reachability gotcha](#the-native-vs-container-reachability-gotcha). |

---

## The persistent-CDP-chrome pattern

The core idea: run **one** headless Chrome as a long-lived, supervised background process —
the same shape as `llama-swap` for the model or the host bridge for privileged operations — and
have the browser toolset **connect** to it over CDP rather than launching a fresh browser per
task.

### 1. Resolve the browser binary

Don't hardcode a path — Playwright manages its own downloaded browser builds under a cache
directory that is versioned and will move when Playwright itself is upgraded
(`~/Library/Caches/ms-playwright/` on macOS, `~/.cache/ms-playwright/` on Linux). Resolve the
real path programmatically instead:

```bash
python3 -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print(p.chromium.executable_path)
"
```

If your hermes-agent install's venv already has Playwright as a dependency (it does, if the
`browser` toolset is enabled), run this against that venv's interpreter so you get the exact
binary the agent itself would use.

> Playwright's default bundled `chromium` build and Google's separately-distributed "Chrome for
> Testing" binary are close but not guaranteed identical across every Playwright release. If you
> specifically need the branded Chrome for Testing binary, install it explicitly via
> `playwright install chrome` (the `chrome` channel) rather than the default `chromium` — consult
> your installed Playwright version's own documentation for how to resolve that channel's
> executable path, since the property shown above resolves the default `chromium` build.

### 2. Launch it headless, pinned to a stable debugging port

```bash
CHROME_BIN="$(python3 -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()')"

"$CHROME_BIN" \
  --headless=new \
  --remote-debugging-port=9222 \
  --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$HOME/.hermes/browser-profile" \
  --no-first-run \
  --no-default-browser-check \
  --disable-gpu
```

- `--remote-debugging-port=9222` is the reference port (matches the port-map convention in
  [docs/architecture/ports.md](architecture/ports.md)); any free loopback port works.
- `--remote-debugging-address=127.0.0.1` makes the loopback-only bind explicit rather than
  relying on the default — be explicit here, this is the flag the
  [reachability gotcha](#the-native-vs-container-reachability-gotcha) and the
  [security note](#security-note-cdp-has-no-authentication) both depend on.
- `--user-data-dir` gives the persistent process a durable profile directory, so cookies and
  login sessions survive both individual tasks and process restarts. Point it wherever your
  runtime home lives — this example uses the same `~/.hermes` convention as the rest of this
  scaffold.

### 3. Supervise it so it survives reboots

Treat it exactly like the gateway or the host bridge: a small always-on service, not a
foreground terminal command. On the native (macOS/launchd) reference topology, follow the same
`RunAtLoad` + `KeepAlive` shape as the shipped
[`launchd/com.hermes-workflows.gateway.plist.example`](../launchd/com.hermes-workflows.gateway.plist.example):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.hermes.browser-cdp</string>
    <key>ProgramArguments</key>
    <array>
        <string><CHROME_BIN></string>
        <string>--headless=new</string>
        <string>--remote-debugging-port=9222</string>
        <string>--remote-debugging-address=127.0.0.1</string>
        <string>--user-data-dir=<HERMES_HOME>/browser-profile</string>
        <string>--no-first-run</string>
        <string>--no-default-browser-check</string>
        <string>--disable-gpu</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string><HERMES_HOME>/logs/browser-cdp.log</string>
    <key>StandardErrorPath</key>
    <string><HERMES_HOME>/logs/browser-cdp.err</string>
</dict>
</plist>
```

On Linux, the portable equivalent is a `systemd --user` unit (`Restart=always`, enabled via
`systemctl --user enable --now hermes-browser-cdp.service`) — same supervision contract, no
launchd dependency.

### 4. Point the profile at it — `browser.cdp_url`

Add a `cdp_url` key alongside the existing `browser:` block on the `qa-tester` profile overlay
(see [`config/profiles/qa-tester/config.yaml.example`](../config/profiles/qa-tester/config.yaml.example)):

```yaml
browser:
  engine: auto                # Playwright selects the available browser engine.
  cdp_url: http://127.0.0.1:9222   # Connect to the persistent Chrome instead of launching one.
  inactivity_timeout: 120
  command_timeout: 60
  allow_private_urls: true
  record_sessions: false
```

When `cdp_url` is set, the toolset **connects** to the already-running browser instead of
spawning and tearing one down per task — the whole point of this pattern.

> Confirm the exact key name and connect-vs-launch precedence against your installed
> hermes-agent version before relying on it for anything unattended, in the same spirit as the
> config-key caveats elsewhere in this scaffold's docs (see, e.g.,
> [docs/task-authoring.md](task-authoring.md)'s `--goal` caveat).

### 5. Verify it's alive

A live CDP endpoint answers `/json/version` with the browser's identity and a
`webSocketDebuggerUrl`:

```bash
curl -fsS http://127.0.0.1:9222/json/version
```

Expected shape (abbreviated):

```json
{
  "Browser": "Chrome/1xx.0.0.0",
  "Protocol-Version": "1.3",
  "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/<uuid>"
}
```

No response (connection refused) means the supervised process isn't running or isn't bound
where you expect — check the supervisor's log path before touching the profile config.

---

## The native-vs-container reachability gotcha

This is the same class of problem as the <model> Docker-bind gotcha documented throughout this
repo (see [docs/architecture/ports.md](architecture/ports.md) and
[docs/architecture/topologies.md](architecture/topologies.md)) — and it is easy to hit blind,
because a `curl http://127.0.0.1:9222/json/version` run **on the host** will succeed right up
until the agent itself is the thing making the request from inside a container.

**The rule:** `browser.cdp_url` must resolve from wherever the *agent process* actually runs,
not from wherever you happened to run the verification curl.

- **Native topology (reference):** the agent and the persistent Chrome share the same host
  loopback — `http://127.0.0.1:9222` just works, no extra step.
- **Docker topology:** a container is in its own network namespace and **cannot** reach a host
  `127.0.0.1` port, CDP included — the exact same failure mode as `llama-swap` on `:8001` (see
  [docker/README.md](../docker/README.md)'s "REQUIRED: make <model> reachable from containers").
  You have two sound options, and one option to avoid:
  1. **Run the browser as a container/sidecar on the same compose network**, reachable by
     service name (e.g. `http://browser:9222`) — the cleanest fix, and the natural extension of
     the "generic headless-Chromium-in-Docker" candidate from the matrix above. No host port is
     ever exposed.
  2. **Tunnel** the host's `127.0.0.1:9222` into the container network (`socat`, SSH
     local-forward, or Docker's `host-gateway` mechanism) without rebinding the browser itself.
  3. **Do not** rebind the browser to `0.0.0.0:9222` and reach it via `host.docker.internal`,
     the way this scaffold's docs suggest as an *acceptable-with-a-firewall-rule* option for
     <model>. See the next section for why CDP does not get the same "acceptable with mitigation"
     treatment.

---

## Security note: CDP has no authentication

llama-swap's `:8001` at least requires a (non-validated, but present) bearer token in most
client libraries, and the host bridge can be put behind `HERMES_BRIDGE_TOKEN`. **The Chrome
DevTools Protocol has no authentication mechanism of any kind.** Anyone who can open a TCP
connection to a CDP port has full remote control of that browser: reading cookies, executing
arbitrary JavaScript in any open tab, and screenshotting whatever authenticated session happens
to be loaded.

Practical consequence: treat a `0.0.0.0`-bound CDP port as strictly worse than the already-flagged
<model> LAN-exposure caveat, not as "the same tradeoff." Keep it loopback-bound
(`--remote-debugging-address=127.0.0.1`) always, and reach it from a container via the sidecar
or tunnel options above — never a raw rebind. See [SECURITY.md](../SECURITY.md) for this
scaffold's general network-exposure posture.

---

## Anti-bot note: headless Chrome is fingerprintable

Even a well-behaved persistent headless Chrome carries automation fingerprints that commercial
bot-defense products (Cloudflare, DataDome, PerimeterX/Akamai, and similar) actively look for —
a `navigator.webdriver` flag set by CDP automation, headless-specific rendering/timing quirks,
inconsistent plugin/mimeType lists, and CDP-specific JS-runtime artifacts. A site with active
bot-defense may challenge or outright block requests that carry these tells, independent of
anything your agent's task logic does wrong.

**This is exactly the case the candidate matrix's Camoufox row exists for.** If a `qa-tester`
task starts failing specifically against a third-party site (not your own app under test) with
CAPTCHA walls or silent blocks, that's the signal to evaluate the escalation, not to add more
retries. Re-read the Camoufox row above before adopting it — the "no official Docker image"
caveat is real and means you're taking on a community-maintained dependency, so reach for it
deliberately rather than by default.

---

## Resource cost

A persistent headless Chrome sitting idle with a blank/parked tab costs roughly **300–500 MB**
of resident memory — paid continuously, whether or not `qa-tester` is actively working, in
exchange for skipping Chrome's own multi-second cold-launch cost on every single task and
keeping session/cookie state alive across a multi-step test run.

This is the same always-on-service-vs-per-task-cost tradeoff this scaffold already makes for
<model> (a resident model paying idle VRAM to avoid a ~55 s cold start per request — see
[docs/backend.md](backend.md)). If memory is tight on your host, weigh whether a per-task
launch (accept the startup latency, skip the idle cost) suits your workload better; nothing
about the `browser` toolset requires the persistent-CDP pattern, it is simply what this
scaffold's virgin voyage settled on.

---

## See also

- [docs/virgin-voyage-results.md](virgin-voyage-results.md) — the end-to-end run that drove this decision
- [docs/roles.md](roles.md) — why `qa-tester` is a real, separately-provisioned profile rather than folded into another one
- [docs/profiles.md](profiles.md) — the `qa-tester` role definition and its toolset
- [docs/architecture/ports.md](architecture/ports.md) — the authoritative port map, including `:9222`
- [docker/services/searxng.md](../docker/services/searxng.md) — the web-search side of "internet access"
- [config/profiles/qa-tester/config.yaml.example](../config/profiles/qa-tester/config.yaml.example) — the profile overlay this page extends
- [SECURITY.md](../SECURITY.md) — network-exposure posture for every service in this scaffold
