# Roles ↔ Profiles — the hybrid mapping

> "Four roles" (`orchestrator`, `coding`, `planner`, `qa-tester`) is a **logical generalization**
> used throughout this documentation to talk about job descriptions. It is not a promise that
> your deploy runs four OS processes. This page states the actual, hybrid mapping plainly —
> including where it changed after the scaffold's first real end-to-end task run (see
> [docs/virgin-voyage-results.md](virgin-voyage-results.md)).

[docs/profiles.md](profiles.md) documents each role's purpose, toolset, and config template.
This page answers a narrower, more practical question: **when you write `--assignee <profile>`
on a real `hermes kanban create` call (see [docs/task-authoring.md](task-authoring.md)), what do
you actually put there?** The honest answer is "it depends which role" — and that's exactly what
this page reconciles.

---

## The headline

A **role** is a job description this documentation uses to talk about responsibilities:
dispatch-and-never-implement, implement, research-and-decompose, verify-via-browser-and-never-fix.
A **profile** is a real, addressable thing on disk — a `config.yaml`, a `state.db`, a memory
directory — that `--assignee` must name exactly for the dispatcher to find it.

On the reference install, those two concepts line up in **three different ways**, not one:

| Role | What it actually is | Real `--assignee` value |
|---|---|---|
| **orchestrator** | The **default/root profile** — the always-on gateway that owns the dispatcher loop, cron scheduling, and the chat gateway. It is `$HERMES_HOME/config.yaml` itself, not a named `profiles/<name>/` subdirectory. | N/A — orchestrator dispatches, it does not receive task assignments. |
| **coding** | The implementation role — the real `coding` profile (primary) or `<second-coding-profile>` (lighter/cheaper tier) on the reference install. The retired name `coder` is not a profile. | `coding` or `<second-coding-profile>` — **not** `coder`. |
| **planner** | **A real profile** with a deliberately narrowed toolset (research, decomposition, kanban create/link — no edits, no spawning); see [docs/profiles.md](profiles.md). | `planner`. |
| **qa-tester** | **A real, separately-provisioned worker profile.** Unlike coding/planner, this one is not an alias — it carries its own `browser` toolset and a live CDP endpoint (`browser.cdp_url`, see [docs/browser-capability.md](browser-capability.md)) that no other profile needs. | `qa-tester` (or whatever you name the dedicated profile you create for it). |

**The practical trap:** [docs/task-authoring.md](task-authoring.md)'s worked example uses
`--assignee coding` for readability. On the reference install that is the real `coding` profile.
The retired name `coder` is not a profile and must not appear in new tasks or configs. Read every `--assignee <role>`
example in this repo's docs as shorthand for "the real profile that currently plays this role in
your deploy," and substitute your own profile name before running the command for real.

---

## Why `qa-tester` is different — the virgin voyage changed this

Earlier drafts of this scaffold's docs described `qa-tester` the same way as `coding`/`planner` —
folded into whichever general-purpose profile had spare cycles (`private`, on the reference
mapping in [docs/profiles.md](profiles.md#mapping-to-the-reference-install-real-profiles)). That
was a reasonable guess before any task had actually exercised the role.

The scaffold's first real end-to-end task run needed a worker that could actually drive a browser
against a running dev server — a distinct capability surface (see
[docs/browser-capability.md](browser-capability.md)), not just spare compute. Folding that into
an already-loaded general-purpose profile has a real cost: either every task on that profile pays
the `browser` toolset's system-prompt weight even when it isn't testing anything, or you're
hand-toggling toolsets between tasks. A dedicated profile is the cleaner shape once the capability
is real rather than aspirational — so `qa-tester` graduated to a real, separately-provisioned
profile, and the other three roles didn't need to.

**This page is the reconciled, authoritative version of the role↔profile mapping.** Where the
per-profile mapping tables in [docs/profiles.md](profiles.md),
[docs/architecture/data-flow.md](architecture/data-flow.md), and
[docs/architecture/topologies.md](architecture/topologies.md) still describe the earlier,
coarser picture (including `qa-tester` folded into `private`), treat this page as superseding
them on that specific question. Updating those tables to match is tracked as an open item — see
the note in [docs/profiles.md](profiles.md#mapping-to-the-reference-install-real-profiles)
pointing back here in the meantime.

---

## "Four roles" is a naming convention, not a process count

Nothing about this scaffold enforces exactly four processes:

- **Fewer is fine.** You could run a single real profile (the root/default install) doing
  dispatch and implementation together, if you don't need the isolation — this is close to what
  `coding` already does on the reference install (see the "orchestrator + coding" note in
  [docs/profiles.md](profiles.md#mapping-to-the-reference-install-real-profiles)).
- **More is fine.** The reference install already runs two coding-role tiers (`coding` and
  `<second-coding-profile>`) side by side. Nothing stops you from running two `qa-tester`-role profiles for
  parallel browser sessions, or splitting `planner` into separate research and decomposition
  profiles.
- **The constant is the vocabulary, not the process count.** These four job descriptions are
  what this documentation's other pages assume when they say "the coding profile" or "the
  qa-tester role" — map that vocabulary onto however many real profiles your own deploy actually
  needs.

---

## A minimum viable mapping for a new adopter

If you're standing this up for the first time, you do not need four profiles on day one:

1. **Start with two:** the root/default install (orchestrator — dispatch only) plus one worker
   profile (`coding`) doing everything else, including ad-hoc verification via a
   force-loaded `browser` skill on individual tasks (see `--skill` in
   [docs/task-authoring.md](task-authoring.md#--skill--force-loading-extra-capability)) rather
   than a standing `qa-tester` profile.
2. **Split out `planner`** once research/decomposition work is frequent enough to want its own
   memory and skill set, separate from implementation.
3. **Graduate to a dedicated `qa-tester`** once you have real, repeatable E2E tests worth
   automating against a persistent browser (see [docs/browser-capability.md](browser-capability.md))
   — this is exactly the point at which the reference install's own mapping changed, per the
   [virgin voyage](virgin-voyage-results.md).

---

## See also

- [docs/profiles.md](profiles.md) — per-role purpose, toolset, and config template
- [docs/task-authoring.md](task-authoring.md) — the `--assignee` contract this page's practical trap refers to
- [docs/browser-capability.md](browser-capability.md) — why `qa-tester` needed to become a real profile
- [docs/virgin-voyage-results.md](virgin-voyage-results.md) — the end-to-end run that forced this reconciliation
- [docs/architecture/data-flow.md](architecture/data-flow.md) — dispatch mechanics per profile
