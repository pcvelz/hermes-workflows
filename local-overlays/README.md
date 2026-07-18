# local-overlays/

Machine-specific configuration and env overlays that should never be
committed — the values here are tied to *your* host (paths, ports, local
service addresses, bring-up secrets for services you never actually start),
not to the project.

## What goes here

This directory is a free-form drop zone for small, host-specific overlay
files you create yourself when bringing Hermes up on your machine. There is
no fixed schema — use whatever shape fits your bring-up flow, for example:

- **A bring-up env file** (e.g. `bringup.env`) — a small `KEY=VALUE` file of
  host-specific variables consumed by your local `docker compose` /
  bring-up commands (things like `HERMES_HOME`, `HERMES_IMAGE`,
  `HERMES_MODEL`, `HERMES_PROFILE`, or placeholder values some compose
  service definitions require to pass their `${VAR:?}` parse guards even
  when you don't start that service — e.g. a DB password for a database you
  never bring up). Keep it minimal: only what your specific bring-up
  actually needs.
- **Config overlay YAML** (e.g. `config.root.yaml`, `config.<profile>.yaml`)
  — a host-specific override layered on top of the tracked
  `config/config.yaml.example` / `config/profiles/<profile>/config.yaml.example`
  templates, for values that differ per machine (a local backend port, a
  host-specific base URL, etc.) rather than per deployment. Copy the
  relevant keys from the tracked `.example` template into your overlay, not
  the whole file — only override what actually differs on your host.

## Why this exists separately from `config/*.yaml.example` and `.env.example`

- `config/**/*.yaml.example` and `.env.example` files are the **project**
  templates — checked in, meant to be copied to their real (gitignored)
  twin (`config.yaml`, `.env`) and filled in with your values.
- `local-overlays/` is for anything that's specific to *this particular
  machine's bring-up* rather than a value every user of the scaffold would
  fill in the same slot for — e.g. a one-off placeholder needed only to
  satisfy a compose file's variable-interpolation guard, or a config
  override you don't want merged into your main profile config. It keeps
  those one-off, host-tied values out of the main config/env templates so
  the templates stay generic and reusable.

## Creating your own overlay files

1. Decide what you actually need an overlay for (most setups won't need
   one at all — start without this directory and only add files here if a
   bring-up step needs a host-specific value that doesn't belong in
   `.env` / `config.yaml`).
2. Create the file directly under `local-overlays/` with whatever name
   describes its purpose (e.g. `bringup.env`, `config.<profile>.yaml`).
3. Fill in only your own host-specific values — never copy values from
   someone else's overlay or from any shared/example file.
4. Confirm it's excluded from git before you forget about it (see below).

## Gitignore status — verify before relying on this

Real overlay files under this directory must never be committed. As
shipped, this repo's tracked `.gitignore` does **not** contain a
`local-overlays/` entry — the exclusion currently relied on for local
overlay files, if any, lives in the *local, untracked* `.git/info/exclude`
(a per-clone file that is itself never shared or committed). That means a
fresh clone gets **no** overlay exclusion by default.

Before creating real files here, verify your own exclusion is actually in
place:

```
git check-ignore -v local-overlays/<your-file>
```

If that prints nothing, the file is NOT ignored — either add an entry to
your local `.git/info/exclude` (recommended for something this
host-specific: `echo 'local-overlays/' >> .git/info/exclude`) or track the
gap with whoever owns this repo's `.gitignore` so overlay files get a
durable, shared exclusion rule instead of a per-clone one.
