# audit-log

The Hermes per-command **audit log** — the analog of Claude Code's `bash-commands.log` +
`file-edits.log`, written into a gitignored project `logs/` folder, **with secret scrubbing**.

## Architecture

A single `post_tool_call` hook (the Hermes analog of CC's `PostToolUse`) appends one line per
**executed** tool call. It is in-process Python — no subprocess per call.

| Tool | Log file |
|---|---|
| `terminal` | `<repo>/logs/bash-commands.log` |
| `write_file` / `edit_file` / `patch` | `<repo>/logs/file-edits.log` |

`logs/` lives in the project root and is **gitignored** (analog of CC's `.claude/logs/`). The
dir is resolved repo-root-relative from `__init__.py` (`.resolve()` follows the profile→repo
plugin symlink), overridable via `HERMES_CMDLOG_DIR` (used by the self-test).

### Secret scrubbing (mandatory)

Every line passes through `_scrub()` before it is written. A verbatim log would persist any
secret pasted into a command forever, so `_scrub()` redacts: secret-named env-var values;
`password:`/`token=`/`api_key:` key→value forms; `--password=` flags; `Authorization:
Bearer|Basic`; AWS `AKIA…`; `://user:pass@` URL creds; PEM `PRIVATE KEY` blocks. Secrets only,
not general PII.

### Design notes

- **Observational only** — never blocks. Gating is `policy-gate`'s `pre_tool_call` job.
- `post_tool_call` fires *after* execution, so this logs only commands that actually ran
  (with `duration` + `exit`). A command blocked by `policy-gate` never reaches this hook.
- **cwd** is not in the `post_tool_call` payload; it's in `agent.log` and joins by `sid`.

### Log line format

```
bash-commands.log : [YYYY-MM-DD HH:MM:SS] [sid] [EXECUTED] [<dur>ms] [exit=<n>] <scrubbed command>
file-edits.log    : [YYYY-MM-DD HH:MM:SS] [sid] [<tool>] <scrubbed path>
```

## Files

| File | Purpose |
|---|---|
| `plugin.yaml` | Manifest — declares the `post_tool_call` hook |
| `__init__.py` | Hook + `_scrub()` + runnable self-test (`python3 __init__.py`) |
| `README.md` | This file |

## Verify

```sh
python3 plugins/audit-log/__init__.py                    # self-test (proves scrubbing)
hermes -p <profile> plugins list | grep audit-log        # expect: enabled · user
```
