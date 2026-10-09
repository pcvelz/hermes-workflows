# composio-gmail-mcp

Wires a **read-only** Composio Gmail MCP server into a Hermes profile, so the
agent can retrieve mail (fetch, list, get; never send, modify or delete). This
is a one-time wiring tool, not a launchd service. Any profile can be wired.

## Architecture

Composio serves each MCP server as a per-server endpoint:
`https://<host>/v3/mcp/<server-id>?user_id=<user-id>`. The server is created
with an allowlist of the 7 read-only Gmail tools. The caller authenticates with
an `x-api-key` header. The Gmail account is a Composio-managed connected account,
identified by `user-id`.

`wire.py` adds an `mcp_servers.composio_gmail` block to the profile's
`config.yaml` (`tools.include` pins the same 7 tools) and writes the API key to
the profile's `.env`. It is a script, not a hand edit, because profile config
files are often guarded against direct edits.

Read-only is enforced fail-closed at two layers: the server's allowlist at
create time, and `tools.include` on the Hermes side. The connected account may
still hold a wider OAuth scope, so the allowlist is what prevents writes.

The 7 read-only tools: `GMAIL_FETCH_EMAILS`, `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`,
`GMAIL_FETCH_MESSAGE_BY_THREAD_ID`, `GMAIL_LIST_THREADS`, `GMAIL_LIST_LABELS`,
`GMAIL_GET_PROFILE`, `GMAIL_GET_ATTACHMENT`.

## Key files

| Key file | Holds |
|----------|-------|
| `keys/composio-gmail-mcp/host` | the Composio MCP host (`backend.composio.dev` or the host returned at create time, verbatim) |
| `keys/composio-gmail-mcp/server-id` | the MCP server id |
| `keys/composio-gmail-mcp/user-id` | the Gmail connected-account user id |
| `keys/composio-gmail-mcp/api-key` | the Composio project API key (a secret) |

Each file is read at runtime and whitespace-stripped. A missing key fails with a
message naming its path. The profile `config.yaml` references the API key as
`${MCP_COMPOSIO_GMAIL_API_KEY}`, so an unresolved variable yields a clean 401,
never an empty header. Do not add a default.

## Files

| File | Role |
|------|------|
| `wire.py` | Writes the `composio_gmail` block into `<profile>/config.yaml` and the key into `<profile>/.env`. Backs up `config.yaml` first. |
| `README.md` | This file. |

## Install / Verify

```bash
# Wire a profile (values come from the key files above):
<hermes-home>/hermes-agent/venv/bin/python3 services/composio-gmail-mcp/wire.py --profile <name>

# Verify through Hermes' own MCP client (expect connect + exactly 7 tools):
hermes -p <name> mcp test composio_gmail
hermes -p <name> mcp list
```

The config is cached by mtime, so start a NEW session before the agent can use
the tools. After a key rotation, remove the `MCP_COMPOSIO_GMAIL_API_KEY` line from
the profile `.env` and re-run `wire.py`.
