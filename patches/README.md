# patches/ — pattern documentation, not shipped patches

This directory documents a **PATTERN** for patching the installed NousResearch hermes-agent
without forking it. It deliberately contains **NO real patched agent code**. The only files
here are this README and `reapply-patches.sh.example`, a skeleton you copy and adapt.

---

## What lives here

- **`README.md`** — This pointer. Start here.
- **`reapply-patches.sh.example`** — Example-only re-apply + drift-check skeleton. Copy it
  to `reapply-patches.sh`, fill in the `PAIRS` array with your real installed-relative and
  patches-relative paths, and review carefully before running.

---

## Read this first

Full explanation of the pattern, the real conceptual example set, and the risks:
[`../docs/patches.md`](../docs/patches.md)

---

## If you adopt this pattern

- Place your whole-file replacement copies under this directory, organized however suits
  your patch set (flat or mirroring the upstream directory structure).
- Record a `.orig.bak` of each pristine upstream file at the install path
  (`HERMES_AGENT_HOME/<file>.orig.bak`) before creating any symlink.
- Never commit secrets, tokens, or machine-specific absolute paths — use environment
  variable placeholders (`HERMES_HOME`, `HERMES_AGENT_HOME`, `~/`, etc.).
- Prefer contributing the fix upstream to NousResearch/hermes-agent rather than carrying
  a local patch indefinitely.

---

> **Note:** An external write-up named eight patch files — those names are **fictional** and
> inaccurate. The real, verified example set contains seven files. See
> [`../docs/patches.md`](../docs/patches.md) for the authoritative list and the
> myth-busting section. Keep everything portable: placeholders only, no personal paths.
