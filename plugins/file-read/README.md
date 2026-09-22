# file-read — reading without writing

<!-- Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/plugins/file-read/README.md -->

Registers the **`file_read`** toolset: one tool, `file_read`, with three ops.

| op | does |
|---|---|
| `open` | reads a file's lines (`offset`, `limit`) |
| `list` | lists file names under a directory (`pattern` = glob on the name) |
| `search` | regex over the readable files under a path |

Anything else — `write`, `move`, `delete`, `OPEN`, a missing op — is refused. The
tool has no code path that changes the filesystem, so the limit is an absent verb,
not an instruction ([docs/enforcement.md](../../docs/enforcement.md)).

## What it reads

A file is read only when name, mode and content all agree:

- **planning formats:** `.md .txt .json .yaml .yml .toml .csv .tsv .log`, and
  extensionless text (`NOTES`, `LICENSE`);
- **named source formats:** `.php .go .rs .c .java .html .twig` and the rest of
  `SOURCE_FORMATS` in `__init__.py` — read to size work, never changed.

Refused whatever else is true: a script extension (`.sh .py .js .ts .rb .pl` …),
a script name (`Makefile`, `.bashrc` …), the executable bit, a `#!` first line,
binary content, an extension on neither list, and a symlink whose target fails
any of these. `list` shows names only; `search` skips every file `open` would
refuse.

## Enabling

Enable the plugin in the profile and grant `file_read` — never `file` — in its
`platform_toolsets`. See `config/profiles/planner/config.yaml.example`.

Tests: `tests/smoke/file-read.sh` (wraps `test_file_read.py`, real hermes-agent).
