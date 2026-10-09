# claude-code-bridge — chat channels answered by a persistent Claude Code session

claude-code-bridge answers allowed chat channels through one long-lived, interactive Claude Code
session per channel (the `claude` TUI, in a detached tmux session). A `pre_gateway_dispatch`
hook spools each allowed message and tells Hermes to skip it; a worker thread in the gateway
types it into the session when the session is idle. A Stop hook writes the reply to the
channel outbox, and the worker posts it: top-level, or in a thread when the user wrote in one.
Hermes' own model is not called for bridged messages. Design: [`docs/claude-code-bridge.md`](../../docs/claude-code-bridge.md).

Opt-in and fail-closed: `enabled: false` by default, empty allowlists admit nobody, and the
hook is fail-open (any error returns `None`, so Hermes handles the message as before).

Reset: an allowed user posting exactly `/reset` or `!reset` (case-insensitive, surrounding
whitespace ignored) starts a fresh Claude Code conversation for that channel. Nothing is posted.

Investigations: a message matching `passthrough_patterns` is not bridged. Hermes answers it in
a thread under that message, in the same channel, with its native delegate_task fan-out.

## Per-channel overrides

By default every channel's session gets the global `allowed_tools` (read-only) and
`append_system_prompt`. A channel can be given more under `claude_code_bridge.channels.<channel-id>`:

```yaml
channels:
  <CHANNEL_ID>:
    allowed_tools: ["Edit(./NOTES.md)"]     # added to the global list, this channel only
    append_system_prompt: "..."             # appended after a blank line, this channel only
```

Unknown sub-keys are ignored, and a missing or malformed `channels` block changes nothing. The
override is applied once, in the session's argv (`config.for_channel`), so `--allowedTools` and
`--append-system-prompt` reflect it. The launch fingerprint includes that argv, so changing one
channel's override restarts only that channel's idle session, through the usual `--resume` path.

## Files

| File | Role |
|---|---|
| `plugin.yaml` | Manifest: name, version, `pre_gateway_dispatch` hook. |
| `__init__.py` | `register(ctx)` and the dispatch hook; allowlists, reset, passthrough. Runnable self-test. |
| `lib/config.py` | Defaults plus the `claude_code_bridge:` block of the profile `config.yaml`. |
| `lib/spool.py` | Atomic per-channel inbox, outbox and `state.json`. |
| `lib/worker.py` | `tick()` per-channel state machine and the daemon `start_thread()`. |
| `lib/session.py` | tmux session lifecycle, idle detection and injection; launch fingerprint. A launch-config change restarts an idle session with `--resume` (context kept), logged at INFO, nothing posted. |
| `lib/mcp.py` | Per-channel `.mcp.json` from `mcp_servers`; resolves `keyfile:` references; MCP argv. |
| `lib/poster.py` | Mattermost REST post, truncated to `max_reply_chars`. |
| `stop_hook.py` | Claude Code Stop hook; writes the reply to the channel outbox. Never posts. |

## Install

1. Symlink the plugin into the profile's plugin home and opt in:

   ```sh
   HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
   ln -sfn "$(pwd)/plugins/claude-code-bridge" "$HERMES_HOME/profiles/<profile>/plugins/claude-code-bridge"
   ```

   then add `claude-code-bridge` to `plugins.enabled` in that profile's `config.yaml`.
2. Configure the `claude_code_bridge:` block (see `config/profiles/chat/config.yaml.example`). Set
   `enabled: true`, `allowed_users` and `allowed_channels` explicitly. Every key and its
   default is in [`docs/claude-code-bridge.md`](../../docs/claude-code-bridge.md#configuration).

## Verify

```sh
python3 plugins/claude-code-bridge/__init__.py          # allowlist decision self-test
python3 -m unittest tests/smoke/test_claude_code_bridge_worker.py -v
python3 -m unittest tests/smoke/test_claude_code_bridge_mcp.py -v   # MCP render, keyfile, argv
hermes -p <profile> plugins list               # expect: claude-code-bridge  enabled · user
```
