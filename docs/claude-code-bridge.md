# claude-code-bridge — chat channels answered by a persistent Claude Code session

> An **optional mode** of the `chat` role (`claude_code_bridge.enabled`). The role itself is backend-agnostic: its model is whatever `config/backends.yaml` configures. See [docs/profiles.md](profiles.md#the-chat-role).

claude-code-bridge connects a chat channel to **one long-lived, interactive Claude Code
session** (the normal `claude` TUI, not `claude -p`) running in a detached tmux
session. Each chat message is typed into that session; when the session ends its
turn, a Stop hook hands the final reply back and the gateway posts it. Hermes' own
model is not called for bridged messages.

It is opt-in: `enabled: false` is the default, and an empty allowlist admits nobody.

It relies only on the user's locally installed Claude Code: the `claude`
executable on `PATH`, its own login and plan. Nothing is written into project
folders or into the user's global Claude Code settings.

## Flow

```
chat ─► Hermes gateway ─► claude-code-bridge plugin (pre_gateway_dispatch)
                            ├─ allowlist check (sender + channel; empty = nobody)
                            ├─ /reset or !reset → enqueue a control item → skip (posts nothing)
                            ├─ investigation (passthrough_patterns) → return None
                            │     └─ Hermes answers in a thread under that message (delegate_task fan-out)
                            ├─ otherwise enqueue to <home>/channels/<channel>/inbox/ → return {"action": "skip"}
                            └─ worker thread (inside the gateway process)
                                 ├─ ensure session: tmux <prefix><channel> running `claude`
                                 ├─ when idle: paste next message, press Enter
                                 └─ when <channel>/outbox/ has a reply: post it (top-level, or in a thread;
                                    see reply placement)
Claude Code session ── Stop hook ──► writes last_assistant_message to <channel>/outbox/
```

## Who is answered, and where

- **Allowlists.** `allowed_channels` lists channel ids; `allowed_users` lists chat
  usernames. `"*"` means every channel the bot is in, or every sender. `[]` means none
  (fail-closed). A message in a channel that is not allowed is left to Hermes as before.
  A message in an allowed channel from a sender who is not allowed is dropped silently.
- **Timestamps.** Each message is injected as `[YYYY-MM-DD HH:MM · sender] text`, in local
  time, from the moment it was received. The session's system prompt tells it that messages
  can arrive late or out of order, so it uses the timestamps to decide what the latest
  request is.
- **Queue.** One message is in flight per channel; the rest wait in FIFO order.

## Reply placement

A reply goes top-level by default. It goes into a thread in two cases:

- **(a) The user wrote inside a thread.** The reply goes into that same thread (the thread
  root is the post's `root_id`, else the source `thread_id`).
- **(b) An investigation.** A message matching `passthrough_patterns` is not bridged at
  all. The hook returns `None`, and Hermes' own agent answers it in a thread under that
  message, in the same channel, with its native `delegate_task` fan-out. If the message is
  already a reply, the answer goes into the existing thread; otherwise the investigation
  post itself becomes the thread root.

`reply_in_thread: true` is a configuration switch that makes every bridged reply a thread on
the user's post. Its default is `false`. The wait-budget failure reply (below) follows the
same placement.

## Investigations (passthrough)

`passthrough_patterns` is a list of case-insensitive regexes. A matching message is not
queued and not sent to the Claude Code session. Hermes answers it instead, in a thread, as in
rule (b) above. The default list covers `investigate`, `investigar`, `investigación`,
`I want to research`, `quiero investigar` and `deep research`. A pattern that does not
compile is logged and skipped.

Passthrough is checked after `/reset` and only for allowed senders in allowed channels.

## Commands

- **`/reset` or `!reset`** (an allowed user, exactly this text, case-insensitive, surrounding
  whitespace ignored) starts a fresh conversation for that channel. The worker stops the
  session, drops queued messages up to and including the reset, clears the outbox and
  forgets the session id, so the next message opens a new Claude Code conversation. Nothing
  is posted to chat. Messages sent after the reset are kept. `!reset` is the reliable form,
  because Mattermost may take `/reset` as a slash command.

## Session lifecycle

- **Launch.** Inside `tmux new-session -d -s <prefix><channel>` in the channel folder:
  `claude --model <model> --effort <effort> --setting-sources project,local --permission-mode <permission_mode> --allowedTools <allowed_tools> --append-system-prompt <append_system_prompt> --session-id <uuid>`.
  The first start passes `--session-id`; later starts pass `--resume <uuid>`.
- **Config change.** The worker stores a launch fingerprint (sha256 of the launch argv plus the rendered `.mcp.json` hash) when a session starts. Before injecting into a running session, if the fingerprint differs (or is unknown, as for sessions started before this check), it stops the session and restarts it with `--resume <uuid>`, so the context is kept and the new settings apply. It logs one INFO line and posts nothing to chat.
- **Attach (watch or take over).** `tmux -L claude-code-bridge attach -t claude-code-bridge-<channel>`
  (socket `tmux_socket`, session prefix `session_prefix`).
- **Idle exit.** After `idle_exit_minutes` with nothing queued or in flight, the session is
  stopped. The next message resumes the same conversation by session id.
- **Wait budget.** If a reply does not arrive within `wait_budget_minutes`, the worker posts
  one failure reply and clears the in-flight item. Nothing else about waiting, restarts or
  providers is posted.
- **Environment.** tmux and the session get a scrubbed environment: only `HOME`, `USER`,
  `LOGNAME`, `PATH`, `SHELL`, `LANG`, `TMPDIR` and `TERM`. Chat credentials and provider keys
  are not passed in.

## Components

| Part | Responsibility |
|---|---|
| `plugins/claude-code-bridge/__init__.py` | Registers `pre_gateway_dispatch`. Enforces `allowed_users` / `allowed_channels` itself (the hook runs **before** gateway authorization). Handles `/reset`, passthrough and enqueue. Starts the worker thread once. |
| Passthrough (`passthrough_patterns`) | Investigations are not bridged. A matching message returns `None`; Hermes answers it in a thread (rule (b)). |
| Worker (`lib/worker.py`) | One in-flight message per channel; FIFO for the rest. Starts or resumes sessions, injects, collects replies, enforces the wait budget, posts via the platform adapter, and stops idle sessions. |
| `_reply_root` (worker) | Picks the reply's `root_id`: the user's thread root if they wrote in one, else the user's post id only when `reply_in_thread: true`, else `None` (top-level). |
| Session launcher (`lib/session.py`) | The `claude` launch line in the Launch section above, inside `tmux new-session -d` with the scrubbed environment. |
| Per-channel settings | `<home>/channels/<channel>/.claude/settings.json` holds only the bridge's Stop hook. `--setting-sources project,local` keeps the user's global hooks and settings out of bridge sessions. |
| Stop hook (`stop_hook.py`) | Reads the hook JSON from stdin and writes the reply to the channel outbox. It never posts, never prints and always exits 0. It never sees chat credentials. |
| Injector | Refuses unless the input box is empty; bracketed paste (`tmux load-buffer` + `paste-buffer -p`), short pause, separate `Enter`; verifies the box emptied, retries with a longer pause if the Enter was absorbed, clears the line and reports on failure. Never `send-keys "text" Enter` in one call. |
| `lib/poster.py` | Mattermost REST post, truncated to `max_reply_chars`. Reads `MATTERMOST_URL` and `MATTERMOST_TOKEN` from the gateway process. Fail-open. |

## Design decisions

- **The reply path stays inside the gateway.** The Stop hook only writes a file;
  the gateway posts. Chat tokens never enter the Claude Code session, where its
  shell tool could read them, and replies go through the adapter.
- **Reply placement.** Replies go top-level by default and into a thread in the two cases
  under "Reply placement". The bridge never threads on the user's own post id unless
  `reply_in_thread: true`.
- **Session lifetime ≈ prompt-cache lifetime.** The prompt cache lives server-side
  and is keyed on content, not on the process. Keeping an idle session alive past
  the cache window saves only startup time, so sessions exit after
  `idle_exit_minutes` (default 60) and are resumed by `--session-id` on the next
  message. Resume restores history but not launch flags, so every resume re-passes
  them. No keep-warm pings.
- **Interactive transport.** `transport: tmux` keeps sessions attachable for a
  human (watch or take over a channel). A `claude -p --input-format stream-json`
  transport is a possible later option; it is not the default and is not implemented.
- **Fail-closed and least privilege.** `enabled: false`, empty allowlists,
  `permission_mode: dontAsk` with a read-only + web tool list. Anything unlisted is
  refused, never prompted (a prompt would stall an unattended session).
- **Quiet chat.** One final post per turn, plain text, capped at
  `max_reply_chars`. Nothing about queueing, sessions or restarts is posted; only
  an exhausted wait produces a single failure reply.
- **Timestamped messages, out-of-order safe.** Each injected message is prefixed with its
  local time and sender (`[YYYY-MM-DD HH:MM · user] text`, from the item's `ts`), and the
  system prompt tells the session that messages may arrive late or out of order, so it
  orders its reasoning by those timestamps. A failed inject re-queues the item under its
  original file name, keeping its queue position.
- **Self-contained.** No dependency on any external tmux harness; the injection and
  idle-detection rules above are implemented in the plugin.

## Verified behaviour (Claude Code 2.1.295, spike)

| Question | Result |
|---|---|
| Stop hook payload | Fires once per turn; contains `last_assistant_message` (the reply text), `session_id`, `effort`, `stop_hook_active`, `transcript_path`. |
| `--setting-sources project,local` | Project Stop hook fires; the user's global SessionStart hook did not appear in the debug log (it did without the flag). |
| `--effort` | Supported as a launch flag. |
| Trust dialog | Not shown for new folders in the spike. The launcher still answers the trust dialog if it appears, once, before the first message. |
| Resume by `--session-id` | Ready in ~1 s, no dialog, prior context recalled. |
| Input methods | `send-keys -l`, bracketed paste, and flattened single-line all submitted exactly once; bracketed paste kept quotes, `$HOME`, backticks and emoji verbatim. |

## Open questions

- Idle/busy detection: the session registry is not consulted. Idle is read from the pane
  (prompt visible, no `esc to interrupt`, empty input box). Re-check those pane signals on
  each Claude Code upgrade.
- The long-history resume summary dialog (sessions above ~100k tokens) can block an
  unattended resume; auto-compaction settings or a dismiss step are needed.
- Behaviour when two messages arrive during one turn: queue separately (default) or
  merge into one prompt.
- A second gateway on the same bot would double-reply; run the bridge in the
  gateway that already owns the bot, scoped by `allowed_channels`.

## Configuration

Set these under the `claude_code_bridge:` block of the profile's `config.yaml`. The defaults below
are the values in `plugins/claude-code-bridge/lib/config.py`. A list may be given as a YAML list or
as a string such as `'["a", "b"]'`.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch. Off means the plugin never bridges a message. |
| `claude_bin` | `""` | Path to the `claude` executable. Empty = first `claude` on `PATH`. |
| `model` | `haiku` | `claude --model` value: alias or full model id. |
| `effort` | `medium` | `claude --effort` value. |
| `transport` | `tmux` | Reserved. `tmux` is the only transport the code uses. |
| `session_prefix` | `claude-code-bridge-` | tmux session name = prefix + channel id. |
| `home` | `""` | Bridge state root. Empty = `<HERMES_HOME>/claude-code-bridge`; one folder per channel below it. |
| `idle_exit_minutes` | `60` | Stop an idle session after this many minutes. |
| `permission_mode` | `dontAsk` | `claude --permission-mode`. Tools not allowed are refused, never prompted. |
| `allowed_tools` | `[Read, Glob, Grep, WebSearch, WebFetch]` | `claude --allowedTools`. May include narrow Bash patterns for a local capability, e.g. `Bash(/path/to/local-tool add:*)` (prefix match on one subcommand of one absolute path). Never add a broad `Bash(*)`. |
| `append_system_prompt` | chat contract text (see below) | `claude --append-system-prompt`. |
| `max_reply_chars` | `15000` | Replies longer than this are cut with an ellipsis. |
| `allowed_users` | `[]` | Chat usernames that may reach the bridge. `"*"` = every sender; empty = nobody. |
| `allowed_channels` | `[]` | Channel ids the bridge answers. `"*"` = every channel the bot is in; empty = none. |
| `reply_in_thread` | `false` | `true` = every bridged reply is a thread on the user's post. |
| `wait_budget_minutes` | `360` | How long to wait for a reply before the single failure reply. |
| `tmux_socket` | `claude-code-bridge` | tmux server socket name (`tmux -L`). |
| `poll_seconds` | `2` | Worker loop interval. |
| `passthrough_patterns` | investigation regexes in EN/ES/NL (see "Investigations") | Case-insensitive; a match is not bridged. |
| `keys_dir` | `""` | Folder that `keyfile:` references resolve against. Empty = `<HERMES_HOME>/keys`. |
| `mcp_servers` | `{}` | Claude Code MCP servers for the session, as a mapping name → server spec (`type: http`, `url`, `headers`). A value `keyfile:<service>/<name>` is replaced at session start by the file `<keys_dir>/<service>/<name>` (whitespace stripped). Empty = an empty server map; the session still runs `--strict-mcp-config`. |
| `mcp_allowed_tools` | `[]` | Tool names appended to `--allowedTools` when an MCP file was rendered, e.g. `mcp__composio_gmail__GMAIL_FETCH_EMAILS`. |
| `channels` | `{}` | Per-channel overrides keyed by channel id. `allowed_tools` is added to the global list; `append_system_prompt` is appended after a blank line, for that channel only. Only `Edit(path)` rules govern file edits; `Write(path)` rules are ignored. |

### MCP servers (how they are wired)

At each session start the bridge renders `<channel-dir>/.mcp.json` (mode 0600) from
`mcp_servers`, resolving `keyfile:` values. The session is launched with
`--mcp-config <that file> --strict-mcp-config`, so only these servers load: no project
`.mcp.json` and no user MCP config. `mcp_allowed_tools` is added to `--allowedTools`.
When no server survives (none configured, or a key is missing), the file is still written with an
empty `mcpServers` map and the flags are still passed, so account, user and plugin MCP servers
(for example a browser connector) never load into a chat session.
The rendered file also gets a `Read(./.mcp.json)` deny rule in the channel's
`.claude/settings.json`, so the model cannot read the key back into a reply.

Failure is non-fatal. A server whose key file is missing or empty is left out, with a log
line naming the server and the key reference (never the value). If no server survives,
no MCP flags are passed and the session starts exactly as without MCP.

Connection timing, per the Claude Code MCP docs (checked 2026-10): a remote HTTP server
is connected at session start to load its tool names, unless a discovery cache from an
earlier session is used, in which case it connects on the first call to one of its tools.
Tool search is on by default, so tool definitions are deferred and the model only calls a
Gmail tool when the message needs one. A first connection that fails with a transient error
is retried three times, then marked failed. The session keeps running and the model is told
the server failed. No daemon is started by the bridge.

The default `append_system_prompt` is: "You are answering in a chat channel. Reply in plain
text, as one message, in the user's language. Each message starts with [time · sender];
messages can arrive late or out of order, so use the timestamps to judge what the latest
request is. Never mention file paths, tools, software, settings, permissions or
infrastructure. If you cannot do something, say in plain words what you can't do here and
offer what you can do instead. Never ask the user to change settings or do technical work."

A worked example with every key is in
[`config/profiles/chat/config.yaml.example`](../config/profiles/chat/config.yaml.example).
