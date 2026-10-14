# session-scope-guard

A Hermes **plugin** that keeps a gateway chat user from reading other people's past sessions through
the `session_search` tool.

## Architecture (one paragraph)

`session_search` can list and read every session in the profile's session database. In a shared chat
channel that exposes one member's private topics to another. This plugin registers a
**`pre_tool_call`** hook that runs before every `session_search` call. If the turn has no gateway
session platform (CLI, cron, tests) it allows everything. In gateway chat it allows only the
**read/scroll shape** (`session_id` given, no `profile` argument) when the session's recorded
`user_id` equals the asking user's `HERMES_SESSION_USER_ID`. It blocks the **discovery shape**
(`query`), the **browse shape** (no `session_id`), any `profile` argument (another profile's database),
sessions owned by someone else, and sessions with no recorded owner. Each block logs
`session-scope-guard: blocked session_search (<reason>)` and returns a `block` action with a message
telling the model to answer from the current conversation. Any lookup error blocks in chat (fail-closed)
and allows outside chat (fail-open).

**Legacy chats are unreadable in chat by design (fail-closed).** A session with no recorded `user_id`
(created before ownership was recorded) has no owner, so in chat it cannot be read through
`session_search`, even by the person who wrote it. Nothing is scrubbed or back-filled.

## Files

| File | Role |
|---|---|
| `plugin.yaml` | Manifest: name, version, `pre_tool_call` hook |
| `__init__.py` | Hook implementation: scope rules, owner lookup, block message |
| `README.md` | This file |
| `tests/smoke/test_session_scope_guard.py` | Unit tests (owner lookup and session env mocked), repo root |

## Install

Symlink the plugin dir into each profile that needs it, then opt in with `plugins.enabled`:

```bash
ln -s <path-to-hermes-workflows>/plugins/session-scope-guard <profile-home>/plugins/session-scope-guard
```

Add `session-scope-guard` to the profile config's `plugins.enabled` list. Verify with
`hermes -p <profile> plugins list` (look for `enabled · user`).

## Verify

```bash
cd <path-to-hermes-workflows>
PYTHONDONTWRITEBYTECODE=1 <hermes-venv>/bin/python3 -m unittest tests/smoke/test_session_scope_guard.py
```

Expected: `OK` (7 tests). A live check is a chat question about another user's topics: the gateway log
must contain `session-scope-guard: blocked session_search` and the reply must name no foreign topic.
