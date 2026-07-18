# policy-gate — a permission gate for the Hermes agent

Replicates Claude Code's `settings.json` permission model on the Hermes agent via a single
`pre_tool_call` hook (the analog of CC's `PreToolUse` exit-2 block). Hermes is
allow-everything by default in CLI mode; this restores **deny/ask** rules **and** adds a
filesystem scope jail CC doesn't have. It is **self-contained** — the policy lives in one
file, with no `extends` to any external base.

## Policy source (self-contained)

The policy is a single JSON document in CC's `settings.json` format, resolved in this order:

1. `AGENT_POLICY_FILE` env var, if set;
2. the plugin's own `settings.hermes.json` (your real policy — **git-ignored**);
3. the committed `settings.hermes.json.example` template (generic defaults, **warn** mode).

Copy the template to start your real policy:

```sh
cp plugins/policy-gate/settings.hermes.json.example plugins/policy-gate/settings.hermes.json
# edit allowedRoots to point at YOUR project, then flip "mode" to "enforce" when ready
```

The document has two blocks — `permissions` (deny/ask rules) and `hermes` (the scope jail):

| CC rule | Hermes tool (arg) |
|---|---|
| `Bash(<glob>)` | `terminal` (`command`) |
| `Write/Edit(<glob>)` | `write_file` / `edit_file` (`path`) |
| `Read(<glob>)` | `read_file` (`path`) |

A match on `deny` → block; on `ask` → block-with-reason (the CLI is non-interactive, so
"ask" degrades to a hard block that tells a human to do it). Rules are mtime-cached.
Fail-open throughout — any load/match error allows the tool.

### The `hermes` block — filesystem scope jail (no CC equivalent)

```json
"hermes": {
  "mode": "warn",
  "allowedRoots": ["~/your-project", "/tmp", "/private/tmp", "/var/folders"],
  "denyOutsideRoots": true,
  "scopedTools": ["write_file", "edit_file", "patch"]
}
```

An **absolute** write/edit/patch path resolving outside every root is a scope violation:
`enforce` → blocked; `warn` → logged to `<repo>/logs/scope-violations.log`, not blocked.
Relative paths are skipped (cwd unknown → fail-open). The shipped template is in **warn**
mode so you can dry-run the jail before enforcing it.

## Files

| File | Purpose |
|---|---|
| `plugin.yaml` | Manifest — declares the `pre_tool_call` hook |
| `__init__.py` | Gate (deny/ask + scope jail) + runnable self-test |
| `settings.hermes.json.example` | Committed generic policy template (warn mode) |
| `settings.hermes.json` | Your real policy — **git-ignored**, created from the template |
| `README.md` | This file |

## Verify

```sh
python3 plugins/policy-gate/__init__.py                       # self-test: deny + warn/enforce scope
python3 -c "import json;json.load(open('plugins/policy-gate/settings.hermes.json.example'))"
hermes -p <profile> plugins list | grep policy-gate           # expect: enabled · user
```

Flip the policy `mode` warn↔enforce to dry-run vs hard-block the folder jail.
