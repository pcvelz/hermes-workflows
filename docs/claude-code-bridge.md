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

## Several posts from one reply (`---post---`)

A reply can be delivered as several separate posts. A line holding only `---post---` ends one
post and starts the next (a marker in the middle of a line is ordinary text):

```
First hotel, name and details.
![Hotel A photo](https://example.com/hotel-a.jpg)
---post---
Second hotel, name and details.
![Hotel B photo](https://example.com/hotel-b.jpg)
```

- Each part is posted in order, on the same thread (the placement above applies to all parts).
- Empty parts are dropped.
- Each part goes through the channel's normal send path. On Mattermost, a remote image written
  as `![alt](https://…)` becomes a file attachment on that part's post and the markdown is
  removed from the text. An image that cannot be downloaded stays visible as its URL. Write
  the plain `![alt](url)` form; a trailing size suffix such as ` =240` is not an image match.
- If a part fails to post, the parts not yet posted are kept for retry; delivered parts are
  never posted again.
- Always on; there is no configuration switch.

Use it for a fixed per-item format such as one post per hotel with its photo. Images render
as attachments on Mattermost; other platforms receive the same parts through their adapter.

## Chat via hooks

Why: a bridged session should work like a plain Claude Code session in its repo. The chat contract in
the system prompt ("You are answering in a chat channel…") and the automatic posting of the final text
pushed the model to deliver quickly instead of following the repo's own procedure. With chat via hooks
the system prompt carries no chat rules; the chat is reached only at the moments it matters.

Off by default. A channel opts in with `chat_hooks: true` (globally or under `channels.<id>`). Off, the
bridge behaves exactly as described in the other sections.

**Flow (on):**

- **Incoming.** Each injected message is prefixed with the rendered `prompt_context` (sender and local
  time), and the system prompt has no chat contract. The channel's `soul_file` and its own
  `append_system_prompt` still apply.
- **Send.** The session answers the chat by running the send command, a script copied into the channel
  folder: `<python> <channel>/chat_send.py "<message>"`. A `---post---` line splits one message into several
  posts. The script checks `refuse_patterns`; a match prints `send_refused` (with the reason) and exits 1,
  and nothing is posted. Otherwise the message goes to the outbox, the turn's sent count goes up, and the
  script prints "Sent to the chat." An interim message does not end the turn; the worker posts it and the
  turn keeps running.
- **Ask.** AskUserQuestion is refused with `ask_denied`, so the session asks the question through the send
  command instead of a dialog nobody can answer.
- **End of turn.** The Stop hook (`stop_hook.py`) decides from the turn's sent count:
  - something was sent: the turn ends and nothing is posted again (a `turn_end` item closes the turn in the worker);
  - nothing was sent and fewer than `max_stop_blocks` blocks so far: the turn is blocked with
    `stop_nothing_sent` as the reason, and the session continues;
  - nothing sent after `max_stop_blocks` blocks: the final text is delivered as a fallback (never a lost turn),
    and a WARNING is written to `reply_guard.log`.
- **Reset.** A new injected turn clears the sent and block counts (`chat_turn.json`).

The reply guard does not run on a channel with chat via hooks: `refuse_patterns` in the send script are the
guard, and a give-up that is never sent cannot reach chat.

**The texts file.** `chat_hooks_file` (global, or per channel) names a JSON file. Template:
`config/chat-hooks.json.example`. The texts live only in that file, never in code or the system prompt.
Relative paths resolve against the Hermes home. Fields:

| Field | Used by | Meaning |
|---|---|---|
| `audience` | `{audience}` in the other texts | Who reads the chat and how to write for them. |
| `ask_denied` | AskUserQuestion denial | Text returned when the session tries to ask a question. |
| `stop_nothing_sent` | Stop hook (block reason) | Text when the turn ends with nothing sent. |
| `send_refused` | `chat_send.py` on a refusal | Text with `{reason}` (from the matched rule). |
| `prompt_context` | Message prefix | Prefix of each injected message; `{sender}` and `{time}` (local, `YYYY-MM-DD HH:MM`). The template adds one sentence: if the message names something that does not exist, the session checks the closest match itself, then confirms it in one short question ("Do you mean X?") before acting on it. |
| `refuse_patterns` | `chat_send.py` | List of `{pattern, reason}`; case-sensitive regexes as written (use `(?i)`). |
| `max_stop_blocks` | Stop hook | Blocks allowed per turn before the fallback delivers the final text (whole number >= 0). |

