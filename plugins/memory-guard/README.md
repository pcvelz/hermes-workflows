# memory-guard

Hermes plugin that blocks the agent from persisting secrets (passwords, tokens, API keys, private keys) into persistent memory (`MEMORY.md` / `USER.md`).

## Architecture

Hermes' `memory` tool writes durable entries that survive across sessions and are injected into every future turn's context. If the agent receives a credential mid-session it can proactively call `memory add` — permanently leaking the secret into every subsequent session.

This plugin is the Hermes analog of Claude Code's `block-memory-write.sh` PreToolUse hook. It registers a `pre_tool_call` hook that fires on every tool call. For the `memory` tool with a write-ish action (`add` or `replace`), it runs a pattern-based secret detector over the `content` argument. If a secret is found, the hook returns `{"action": "block", "message": "…"}` — the Hermes equivalent of a CC deny — and the write never reaches disk. All other calls (non-memory tools, `remove`, clean content) receive `None` (allow).

**Fail-open by design:** any exception inside the hook catches silently and returns `None`, so a plugin bug can never break a legitimate memory write.

**Detection scope (v1 — secrets only, not general PII):**
- Secret-named env-var values present in the text (e.g. the live value of `GMAIL_PASSWORD`)
- `key=value` / `key: value` credential forms (`password`, `passwd`, `secret`, `token`, `api_key`, `access_key`, `auth_token`, `client_secret`, `private_key`, `oauth_token`)
- CLI flag forms (`--password=X`, `--token X`)
- `Authorization: Bearer/Basic <value>` headers
- AWS access key IDs (`AKIA…`)
- URL-embedded credentials (`scheme://user:pass@host`)
- PEM private-key block headers (`-----BEGIN … PRIVATE KEY-----`)

## Files

| File | Purpose |
|---|---|
| `plugin.yaml` | Plugin manifest (name, version, hooks declaration) |
| `__init__.py` | `register(ctx)` entry point + `_on_pre_tool_call` hook + `_contains_secret` detector + self-test |
| `README.md` | This file |

## Verify

Run the self-test (no Hermes runtime needed):

```bash
python3 plugins/memory-guard/__init__.py     # expect: all six cases PASS
hermes -p <profile> plugins list | grep memory-guard   # expect: enabled · user
```
