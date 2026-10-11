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

Passthrough channels (`passthrough_channel_prefixes`, e.g. `["inv-"]`): a channel whose internal
name starts with one of these prefixes is never bridged; Hermes answers every message in it. The
name is looked up once through the Mattermost REST API (`MATTERMOST_URL` / `MATTERMOST_TOKEN` from
the gateway environment) and cached. A name that cannot be resolved is also left to Hermes, so a
failed lookup never hands a channel to the bridge.

## Per-channel overrides

By default every channel's session gets the global `allowed_tools` (read-only) and
`append_system_prompt`. A channel can be given more under `claude_code_bridge.channels.<channel-id>`:

```yaml
channels:
  <CHANNEL_ID>:
    allowed_tools: ["Edit(./NOTES.md)"]     # added to the global list, this channel only
    disallowed_tools: ["Read(//abs/path/**)"]  # added to the global disallowed_tools, this channel only
    append_system_prompt: "..."             # appended after a blank line, this channel only
    cwd: "~/some/project"                   # session starts in this directory, this channel only
```

**Working directory (`cwd`).** The session starts in `cwd` if set (`~` expanded, must exist), else in
the channel's spool directory. Quote channel ids in YAML. A bad `cwd` logs a warning and falls back.
The bridge's Stop hook, deny rules and MCP config are still loaded from the spool directory.

`disallowed_tools` (global default `[]`) is passed as `claude --disallowedTools`, a deny rule that
wins over any allow rule. Use it to take a file or folder away from a channel that otherwise has
broad read tools: absolute paths take the form `Read(//abs/path/**)` (a single leading `/` means
relative to the project). No flag is passed when the list is empty.

### Per-channel soul and browser

A channel can also set `soul_file` (a persona text file, read at session start and appended after
its `append_system_prompt`; `~` expands, a relative path resolves against `HERMES_HOME`), its own
`mcp_servers` (merged into the global map by name, `type: stdio` or `http`) and `mcp_allowed_tools`.
A missing soul file logs a warning and the session starts without it. Full recipe, the Playwright
browser example and the clarify-before-acting pattern: [docs/claude-code-bridge.md](../../docs/claude-code-bridge.md)
("Per-channel assistant" and "Browser access for a bridged channel").

### Recording user input in a file

A channel can also have each injected user message written into one of its files, before the
session is told about it:

```yaml
channels:
  <CHANNEL_ID>:
    record_user_input: {file: CLAUDE.md, exclude_users: [<bot-username>]}
```

For every message the worker injects (not `/reset`, not an `exclude_users` sender), it appends
`- YYYY-MM-DD HH:MM @user-input (<user>): <text>` inside the block delimited by
`<!-- @user-input:start -->` and `<!-- @user-input:end -->` in `<channel dir>/<file>`. A
`(none yet)` line in the block is replaced. The write is atomic. If the markers or the file are
missing, nothing is written and a warning is logged. A line already in the block is not added
twice, so a message re-queued after a failed inject is recorded once.

Unknown sub-keys are ignored, and a missing or malformed `channels` block changes nothing. The
override is applied once, in the session's argv (`config.for_channel`), so `--allowedTools`,
`--disallowedTools` and `--append-system-prompt` reflect it. The launch fingerprint includes that argv, so changing one
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
| `lib/senders.py` | Sends a reply through the adapter of the platform the message arrived on (Mattermost, Telegram, …), truncated to `max_reply_chars`. |
| `stop_hook.py` | Claude Code Stop hook; the reply guard (blocks a give-up reply, counted in `reply_guard_state.json`), otherwise writes the reply to the channel outbox. Never posts. |
| `browser_guard.py` | Browser guard: a stdlib PreToolUse hook (copied per channel) with interval spacing, hourly and daily caps, URL and tool denies. Blocks with exit 2. Enabled per channel by `browser_guard`; see below. |
| `chat_send.py` | Chat via hooks: the send command the session runs (copied per channel when `chat_hooks` is on). Checks `refuse_patterns`, then writes the message to the outbox. |
| `lib/chat_hooks.py` | Chat via hooks: placeholder rendering, the send command, the injected message prefix. |
| `repo_scope_guard.py` | Write guard: a PreToolUse hook (copied per channel) that keeps a repo channel's writes inside its own repo, its `.claude` and its companions, under `repo_scope_root`. Exit 2 names the owning repo. |
| `public_first_guard.py` | Write guard: a PreToolUse hook that denies a private copy of a file that exists as a real file in the public repo. Exit 2 says to edit the public file. |

**Chat via hooks (example).** Set `chat_hooks: true` and `chat_hooks_file` on a channel (template:
`config/chat-hooks.json.example`). The system prompt then carries no chat contract; the chat is reached only
through the send command and the Stop hook. Full flow and texts file: [docs/claude-code-bridge.md](../../docs/claude-code-bridge.md#chat-via-hooks).

**Browser guard (example).** Set `browser_guard` on a channel (or globally); the hook is registered only when it is set. Blocked calls get a short reason from the hook, and invalid values block rather than disable.

```yaml
claude_code_bridge:
  channels:
    "<channel-id>":
      browser_guard:
        min_interval_s: 5
        max_per_hour: 120
        max_per_day: 400
        deny_url_patterns: ["mode=map"]
        deny_tools: ["mcp__browser__browser_take_screenshot"]
```

Full keys, counting rules and the state file: [docs/claude-code-bridge.md](../../docs/claude-code-bridge.md#browser-guard). Browsing rules for the agent: [docs/browsing-playbook.md](../../docs/browsing-playbook.md).

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
