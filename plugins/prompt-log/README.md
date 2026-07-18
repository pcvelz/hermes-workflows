# prompt-log

Logs every **user message** (with secrets scrubbed) to `<repo>/logs/user-prompts.log` — the
Hermes analog of Claude Code's `log-user-prompt.sh` UserPromptSubmit hook. Completes the audit
trio next to [`audit-log`](../audit-log/)'s `bash-commands.log` + `file-edits.log`.

## Architecture

A single `pre_llm_call` hook (the Hermes analog of CC's `UserPromptSubmit`). The agent core
invokes it once per turn with the user message; the plugin appends one scrubbed line and
**always returns `None`** — it is observational and must never alter the turn (unlike a
steering plugin such as `skill-router`, which uses the same event to *inject* context).
In-process Python, no subprocess.

Log dir resolution + secret scrubbing are the same approach as `audit-log` (`<repo>/logs/`,
gitignored, `HERMES_CMDLOG_DIR` override; redacts `key: value` credential forms +
`Authorization: Bearer` + secret-named env values before writing).

### Log line format

```
[YYYY-MM-DD HH:MM:SS] [sid] [PROMPT] <scrubbed, newline-escaped, truncated to 500 chars>
```

`sid` = the unique tail of the Hermes session id.

## Files

| File | Purpose |
|---|---|
| `plugin.yaml` | Manifest — declares the `pre_llm_call` hook |
| `__init__.py` | Hook + scrub + runnable self-test (`python3 __init__.py`) |
| `README.md` | This file |

## Verify

```sh
python3 plugins/prompt-log/__init__.py                   # self-test (proves redaction)
hermes -p <profile> plugins list | grep prompt-log       # expect: enabled · user
tail logs/user-prompts.log
```