Placeholders: `{send}` renders as the exact send command line (with `"<message>"`); `{audience}`, `{reason}`,
`{sender}`, `{time}` as above. Other braces are left as written.

**Validation and fail-off.** A missing file, a missing or wrongly typed field, or an invalid pattern turns
chat via hooks off for that launch and logs one WARNING. The launch then keeps the chat contract and the
reply guard. The launch fingerprint includes the texts, so editing the file restarts an idle session
(with `--resume`).

**Spool channels.** The session's Bash allow list gets one prefix rule for the send command
(`Bash(<python> <channel>/chat_send.py:*)`), and nothing else. A repo channel (with a `cwd`) runs under the
user's own settings, so its settings must allow that command; the bridge adds no rule there.

**Files written per launch (on):** `chat_send.py` (copied from the plugin), `chat_hooks.json` (the rendered
texts the hooks read), `chat_turn.json` (per-turn counts, created when used). Off, the first two are removed
and the turn file is removed too.

## Debrief

Why: when a bridged session fails, the operator wants to ask it why, while its context is still alive, without
the chat seeing the exchange. `!debrief <question>` does that.

- **Who.** Allowed senders only (the same allowlist as every bridged message). Any other sender's command is ignored.
- **Where.** The answer is always filed in the channel's spool, as `debrief/<time_ns>.md`. The operator reads the
  file there; the bridge logs one INFO line with its path and never the content. The answer is also posted to
  `operator_channel` (top-level), but only when that channel is in the same team as the bridged channel (see
  Team boundary). Nothing is posted to the bridged channel. No `operator_channel` is needed to ask.
- **How.** The question is queued like a chat message and injected as an operator turn, prefixed with
  `[operator debrief — answer the operator, not the chat user]`. The session's final text is the answer.
- **Chat rules off for that turn.** The Stop hook delivers the final text without the chat-hooks block and without
  the reply guard. `chat_send.py` refuses during the turn ("Debrief turn: answer in your final text, not in the chat.").
- **Cleanup.** The operator's `!debrief` post is deleted from the channel when the command is picked up
  (best-effort; the bridge needs the right to delete posts). The bridge posts no acknowledgement.
- **Marker.** A `debrief.json` file in the channel folder marks the debrief turn. It is removed when the answer
  is posted, and a normal injection clears it too.

## Team boundary

Operator posts never cross teams. Every post the bridge makes about a channel (a debrief answer, a blocked-prompt
notice) goes only to a channel in the SAME team as the bridged channel. The bridge resolves both channels' team
through the Mattermost API (cached). If the teams differ, or a lookup fails, nothing is posted: a WARNING is logged
without any content, and the notice is not retried. A debrief answer is still filed in the spool.

- **Per channel.** `operator_channel` can be set globally or per channel (`channels.<id>.operator_channel`), so each
  team can have its own operator channel. A channel's own value wins.
- **Global value.** A global `operator_channel` logs a WARNING at load: it is checked against each bridged
  channel's team at send time.
- **Replies in the bridged channel** are not operator posts and are unaffected: they stay in their own channel.

## Write guards

Two PreToolUse hooks on `Write|Edit|MultiEdit|NotebookEdit`, written into the channel's `.claude/settings.json`
next to the Stop hook and the browser guard. Both are off by default and per channel. A guard that is off has
its script removed from the channel folder.

**`repo_scope_guard`** (repo channels only: the channel needs a `cwd`). A session writes only inside its own repo,
which is the git toplevel of the channel's `cwd` (a worktree maps back to its main repo), inside that repo's
`.claude` (resolved), or inside a `repo_scope_companions` entry. A write under `repo_scope_root` (for example a
code folder that holds several repos) to any other repo is denied with exit 2; the message names the owning repo.
Writes outside `repo_scope_root`, a session with no `cwd`, and a missing `repo_scope_root` all fail open.
Companions are added to the global list, so a channel adds its own; the guard's script is `repo_scope_guard.py`.

```yaml
claude_code_bridge:
  repo_scope_guard: true
  repo_scope_root: /path/to/code          # the folder that holds the repos
  repo_scope_companions: []               # extra directories this session may also write
  channels:
    "<channel-id>":
      repo_scope_companions: ["/path/to/companion-repo"]
```

**`public_first_guard`** (any channel). Keeps one canonical copy of a file that lives in two repos. Config:
`public_first: {private_root: <path>, public_root: <path>}`. A write to a file under `private_root` is denied when
the same relative path exists under `public_root` as a real file (not a symlink, and not under a symlinked
directory), and the private path is a real file or does not exist yet. The message says to edit the public file
and symlink it into the private repo. A private path that is a symlink into the public repo passes, and so does
a private-only path. Use it for the case of a public repo and its private integration repo.

