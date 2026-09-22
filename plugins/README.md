# plugins/ — Hermes agent-governance layer

Small, self-contained plugins that extend the Hermes agent loop with **deterministic**
governance: a permission gate, audit logging, a secret-into-memory block, and per-turn
steering. They are the Hermes analog of Claude Code's `settings.json` + hook chain — soft
persona/skill prose is guidance only and a small local model ignores it under pressure, so
anything you need to *enforce* or *gate* belongs here, in code that fires on a hook.

## The hook lifecycle

Each plugin is a directory with a manifest (`plugin.yaml`) and an entrypoint module
(`__init__.py`) whose `register(ctx)` wires one or more hooks. Three hook points are used:

| Hook | Fires | CC equivalent | Return contract |
|---|---|---|---|
| `pre_tool_call` | before every tool call | `PreToolUse` | `{"action":"block","message":…}` to block, else `None` |
| `post_tool_call` | after every tool call | `PostToolUse` | `None` (observational) |
| `pre_llm_call` | once per turn, before the LLM | `UserPromptSubmit` | `{"context":…}` to inject text into the current turn, else `None` |

A `pre_tool_call` hook returning a block dict is the analog of CC's `PreToolUse` exit-2 —
the tool never runs. A `pre_llm_call` hook returning `{"context":…}` injects that string
into the **current turn's user message** (the freshest, cache-safe, ephemeral slot — it
never touches the cached system-prompt prefix and is never persisted).

## The plugins

| Plugin | Hook | What it does |
|---|---|---|
| [`policy-gate`](policy-gate/) | `pre_tool_call` | Deny/ask permission rules + a filesystem "allowed roots" jail. Self-contained policy file; ships in **warn** mode. |
| [`kanban-harness`](kanban-harness/) | `pre_tool_call` + tool | Enforces a per-board, per-role kanban transition table (default: coding → QA → human → done): refuses every agent move without a row, including side doors, and provides `kanban_handoff`. **Fail-closed** for kanban mutations. See [docs/kanban-harness.md](../docs/kanban-harness.md). |
| [`file-read`](file-read/) | tool | The `file_read` toolset: open, list, search — no write path. Refuses scripts/executables and any op it cannot classify. Grant instead of `file` to a role that must not edit. |
| [`audit-log`](audit-log/) | `post_tool_call` | Appends every executed command / file-edit to a greppable, **secret-scrubbed** log. |
| [`memory-guard`](memory-guard/) | `pre_tool_call` | Blocks the agent from persisting secrets (passwords, tokens, keys) into durable memory. |
| [`prompt-log`](prompt-log/) | `pre_llm_call` | Appends every user prompt (secret-scrubbed) to an audit log. |
| [`skill-router`](skill-router/) | `pre_llm_call` | Injects a matching skill's `router_directive` into the turn so a small local model actually follows it. |
| [`tooling-steering`](tooling-steering/) | `pre_llm_call` | For HTTP-API / MCP-setup requests, injects a tool-discipline instruction so the agent runs the call itself. |

Every plugin except `kanban-harness` and `file-read` (which refuses what it cannot classify) is **fail-open**: any error in a hook returns `None` (allow), so a plugin bug
can never break a legitimate tool call or turn. Each is independently reviewable — no shared
library is factored out, on purpose (self-contained is the security-review-friendly choice).

## Trust note

These plugins run **in-process** inside the agent and can see tool arguments, the user
message, and environment variables (the log scrubbers read `os.environ` to redact secret
values). Review a plugin before enabling it, and keep your real policy file
(`policy-gate/settings.hermes.json`) — which may name real paths — out of git (it is
gitignored; only the `.example` template is committed).

## Enabling a plugin

Hermes plugin homes are **profile-aware**. Symlink the plugin dir into every profile's plugin
home, then opt in per profile via the `plugins.enabled` list in that profile's config:

```sh
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SRC="$(pwd)/plugins/policy-gate"

# top-level (default profile) + one worker profile:
ln -sfn "$SRC" "$HERMES_HOME/plugins/policy-gate"
ln -sfn "$SRC" "$HERMES_HOME/profiles/<profile>/plugins/policy-gate"

# then add to that profile's config.yaml:
#   plugins:
#     enabled:
#       - policy-gate

hermes -p <profile> plugins list   # expect: policy-gate  enabled · user
```

Each plugin's `__init__.py` (and any `lib/*.py`) has a runnable self-test — run it directly
(`python3 plugins/<name>/__init__.py`) to validate behaviour without a live Hermes runtime.
