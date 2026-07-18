# tooling-steering

A Hermes **plugin** that makes the agent *reliably* use its own tools for HTTP API calls and
MCP setup tasks — instead of asking the user to paste curl commands or copy-paste credentials.

## Architecture (one paragraph)

When a user asks the agent to hit an endpoint, run a curl, or set up an MCP integration, the
agent sometimes deflects and asks the user to do the mechanical step themselves. A persona line
alone is too fragile on a small local model to fix this reliably. This plugin replaces "prompt
and pray" with a deterministic mechanism. It registers a **`pre_llm_call`** hook (fires once per
turn, before the tool loop). The callback classifies the user's message via `lib/tooling_intent.py`;
if it signals an HTTP API / backend call or MCP setup (curl, HTTP verbs, endpoint paths, bearer
tokens, REST/GraphQL, MCP, Composio, Smithery, etc.), it returns `{"context": <instruction>}`.
Hermes injects that text into the *current turn's user message* — the freshest, most-followed
position — telling the agent to use the `terminal` tool with curl (or the `web` tool) and complete
the task end-to-end itself, without driving the user's visible browser. The injection is
**ephemeral** (never persisted, never in the cached system-prompt prefix). Direct coding / file /
chat messages are classified `None` and left untouched.

## Files

| File | Role |
|---|---|
| `plugin.yaml` | Manifest (declares the `pre_llm_call` hook). |
| `__init__.py` | Thin wiring: `register(ctx)` + the hook callback. |
| `lib/tooling_intent.py` | Single source of truth: `classify()` + `build_steer()`. Self-tests via `python3 lib/tooling_intent.py`. |
| `README.md` | This file. |

## Verify

```bash
python3 plugins/tooling-steering/lib/tooling_intent.py    # classifier self-test (expect 15/15)
hermes -p <profile> plugins list | grep tooling-steering  # expect: enabled · user
```

## Why a plugin, not just a persona line

The persona is always-on but competes with everything else in the system prompt and is easy for
a small model to ignore under task pressure. This plugin carries the *per-turn, just-in-time*
tool-discipline instruction, which fires only when the message actually signals an API / MCP task —
keeping the prompt clean for all other turns.