**Both guards fail open.** A guard that is not configured (missing roots), cannot read its input, or raises any
error exits 0 and the write goes through. They protect against a session writing into the wrong repo by
mistake; they are not a security boundary.

**Verify.** `tests/smoke/test_claude_code_bridge_write_guards.py` runs both scripts as subprocesses on fixture
trees: the same case matrix as the reference suites, plus the session install (hooks in settings, fail-open
configs, fingerprint, channel overrides).

## Reply guard (give-ups never reach chat)

Why: an assistant sometimes ends a turn with a give-up ("I cannot do it right now…", "try again
later"). Prose in the soul did not prevent it, so the rule is enforced. A give-up is never posted.

The guard is the bridge's **Claude Code Stop hook** (`stop_hook.py`), the same hook that hands the
reply to the outbox. It runs when the turn ends, before the reply is handed over, so the check and
the nudge happen in the same turn. An earlier design held the reply in the worker until the pane went
idle and then typed the nudge; that was racy and could post after 300 s. The worker has no guard now.

```yaml
channels:
  "<channel-id>":
    reply_guard:
      patterns: ["(?i)cannot do it right now", "(?i)try again later"]
      nudge: "Your reply was not sent: it gives up instead of delivering. ..."
      max_nudges: 2
```

- **Check.** The hook checks `last_assistant_message`, the final reply. A multi-part reply
  (`---post---`) is checked as a whole.
- **Block.** On a match, while the turn's count is below `max_nudges`, the hook prints
  `{"decision": "block", "reason": "<nudge> Matched phrase: …"}` and exits 0. Claude Code continues
  the same turn with the reason as feedback. The give-up is not written to the outbox.
- **Count.** `reply_guard_state.json` in the channel folder holds the turn's nudge count. A block adds
  one. A delivered reply resets it to 0, and so does the worker when it injects a new turn.
- **Cap.** After `max_nudges` blocks the reply is delivered, and one WARNING line is appended to
  `reply_guard.log`.
- **Log.** Each block appends one INFO line to `reply_guard.log` with the pattern. The reply text and
  the nudge text are never logged.
- **Config file.** Each session launch writes `reply_guard.json` (the channel's merged, compiled guard).
  A changed guard changes the launch fingerprint, so an idle session restarts with `--resume` and
  rewrites the file. Removing the guard removes the file.
- **Fail open.** A missing or broken `reply_guard.json`, or a count that cannot be recorded, delivers
  the reply and logs a WARNING. A broken guard never stalls a turn; the trade-off is that a give-up can
  then reach chat.
- **Off by default.** The guard is on only when a channel (or the global `reply_guard`) has at least one
  pattern that compiles. A channel's keys merge over the global block. An invalid pattern is dropped.
- **Not covered.** The hook does not block a turn that still has background work running (see Open questions).

## Blocked prompts

Why: a bridged session is unattended. When a repo hook or a permission rule opens a confirmation dialog
("Do you want to proceed? ❯ 1. Yes 2. No"), nobody answers it, the turn never ends, and chat gets no
reply and no explanation. This is detection on the session's pane, handled per channel.

```yaml
claude_code_bridge:
  blocked_prompt: {mode: off}      # default: no detection at all
  blocked_settle_s: 60             # the same dialog must stay on screen this long
  blocked_timeout_s: 600           # decline mode: seconds from first sight to Escape
  operator_channel: ""             # Mattermost channel id for notices; empty = WARNING log only
  deny_ask_user_question: false    # true = AskUserQuestion is refused (answer in the chat reply instead)
  channels:
    "<channel-id>":
      blocked_prompt: {mode: notify}   # a channel overrides the global mode
```

- **Detection.** The worker captures the pane while a turn is in flight. A dialog is read from the
  bottom 60 lines: a question line ("Do you want to proceed?", "Would you like to proceed", …) or the
  "Enter to select" footer, at least two numbered option rows, and a `❯` selector line. The idle footer
  (`⏵⏵ …`, `shift+tab to cycle`, `? for shortcuts`) does not veto a dialog that has an option block. A
  busy spinner or an empty `❯` prompt is not a dialog.
- **Settle.** The dialog's signature (a hash of the question and the options) must stay the same for
  `blocked_settle_s` across worker ticks before it counts. A different signature starts over.
- **`mode: off`** (the default) does nothing: no capture, no notice, no key press. Behaviour is exactly
  as before the feature.
- **`mode: notify`.** Posts the channel name, the question and the options to `operator_channel`, once
  per signature. Never to the bridged channel. With `operator_channel` unset, only a WARNING is logged.
  Nothing is answered.
- **`mode: decline`.** Notifies, then presses Escape (decline, the safe default) `blocked_timeout_s`
  after the dialog was first seen, logs a WARNING, and queues a note for the session. The note goes in
  on the next idle session: "The dialog '<question>' was declined because nobody can answer it here.
  Finish the task without that step, and say in your reply what was not done." The turn then ends and
  its reply reaches chat.
- **`mode: accept`.** Once a dialog has settled, presses Enter on its first (default) option and logs
  one INFO line with the question. The Enter gets a full settle window; if the same dialog is still
  there, Enter is pressed again, at most three presses in all. After that the dialog is notified
  instead. Exempt dialogs are never answered; they are only notified: rewind dialogs, the `/model`
  selection ("Select model"), the exit confirmation while background work runs ("Background work is
  running"), and AskUserQuestion menus.
- **Operator answers.** Not implemented. The worker does not read the operator channel, so a reply
  there is not delivered to the session. The notice says nothing is answered by the bridge.
- **AskUserQuestion.** With `deny_ask_user_question: true` the session's settings get a PreToolUse
  hook for AskUserQuestion that exits 2 with "Ask the user in your chat reply instead; there is no
  interactive user here." The toggle is part of the launch fingerprint, so an idle session restarts
  (with `--resume`) when it changes. A channel with a repo cwd gets the hook through its `--settings`
  file as well.
- **Scope.** Detection runs only while a turn is in flight. The pane is read as text; the bridge does
  not inspect the dialog's meaning beyond the rules above.

## Investigations (passthrough)

`passthrough_patterns` is a list of case-insensitive regexes. A matching message is not
queued and not sent to the Claude Code session. Hermes answers it instead, in a thread, as in
rule (b) above. The default list is English: `investigate` (and its forms), `I want to research` and
`deep research`. To cover another language, add one regex per trigger word in
`claude_code_bridge.passthrough_patterns`; that list replaces the default, so copy the English entries too.
A pattern that does not compile is logged and skipped.

Where Hermes' passthrough reply lands: by default (`passthrough_reply_in_thread: true`) it is a thread on the investigation
post, or the existing thread if the message was already a reply. With `passthrough_reply_in_thread: false` it goes where the
user wrote: a main-channel post gets a main-channel reply (no thread, so Hermes uses the channel-level session), and a post
inside a thread gets its reply in that thread.

`passthrough_channel_prefixes` (default `[]`, off) is a list of channel-name prefixes. Every message in an allowed
channel whose internal name starts with one of them is left to Hermes, never bridged (for example research
channels created per topic). A channel name that cannot be resolved is left to Hermes too (fail closed to Hermes).

Passthrough is checked after `/reset` and only for allowed senders in allowed channels.

## Commands

- **`/reset` or `!reset`** (an allowed user, exactly this text, case-insensitive, surrounding
  whitespace ignored) starts a fresh conversation for that channel. The worker stops the
  session, drops queued messages up to and including the reset, clears the outbox and
  forgets the session id, so the next message opens a new Claude Code conversation. Nothing
  is posted to chat. Messages sent after the reset are kept. `!reset` is the reliable form,
  because Mattermost may take `/reset` as a slash command.
- **`!reset clear`** does the same reset and also deletes the channel's posts since the previous reset: the
  users' messages, the bot's replies and the `!reset clear` post itself. System posts (joins, header changes)
  are kept. The previous reset point is the time of the last `!reset` the bridge handled (stored in the
  channel's state); with none stored, only posts from the last 24 hours are deleted. The same allowlist as
  every bridged message applies. Deletions are best-effort: a failed delete is logged and not retried. Nothing
  is posted to chat. Why: tests start from a clean conversation, with no earlier turns left in the channel.
  Mattermost's `since` filter is applied to the channel's pages (newest first), so the listing stops at the
  first older post.

## Self-healing (restarts and startup)

- **Adapter readiness.** The worker starts when the plugin loads, but the gateway reference only arrives with the first inbound message. A Mattermost reply sent before that (for example an in-flight turn resumed at startup) is posted through the REST API with the gateway's own bot credentials. Other platforms with no live adapter hold the reply without using a retry attempt.
- **Held replies.** A reply that cannot be posted stays in the channel outbox. A failed post is retried with backoff and parked at the attempt cap; a held reply is never dropped.
- **In-flight resume.** A turn whose Claude Code session died with the gateway is relaunched on the same conversation and its message is injected once. A later session loss does not inject it a second time.
- **Start backoff.** A failed session start records `start_retry_at` in the channel state; relaunches wait for it to expire.
- **Launch script.** Each session is started from an owner-only `launch.sh` in the channel folder, run by tmux.

## Session lifecycle

- **Launch.** Inside `tmux new-session -d -s <prefix><channel>` in the channel folder:
  `claude --model <model> --effort <effort> --setting-sources project,local --permission-mode <permission_mode> --allowedTools <allowed_tools> --append-system-prompt <append_system_prompt> --session-id <uuid>`.
  A channel with a `cwd` (a repo) instead starts plain `claude` in that folder, with no permission,
  allow or setting-sources flags: the user's own settings decide the rights. Its line adds only the
  Stop hook (`--settings`), the MCP config, the soul and the built-in connector deny.
  `claude_args` (see Configuration) is appended verbatim to either line.
  The first start passes `--session-id`; later starts pass `--resume <uuid>`.
- **Config change.** The worker stores a launch fingerprint (sha256 of the launch argv plus the rendered `.mcp.json` hash) when a session starts. Before injecting into a running session, if the fingerprint differs (or is unknown, as for sessions started before this check), it stops the session and restarts it with `--resume <uuid>`, so the context is kept and the new settings apply. It logs one INFO line and posts nothing to chat.
- **Attach (watch or take over).** `tmux -L claude-code-bridge attach -t claude-code-bridge-<channel>`
  (socket `tmux_socket`, session prefix `session_prefix`).
- **Idle exit.** After `idle_exit_minutes` with nothing queued or in flight, the session is
  stopped. The next message resumes the same conversation by session id.
- **Wait budget.** If a reply does not arrive within `wait_budget_minutes`, the worker posts
  one failure reply and clears the in-flight item. Nothing else about waiting, restarts or
  providers is posted.
- **In-flight survival.** A turn in flight survives a gateway restart and a machine crash: if its
  session is gone, the worker relaunches it on the same conversation and re-injects the message
  exactly once (marker `inflight_redelivered_at`, saved before the injection), then the reply flows back
  normally. A second loss does not inject again; the wait budget decides.
  A finished reply is never dropped: while the platform adapter is not connected yet (right after a
  gateway restart) it is held in the outbox and posted once the adapter is live, with no attempt used,
  bounded by `wait_budget_minutes`. A real post failure backs off 2 s, 4 s, ... capped at 5 min; after
  20 failures the reply is parked in the outbox (one ERROR line) and posted after the next restart.
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
| Stop hook (`stop_hook.py`) | Reads the hook JSON from stdin. Runs the reply guard (blocks a give-up reply, see Reply guard), otherwise writes the reply to the channel outbox. It never posts. It never sees chat credentials. |
| Injector | Refuses unless the input box is empty; bracketed paste (`tmux load-buffer` + `paste-buffer -p`), short pause, separate `Enter`; verifies the box emptied, retries with a longer pause if the Enter was absorbed, clears the line and reports on failure. Never `send-keys "text" Enter` in one call. |
| `lib/poster.py` | Mattermost REST post, truncated to `max_reply_chars`. Reads `MATTERMOST_URL` and `MATTERMOST_TOKEN` from the gateway process. Fail-open. |

## Design decisions

- **The reply path stays inside the gateway.** The Stop hook writes a file (or, for a give-up,
  blocks the turn); the gateway posts. Chat tokens never enter the Claude Code session, where its
  shell tool could read them, and replies go through the adapter.
- **One guard, in the same turn.** The reply guard is the Stop hook, not a worker-side hold: the check
  runs when the turn ends, so there is no race with the pane going idle.
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

- Background work and the Stop hook: a turn that still has a background Bash or Monitor task running
  is not held by the hook. The hook input has `transcript_path`, but the transcript's record of a
  background task (launched, completed) was not verified, so no block is made on it. A self-woken
  turn is still delivered by the worker (`last_reply_to`).
- The Stop-hook `decision: block` output is not yet verified against a live Claude Code run: the
  integration run was blocked by the 1Password guard. Re-run the live check before relying on the guard.
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
| `disallowed_tools` | `[]` | `claude --disallowedTools` (a deny rule; wins over `allowed_tools`). Empty = no flag. Use `Read(//abs/path/**)` for an absolute path (`//` = filesystem root; a single `/` is project-relative). Added to by a channel's own `disallowed_tools`. Part of the launch fingerprint, so a change restarts that channel's idle session. |
| `append_system_prompt` | chat contract text (see below) | `claude --append-system-prompt`. |
| `max_reply_chars` | `15000` | Replies longer than this are cut with an ellipsis. |
| `allowed_users` | `[]` | Chat usernames that may reach the bridge. `"*"` = every sender; empty = nobody. |
| `allowed_channels` | `[]` | Channel ids the bridge answers. `"*"` = every channel the bot is in; empty = none. |
| `reply_in_thread` | `false` | `true` = every bridged reply is a thread on the user's post. |
| `wait_budget_minutes` | `720` | How long to wait for a reply before the single failure reply. |
| `tmux_socket` | `claude-code-bridge` | tmux server socket name (`tmux -L`). |
| `poll_seconds` | `2` | Worker loop interval. |
| `passthrough_patterns` | investigation regexes in EN/ES/NL (see "Investigations") | Case-insensitive; a match is not bridged. |
| `passthrough_reply_in_thread` | `true` | `false` = a passthrough reply goes where the user wrote (main post -> main reply, thread post -> that thread). |
| `repo_permission_mode` | `""` (unset) | Repo channels only (a `cwd`): `--permission-mode` value; unset = the user's own settings decide. Valid: `default`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions`; others are ignored with a warning. Spool channels ignore it. Per-channel value overrides the global one. |
| `claude_args` | `[]` | Extra `claude` arguments, appended verbatim to the launch line (a channel's list is added to it). Use it to match how the deployment's user launches Claude Code, e.g. a permission flag, so a repo channel needs no other setup. |
| `keys_dir` | `""` | Folder that `keyfile:` references resolve against. Empty = `<HERMES_HOME>/keys`. |
| `mcp_servers` | `{}` | Claude Code MCP servers for the session, as a mapping name → server spec (`type: http`, `url`, `headers`). A value `keyfile:<service>/<name>` is replaced at session start by the file `<keys_dir>/<service>/<name>` (whitespace stripped). Empty = an empty server map; the session still runs `--strict-mcp-config`. |
| `mcp_allowed_tools` | `[]` | Tool names appended to `--allowedTools` when an MCP file was rendered, e.g. `mcp__composio_gmail__GMAIL_FETCH_EMAILS`. |
| `channels` | `{}` | Per-channel overrides keyed by channel id. `allowed_tools` and `disallowed_tools` are added to the global lists; `append_system_prompt` is appended after a blank line, for that channel only. `model`, `effort`: run this channel's session on another model or effort than the global default. Only `Edit(path)` rules govern file edits; `Write(path)` rules are ignored. A channel may also set `record_user_input: {file, exclude_users}`: each injected user message is appended to the `@user-input` block of `<channel dir>/<file>` before the session sees it (see the plugin README, "Recording user input in a file"). |

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

## Per-channel assistant (bootstrap prompt / soul)

A channel can be given its own assistant: a short bootstrap prompt, a longer persona file
(the "soul"), its own working folder and its own tools. All of it is set under
`claude_code_bridge.channels.<channel-id>`, and only that channel is affected.

**`append_system_prompt` versus `soul_file`.**

- `append_system_prompt` is a string passed as `--append-system-prompt`. It is appended after a
  blank line to the global prompt. Use it for a one-line contract, such as the reply language.
- `soul_file` is a path to a text file. Its text is read at session start and appended after the
  channel's `append_system_prompt`, separated by blank lines. The order is always: global prompt,
  then the channel prompt, then the soul.
  - `~` expands to the user's home.
  - A relative path resolves against the profile's `HERMES_HOME` (the folder that holds the
    profile's `config.yaml`), so `souls/<name>.md` means `<HERMES_HOME>/souls/<name>.md`.
  - A missing, unreadable or blank file logs a warning and the session starts with the prompt
    alone. A bad soul never stops the bridge.
  - The soul is passed on the launch line like any other prompt. Quotes, `$`, backticks and
    newlines survive intact; a multi-kilobyte soul is covered by the test suite. Large souls are
    supported: the launch line is written to the channel's owner-only `launch.sh`, which tmux runs,
    so a soul of tens of kilobytes does not hit tmux's command length limit.
  - Editing the file takes effect on the next message. The launch fingerprint covers the argv,
    so the idle session restarts with `--resume` and keeps its conversation.

**Working folder (`cwd`).** The session starts in `cwd` (per channel only; `~` expands; must be an
existing directory). Unset, it starts in the channel's own spool folder. The bridge's Stop hook and
deny rules are loaded explicitly for a custom `cwd`, so the channel's folder setup does not depend
on where the session starts.

A channel with a `cwd` is a normal Claude Code session in that folder: the folder's own settings (user,
project and local), its permission mode and its allow/deny rules apply, and the bridge adds only its
Stop hook, MCP config, soul and the built-in connector deny. The bridge's own `permission_mode`,
`allowed_tools` and `disallowed_tools` are not applied there. A channel without a `cwd` keeps the
bridge's sandbox (`--setting-sources project,local`, `permission_mode`, `allowed_tools`).

**Permission mode for a repo channel.** By default a repo channel is a plain `claude` session, and the
user's own settings (including `defaultMode`) decide what it may do. To match how the deployment's user
runs sessions, set `repo_permission_mode` (global or per channel): `default`, `acceptEdits`, `plan`,
`dontAsk` or `bypassPermissions`, passed as `--permission-mode`. An unknown value is logged once and
ignored. Spool channels (no `cwd`) ignore it and keep their locked `permission_mode` and allow list.

**Per-channel tools.** Each of these is merged with the global value:

| Key | Merge rule |
|---|---|
| `allowed_tools` | Added to the global list. |
| `disallowed_tools` | Added to the global deny list. |
| `mcp_allowed_tools` | Added to the global list. |
| `mcp_servers` | Merged into the global map by server name. A channel entry with the same name wins. |

### Example (placeholders only)

```yaml
claude_code_bridge:
  enabled: true
  allowed_channels: ["<channel-id>"]
  allowed_users: ["<chat-username>"]
  channels:
    "<channel-id>":
      append_system_prompt: "Reply in the user's language, as one short message."
      soul_file: "souls/<assistant-name>.md"   # <HERMES_HOME>/souls/<assistant-name>.md
      cwd: "~/<project-folder>"                # optional: start in this folder
      disallowed_tools: ["Read(//<absolute-path>/**)"]
      mcp_servers:
        <server-name>:
          type: stdio
          command: npx
          args: ["@playwright/mcp@latest", "--cdp-endpoint", "http://127.0.0.1:<port>"]
      mcp_allowed_tools:
        - mcp__<server-name>__<tool-name>
```

The soul file is ordinary prose. A minimal skeleton:

```markdown
# <assistant-name>

You help <audience> with <topic>. Speak plainly, in the user's language.

## Fixed defaults
Always use <fixed default, e.g. the same city or the same number of people>. Never ask about these.

## Clarify before acting
If a request leaves a required search parameter open (for example the place or the dates), do not
guess silently. Either state your assumed query back in one short message and ask for confirmation,
or ask for the missing parameters directly. Fixed defaults are written above and are never asked about.
```

**Clarify before acting (the generic pattern).** A request that leaves a required parameter open
gets one of two responses: a short restatement of the assumed query with a request to confirm, or a
direct question for the missing parameters. A value the operator fixed in the soul is applied
silently and is never asked about. The pattern lives in the soul, so it changes with the file and
needs no code change.

**Polite pacing for searches (soul pattern).** A soul that drives a search over many pages, such as
a matrix-style search of listings, should pace itself the way a considerate person would: one request
at a time, a pause between page loads, and a stop on a 429 response or a captcha. When the requested
range is oversized (very many pages, dates or places), push back in one short message and propose a
narrower range instead of running it. The pattern lives in the soul, names no site, and needs no code
change.

## Browser access for a bridged channel

A bridged session can drive a real browser through a per-channel stdio MCP server. The recipe
attaches Playwright MCP to a Chrome that is already running with a DevTools endpoint (see
[browser-capability.md](browser-capability.md) for starting that Chrome). Playwright MCP does not
launch its own browser in this setup.

```yaml
claude_code_bridge:
  channels:
    "<channel-id>":
      mcp_servers:
        browser:
          type: stdio
          command: npx
          args: ["@playwright/mcp@latest", "--cdp-endpoint", "http://127.0.0.1:9222"]
          # optional: keep screenshot images out of the model's context (see the image caution below)
          # args: ["@playwright/mcp@latest", "--cdp-endpoint", "http://127.0.0.1:9222", "--image-responses", "omit"]
      mcp_allowed_tools:
        - mcp__browser__browser_navigate
        - mcp__browser__browser_navigate_back
        - mcp__browser__browser_snapshot
        - mcp__browser__browser_click
        - mcp__browser__browser_type
        - mcp__browser__browser_fill_form
        - mcp__browser__browser_press_key
        - mcp__browser__browser_select_option
        - mcp__browser__browser_hover
        - mcp__browser__browser_wait_for
        - mcp__browser__browser_tabs
```

**Verified flags.** `npx -y @playwright/mcp@latest --help` (checked 2026-10) lists
`--cdp-endpoint <endpoint>  CDP endpoint to connect to.`, `--cdp-timeout <timeout>` (default 30000 ms)
and `--image-responses <mode>` (`allow`, `omit` or `only`; default `allow`). The `--cdp-endpoint` value
is the Chrome's DevTools address, here `http://127.0.0.1:9222`.

**Verified tool names.** A `tools/list` request over the server's stdio transport returned these 26
tools (checked 2026-10 against server version 1.64.0-alpha). Claude Code exposes them as
`mcp__<server>__<tool>`, so the server key `browser` gives the prefix `mcp__browser__`:

`browser_close`, `browser_resize`, `browser_console_messages`, `browser_handle_dialog`,
`browser_emulate_media`, `browser_evaluate`, `browser_file_upload`, `browser_drop`, `browser_find`,
`browser_fill_form`, `browser_press_key`, `browser_type`, `browser_navigate`, `browser_navigate_back`,
`browser_network_requests`, `browser_network_request`, `browser_run_code_unsafe`,
`browser_take_screenshot`, `browser_snapshot`, `browser_click`, `browser_drag`, `browser_hover`,
`browser_select_option`, `browser_tabs`, `browser_wait_for`.

The example list leaves out `browser_evaluate`, `browser_run_code_unsafe` and
`browser_file_upload` (arbitrary page script or local file access), and `browser_take_screenshot`
(see the image caution). Add them only on purpose. Tool names can change between versions, so
re-check with `tools/list` after an upgrade.

**Image-size caution.** Prefer `browser_snapshot` (the accessibility tree, text only) over
screenshots. Do not take full-page screenshots, and do not take screenshots of map views. Anthropic
rejects an image larger than 2000 px on a side in a request with many images, and that error ends
the turn, so the user gets no answer. Using `--image-responses omit` in the server args keeps images
out of the model's context entirely.

### Browser guard

A per-channel `browser_guard` is a mechanical rate limit and URL guard for the browser tools. It is
a `PreToolUse` hook on the session, so it applies whatever the model decides. Set it per channel, or
globally (a channel's keys are merged over the global ones):

```yaml
claude_code_bridge:
  channels:
    "<channel-id>":
      browser_guard:
        tools: "mcp__browser__.*"            # regex of guarded tool names (default shown)
        network_tools: "mcp__browser__browser_(navigate|navigate_back|click|press_key|select_option|fill_form)"
        min_interval_s: 5                    # spacing between network calls; the hook sleeps the remainder
        max_per_hour: 120                    # 0 or unset = no cap
        max_per_day: 400
        deny_url_patterns: ["mode=map"]      # regexes; block when tool_input.url matches
        deny_tools: ["mcp__browser__browser_take_screenshot", "mcp__browser__browser_evaluate", "mcp__browser__browser_run_code"]
```

- **Blocking.** A denied call exits the hook with code 2 and a short reason on stderr, which Claude
  Code shows to the model (for example "browser budget reached for this hour; report the results you
  have"). Nothing is posted to chat by the guard itself.
- **Counting.** Only `network_tools` count toward the caps and the spacing. Snapshots and other
  non-network tools pass without delay.
- **Files.** The hook is `browser_guard.py` and its config `browser_guard.json` in the channel
  directory. The call history is `browser_guard_state.json` (timestamps, pruned to 24 h, written
  atomically; a missing or corrupt file counts as empty), so caps survive a session restart.
- **Fail closed.** An invalid value (for example a non-numeric cap, a bad regex, or a `deny_tools`
  name outside `tools`) blocks every guarded call instead of disabling the guard. Unexpected errors in
  the hook also block.
- **Relaunch.** The guard config is part of the launch fingerprint, so changing it restarts an idle
  session with `--resume`, as for other launch settings.
- **Keep the sleep short.** The hook sleeps up to `min_interval_s` per call. Keep it well below
  Claude Code's hook timeout (about 60 s by default).

For what the browser should and should not do on a third-party site, see
[browsing-playbook.md](browsing-playbook.md).
