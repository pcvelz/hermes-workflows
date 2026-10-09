#!/usr/bin/env python3
"""check-configs.py — tripwire for profile config corruption.

A profile config.yaml that stops parsing makes the agent runtime fall back to
built-in defaults silently; plugins listed in the file then run as no-ops and
the only trace is one stderr line. This makes that failure LOUD (a macOS
notification) and deterministic (a sentinel file other tooling can check).

Every run:
  1. yaml.safe_load() the root config ($HERMES_HOME/config.yaml) and every
     $HERMES_HOME/profiles/*/config.yaml.
  2. Any parse failure writes $HERMES_HOME/CONFIG_BROKEN (the failing files,
     the exception, a timestamp) and fires a notification.
  3. A clean run removes a stale sentinel.
  4. Always exits 0: it runs from a launchd WatchPaths agent, which should
     never be given a reason to thrash.

HERMES_HOME defaults to ~/.hermes. The installer sets it in the plist.
"""
import datetime
import glob
import os
import subprocess
import sys

try:
    import yaml
except ImportError as _exc:  # pragma: no cover - PyYAML is expected to be present
    yaml = None
    _YAML_IMPORT_ERROR = _exc
else:
    _YAML_IMPORT_ERROR = None

HERMES_HOME = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser("~/.hermes")
SENTINEL = os.path.join(HERMES_HOME, "CONFIG_BROKEN")


def _profile_configs():
    """[(name, path), ...] for the root config plus every profile that has one."""
    candidates = [("default", os.path.join(HERMES_HOME, "config.yaml"))]
    for path in sorted(glob.glob(os.path.join(HERMES_HOME, "profiles", "*", "config.yaml"))):
        candidates.append((os.path.basename(os.path.dirname(path)), path))
    return [(name, path) for name, path in candidates if os.path.isfile(path)]


def _timestamp_note(bad_paths):
    """UTC timestamp for the sentinel. Never raises."""
    try:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        for p in bad_paths:
            try:
                return f"(datetime.now unavailable; mtime of {p} = {os.path.getmtime(p)})"
            except OSError:
                continue
        return "(timestamp unavailable)"


def _write_sentinel(failures):
    """failures: list of (profile_name, path, exception)."""
    ts = _timestamp_note([path for _, path, _ in failures])
    lines = [
        "HERMES CONFIG_BROKEN",
        "=====================",
        f"Written: {ts}",
        "",
        "One or more Hermes config.yaml files failed to parse. The runtime falls",
        "back to built-in defaults when this happens, which drops plugins.enabled",
        "with only a stderr line nobody sees.",
        "",
        "Fix the YAML below. The next clean run of check-configs.py clears this",
        "sentinel automatically.",
        "",
    ]
    for name, path, exc in failures:
        lines.append(f"--- profile '{name}': {path} ---")
        lines.append(str(exc))
        lines.append("")
    tmp = SENTINEL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, SENTINEL)


def _clear_sentinel():
    try:
        os.remove(SENTINEL)
        return True
    except FileNotFoundError:
        return False


def _notify(failures):
    names = [name for name, _, _ in failures]
    if len(names) == 1:
        subject = f"{names[0]} profile config is corrupt"
    else:
        subject = f"{len(names)} profile configs are corrupt ({', '.join(names)})"
    message = f"{subject}; the runtime is on built-in defaults"
    title = "Hermes CONFIG_BROKEN"
    # Embedded in an AppleScript double-quoted string literal.
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"config-guard: notification failed: {exc}", file=sys.stderr)


def _run():
    if yaml is None:
        print(f"config-guard: SKIPPED, PyYAML unavailable ({_YAML_IMPORT_ERROR}); "
              "cannot validate configs this run", file=sys.stderr)
        return 0

    configs = _profile_configs()
    failures = []
    for name, path in configs:
        try:
            with open(path, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
        except Exception as exc:
            failures.append((name, path, exc))

    if failures:
        _write_sentinel(failures)
        _notify(failures)
        bad = ", ".join(f"{n} ({p})" for n, p, _ in failures)
        print(f"config-guard: BROKEN, {len(failures)}/{len(configs)} config(s) failed to parse: "
              f"{bad}; sentinel written to {SENTINEL}, notification fired")
    else:
        cleared = _clear_sentinel()
        stale = " (stale sentinel cleared)" if cleared else ""
        print(f"config-guard: OK, {len(configs)} config(s) parsed cleanly{stale}")
    return 0


def main():
    # Never let an unexpected exception thrash the launchd WatchPaths agent.
    try:
        return _run()
    except Exception as exc:
        print(f"config-guard: unexpected error, exiting clean: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
