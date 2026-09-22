# Contributing to hermes-workflows

Thanks for your interest! This is a **foundation / base** project: it ships a portable scaffold
(docs, config templates, and the key scripts) rather than a finished product. Contributions
that flesh out the scaffold — new profiles, skills, crons, docs, and hardening — are especially
welcome.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Getting started

```bash
# Fork on your host, then:
git clone <your-fork-url> hermes-workflows
cd hermes-workflows
git checkout -b feat/<short-description>
```

## Development setup

There are two deployment paths. Most contributors only need one.

### Native (launchd + venv) — reference path

1. Create a venv and install the upstream NousResearch hermes-agent (this scaffold tracks
   `v0.14.0`; see [docs/architecture/topologies.md](docs/architecture/topologies.md) for exact
   pinning — do not hard-pin a version in your PR).
2. Copy `config/` into your `HERMES_HOME` (default `~/.hermes`) and rename each `*.example`.
3. Point the config at your LLM backend per [docs/backend.md](docs/backend.md).

### Docker

```bash
cd docker && cp .env.example .env && docker compose up -d
```

> If your LLM backend binds loopback only, containers can't reach it directly. Rebind it to
> `0.0.0.0` (then reference via `host.docker.internal`) or tunnel it — and read the
> LAN-exposure caveat in [SECURITY.md](SECURITY.md).

### Enable the pre-commit hook

Before your first commit, enable the tracked pre-commit hook so it runs the
same static checks CI runs (`bash scripts/test.sh static`) and blocks the
commit locally on failure, instead of finding out on GitHub:

```bash
git config core.hooksPath .githooks
# or: bash scripts/install-hooks.sh
```

Bypass in an emergency with `git commit --no-verify`. Full details:
[docs/testing.md](docs/testing.md#local-pre-commit-gate-catch-a-red-ci-before-you-push).

## How to propose a new profile

A profile is a `config.yaml` + a skills include-list + a per-profile `MEMORY.md`
(behavioral rules only — keep it under **~2200 chars**). Profiles live under
`config/profiles/`. A good profile PR:

- [ ] Uses one of the role names (`orchestrator` / `coder` / `planner` / `qa-tester`) or clearly justifies a new role.
- [ ] Includes a minimal `config.yaml.example` and `MEMORY.md.example`.
- [ ] Documents what the profile is *for* in [docs/profiles.md](docs/profiles.md).

## How to propose a new skill

Skills are **whitelisted via an include-list** to avoid system-prompt bloat
(see [docs/skills-whitelist.md](docs/skills-whitelist.md)). Keep skills focused and small:
add the skill, add it to the relevant profile's include-list, and document it.

## How to propose a new cron

Crons live under `cron/` (see [cron/README.md](cron/README.md)). Describe the **schedule**,
**purpose**, **idempotency**, and how it **fails safe**. Existing categories are watchdogs,
memory sync, board hygiene, and backups — proposals should fit that pattern.

## Commit conventions

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add planner research-digest cron
fix: correct base_url in coder profile
docs: clarify the 0.0.0.0 bind caveat
chore: bump .editorconfig indent rules
```

Keep secrets out of commits (see [SECURITY.md](SECURITY.md)). Never commit `*.db`, `.env`, or
private vault content (they are gitignored).

## Pull requests

- Open against `main` and fill in the [PR template](.github/PULL_REQUEST_TEMPLATE.md).
- No secrets, tokens, `*.db`, or `.env` files.
- Keep everything **portable**: placeholders only (`HERMES_HOME`, `~/`, `<your-token>`,
  `<your-domain>`) — never bake in a personal home-directory path.
- Update docs if you change behavior or the repository layout.

## Releasing — every change ends in one

A change is done when it is released, not when it is committed. Tested work is
released without waiting to be asked:

1. `bash scripts/test.sh static` and `bash scripts/test.sh smoke` are green.
2. Commit, push `main`.
3. Bump from the last tag (`git tag --sort=-creatordate | head -1`): minor for a
   feature, patch for a fix.
4. `git tag vX.Y.Z && git push origin vX.Y.Z`
5. Release notes = what changed since the last tag, grouped by feature.
6. `gh release create vX.Y.Z --title "vX.Y.Z — <headline>" --notes-file <notes>`, then
   `gh release view vX.Y.Z` to confirm it is published.

Templates (`*.example`) carry one `Source of truth:` line linking their own file on
`main`, so every live copy can be checked against the latest release.
