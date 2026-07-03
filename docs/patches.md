# Patching the Hermes agent without forking

> **ADVANCED / OPTIONAL.** This documents a PATTERN, not shipped code. This repository
> ships **NO** patched agent code. Prefer contributing fixes upstream to
> [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent); patch only
> as a last resort, and treat every patch as **temporary**.

---

## The problem

Hermes is installed natively from upstream (upstream NousResearch hermes-agent, pinned to
a specific git tag). Sometimes you need a small local change to the installed agent — a
bugfix, a behavior tweak, an adapter shim — before or without it landing upstream.

Forking the entire repository is heavyweight and drifts; editing installed files in place
is fragile because a `hermes update` silently overwrites them.

---

## The pattern: replacement files symlinked over the install

The mechanism works in four steps:

1. **Keep a `patches/` directory** containing full replacement copies of the files you
   change. These are NOT unified diffs — they are whole-file replacements. The running
   gateway loads your version instead of upstream's.

2. **Record the pristine upstream version** once as `<file>.orig.bak` alongside the
   installed file at `HERMES_HOME/hermes-agent/<path>.orig.bak`. This is the baseline for
   future drift detection.

3. **Symlink** the installed path to your `patches/` copy:
   ```bash
   ln -sf <repo>/patches/<file> "$HERMES_AGENT_HOME/<installed-path>"
   ```

4. The running gateway now loads your version transparently.

### Directory-mapping convention (PAIRS)

Each patched file is recorded as a `installed-relative|patches-relative` pair, for
example:

```
"agent/anthropic_adapter.py|anthropic_adapter.py"
"gateway/run.py|gateway/run.py"
```

The re-apply script iterates these pairs to rebuild all symlinks after an upgrade. A
worked skeleton is in [`../patches/reapply-patches.sh.example`](../patches/reapply-patches.sh.example).

---

## Re-applying after an upgrade

A `hermes update` replaces the symlinks with fresh upstream files, so patches must be
re-applied. The re-apply + drift-check algorithm (mirrored in the example script):

For each pair in PAIRS:

1. If the installed path is **already a symlink** pointing at the repo copy — leave it.
   (Idempotent, nothing to do.)

2. If the installed path is a **real file** (not a symlink — i.e. it was overwritten by an
   upgrade):
   - Compute the `.orig.bak` path alongside the install.
   - If `.orig.bak` exists and `diff -q "$bak" "$rtpath"` shows they **differ** — upstream
     changed the file. The patch may be **STALE**:
     - Copy the new upstream file aside as `<file>.upstream-<timestamp>` for reference.
     - Emit a **loud warning** to stderr instructing you to manually re-derive the patch
       against the new upstream before trusting it.
     - Increment a stale counter.
   - Re-symlink: `ln -sf "$repopath" "$rtpath"`.

The script is **idempotent** and never force-overwrites without reporting.

### End-of-run actions

After re-linking all pairs:

- **Reload the gateway** (reference: on macOS, `launchctl unload+load` the relevant
  `com.hermes-workflows.gateway*.plist` under `~/Library/LaunchAgents/`). Use the placeholder plist
  name from your own install.
- **Re-verify behavior** with a smoke test or an end-to-end probe that exercises the code
  path you patched.

If the stale counter is non-zero the script exits non-zero — treat it as a **must-fix**
before relying on the patched gateway.

---

## Illustrative example targets

The following table lists files that are **common candidates** for local patches in a
typical Hermes deploy. They are illustrative — the specific changes depend on your
environment. This scaffold ships **no** patched files.

| Installed path (relative to `HERMES_AGENT_HOME/`) | What a patch here typically addresses |
|---|---|
| `agent/agent_init.py` | Agent startup / initialization tweak |
| `agent/anthropic_adapter.py` | Anthropic-compatible request/response adapter shim (e.g. read-timeout adjustment for backends with cold-start latency) |
| `tools/approval.py` | Tool-call approval flow adjustment |
| `tools/mcp_oauth.py` | MCP OAuth handling shim |
| `gateway/run.py` | Gateway run-loop tweak |
| `gateway/platforms/<gateway>.py` | Platform-specific gateway integration adjustment |
| `hermes_cli/oneshot.py` | One-shot CLI invocation tweak |

### Why patches exist — a concrete example

`agent/anthropic_adapter.py` is a common patch target because the default client
read timeout in the upstream adapter may be shorter than the cold-start latency of a
self-hosted backend. Raising client read timeouts to **≥ 120 s** is the kind of
environment-specific fix that may not belong in the upstream default configuration —
making it a good candidate for a temporary local patch while a proper upstream fix is
discussed.

This illustrates the general principle: patches bridge the gap between upstream defaults
and your specific environment, and every patch should come with a plan to contribute the
fix or close the gap another way.

---

## Risks (read before you patch)

- **Pinned to a specific upstream checkout.** A patch is only valid against the exact
  agent version it was derived on. Applying it to a different version may introduce subtle
  bugs silently.

- **Applied in-place over a live running gateway.** A bad relink can break the running
  service immediately. Always reload the gateway and verify behavior after every change.

- **Broken by `hermes update`.** Upgrades overwrite symlinks and may change the underlying
  file, silently invalidating the patch. Run the re-apply script after every upgrade.

- **Not portable diffs.** These are whole-file replacements tied to one machine's install.
  They are not shareable patches and will not cleanly apply elsewhere without manual work.

- **Drift can be silent.** Without the `.orig.bak` drift check you can end up running a
  stale patch on top of changed upstream behavior — potentially combining both sets of
  bugs.

---

## Recommendation

Treat patches as **advanced / optional** and **temporary**. The preferred path is:

1. Open an issue against [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
2. Submit a PR with the fix.
3. If the PR cannot land in time, apply a local patch *and record it as technical debt*.
4. Delete the patch entry once the fix ships in an upstream release.

Keep the patch set **as small as possible**. Record a `.orig.bak` for every patched file.
Run the re-apply script (with drift check) after every upgrade. Be suspicious of any stale
warning — resolve it before trusting the patched code in production.
