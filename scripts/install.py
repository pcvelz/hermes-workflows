#!/usr/bin/env python3
# Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/scripts/install.py
"""install.py — register this project's launchd jobs, verify each one, report.

One command, typed by the user, is the consent to write into
~/Library/LaunchAgents. Nothing else in this repo writes there.

For every job it either registers it, finds it already correct, reloads it, or
skips it -- and says which, with the reason. After registering it does not
trust the file: it reads the LOADED job back with `launchctl print` and compares
its live environment with the plist's EnvironmentVariables. A plist edit never
reaches a job that is already registered; only bootout + bootstrap does, so a
drift is reloaded here rather than left for a day of false pages.

Jobs:
  gateway    required  the Hermes gateway; its in-process dispatcher
                       (kanban.dispatch_in_gateway) is the loop's motor
  escalator  required  the resilience watcher (scripts/resilience)
  backup     optional  --with backup
  bridge     optional  --with bridge --project-dir DIR

Exit 0 only when every job asked for is loaded with the environment on disk
AND alive: no non-zero last exit code, and a KeepAlive job actually running.
The gateway's profile is never guessed; see docs/getting-started.md.
Re-running changes nothing when nothing changed, and still reports the truth.
"""
import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREFIX = "com.hermes-workflows."
REQUIRED = ("gateway", "escalator")
OPTIONAL = ("backup", "bridge")
TOKEN = re.compile(r"__[A-Z0-9_]+__")


class Fail(Exception):
    pass


def launchctl(*args):
    exe = os.environ.get("HERMES_LAUNCHCTL", "launchctl")
    return subprocess.run([exe, *args], capture_output=True, text=True)


def domain():
    return "gui/%d" % os.getuid()


def live_environment(label):
    """The loaded job's environment, or None when launchd has no such job."""
    r = launchctl("print", "%s/%s" % (domain(), label))
    if r.returncode != 0:
        return None
    env, inside = {}, False
    for line in r.stdout.splitlines():
        if line == "\tenvironment = {":
            inside = True
        elif inside and line.strip() == "}":
            break
        elif inside:
            m = re.match(r"^\s*(\S+) =>(?: (.*))?$", line)  # an empty value prints as "K => "
            if m:
                env[m.group(1)] = m.group(2) or ""
    return env


def drift(want, live):
    return sorted(k for k, v in want.items() if live.get(k) != v)


def placeholders(obj):
    if isinstance(obj, str):
        return TOKEN.findall(obj)
    if isinstance(obj, dict):
        return [t for v in obj.values() for t in placeholders(v)]
    if isinstance(obj, list):
        return [t for v in obj for t in placeholders(v)]
    return []


def health(label, keepalive):
    """None when the loaded job is alive, else why it is not. Loaded is not
    alive: a job can be registered with a perfect environment and be dying
    every few seconds."""
    r = launchctl("print", "%s/%s" % (domain(), label))
    if r.returncode != 0:
        return "not loaded"
    state = re.search(r"^\tstate = (.+)$", r.stdout, re.M)
    code = re.search(r"^\tlast exit code = (.+)$", r.stdout, re.M)
    state = state.group(1).strip() if state else "unknown"
    code = code.group(1).strip() if code else "(never exited)"
    if code not in ("0", "(never exited)"):
        return "loaded but dying: last exit code %s, state %s" % (code, state)
    if keepalive and state != "running":
        return "loaded but not running: state %s" % state
    return None


def stderr_tail(pl, n=5):
    path = pl.get("StandardErrorPath")
    if not path or not Path(path).is_file():
        return ""
    lines = [l for l in Path(path).read_text(errors="replace").splitlines() if l.strip()]
    return " | ".join(lines[-n:])


def dispatches(home, profile):
    """Only the profile's own config counts. The root config setting the key
    once let a profile that does not exist pass this check."""
    cfg = home / "profiles" / profile / "config.yaml"
    return cfg.is_file() and bool(re.search(r"^\s*dispatch_in_gateway:\s*true\b",
                                            cfg.read_text(), re.M))


def profiles(home):
    root = home / "profiles"
    return sorted(p.name for p in root.iterdir() if (p / "config.yaml").is_file()) \
        if root.is_dir() else []


def motor_profile(home, asked):
    """The profile whose gateway runs the dispatcher. Named: it must exist and
    set the key. Not named: exactly one profile may set it; otherwise refuse
    and list the choices rather than guess."""
    found = profiles(home)
    motors = [p for p in found if dispatches(home, p)]
    if asked:
        if asked not in found:
            raise Fail("profile %r does not exist: no config.yaml in %s. Profiles found: %s"
                       % (asked, home / "profiles" / asked, ", ".join(found) or "none"))
        if asked not in motors:
            raise Fail("profile %r does not set kanban.dispatch_in_gateway: true in %s "
                       "-- the gateway would run with no motor"
                       % (asked, home / "profiles" / asked / "config.yaml"))
        return asked
    if len(motors) == 1:
        return motors[0]
    if not motors:
        raise Fail("no profile in %s sets kanban.dispatch_in_gateway: true (profiles found: %s); "
                   "set it in the one that should run the dispatcher"
                   % (home / "profiles", ", ".join(found) or "none"))
    raise Fail("%d profiles set kanban.dispatch_in_gateway: true (%s); choose the one "
               "that runs the dispatcher with --profile NAME"
               % (len(motors), ", ".join(motors)))


def upstream_gateways(agents_dir):
    if not agents_dir.is_dir():
        return []
    return sorted(p.name[:-len(".plist")] for p in agents_dir.glob("ai.hermes.gateway*.plist"))


def prepare(job, a):
    """Preconditions for one job; returns the placeholder values. Raises Fail."""
    home = a.hermes_home
    vbin = home / "hermes-agent" / "venv" / "bin"
    values = {"__HERMES_HOME__": str(home), "__HERMES_WORKFLOWS_DIR__": str(REPO),
              "__BOARD__": a.board, "__PYTHON3__": str(vbin / "python3")}
    if job in ("gateway", "escalator", "bridge") and not (vbin / "python").exists():
        raise Fail("hermes-agent not installed: no python in %s" % vbin)
    if job == "gateway":
        values["__PROFILE__"] = motor_profile(home, a.profile)
        # `hermes gateway install` (upstream) registers ai.hermes.gateway*. Two
        # KeepAlive gateways each started with --replace kill each other forever.
        for other in upstream_gateways(a.agents_dir):
            if live_environment(other) is not None:
                raise Fail("%s is already loaded (upstream `hermes gateway install`); "
                           "run one gateway: `launchctl bootout %s/%s` first, or keep "
                           "that one and skip this installer's gateway" % (other, domain(), other))
    if job == "escalator" and not a.check:
        # TCC blocks a launchd job from reading under ~/Documents: copy, not link.
        dest = home / "launchd-bin"
        dest.mkdir(parents=True, exist_ok=True)
        for src in sorted((REPO / "scripts" / "resilience").glob("*.py")):
            target = dest / src.name
            if not target.exists() or target.read_bytes() != src.read_bytes():
                shutil.copy2(src, target)
    if job == "bridge":
        if not a.project_dir:
            raise Fail("the bridge needs --project-dir DIR (the project it serves)")
        values["__PROJECT_DIR__"] = str(Path(a.project_dir).expanduser().resolve())
    return values


def render(job, values):
    text = (REPO / "launchd" / ("%s%s.plist.example" % (PREFIX, job))).read_text()
    for k, v in values.items():
        text = text.replace(k, v)
    # The examples' comments carry "--flag" text, which is not well-formed XML
    # inside a comment. The installed file is the example without its comments,
    # plus one line saying where it came from; the example stays the reference.
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    body = re.sub(r"\n\s*\n+", "\n", body)
    text = body.replace("<plist ", "<!-- Generated by scripts/install.py from launchd/%s%s.plist.example."
                        " Edit the example and re-run the installer. -->\n<plist " % (PREFIX, job), 1)
    pl = plistlib.loads(text.encode())
    left = placeholders(pl)
    if left:
        raise Fail("unfilled placeholder(s) in the example: %s" % ", ".join(sorted(set(left))))
    return text, pl


def install(job, a):
    """Returns (status, detail, ok)."""
    label = PREFIX + job
    try:
        values = prepare(job, a)
        text, pl = render(job, values)
    except Fail as e:
        return "FAILED", str(e), False
    who = "profile %s; " % values["__PROFILE__"] if job == "gateway" else ""
    keepalive = bool(pl.get("KeepAlive"))

    def dead(reason):
        tail = stderr_tail(pl)
        return "FAILED", who + reason + (" -- stderr: %s" % tail if tail else ""), False

    want = pl.get("EnvironmentVariables", {})
    path = a.agents_dir / (label + ".plist")
    on_disk = path.read_text() if path.exists() else None
    live = live_environment(label)

    if a.check:
        if on_disk is None or live is None:
            return "missing", "not %s" % ("on disk" if on_disk is None else "loaded"), False
        if on_disk != text:
            return "stale", "%s differs from the example" % path, False
        d = drift(want, live)
        if d:
            return "drift", "live environment differs: %s" % ", ".join(d), False
        bad = health(label, keepalive)
        if bad:
            return dead(bad)
        return "ok", who + "alive, environment verified", True

    if on_disk != text:
        status, why = ("registered", "") if on_disk is None else ("updated", "plist rewritten")
        a.agents_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        reload = True
    elif live is None:
        status, why, reload = "registered", "was on disk but not loaded", True
    elif drift(want, live):
        status, why, reload = "reloaded", "live environment had drifted: %s" % ", ".join(drift(want, live)), True
    else:
        status, why, reload = "unchanged", "", False

    if reload:
        if live is not None:
            launchctl("bootout", "%s/%s" % (domain(), label))
        # A bootout can take a moment to finish; bootstrap then says "5: I/O error".
        for attempt in range(5):
            r = launchctl("bootstrap", domain(), str(path))
            if r.returncode == 0 or live is None:
                break
            time.sleep(1)
        if r.returncode != 0:
            return "FAILED", "not loaded: bootstrap said %s" % (r.stderr.strip() or r.returncode), False
        # Give a job that dies on start the time to die before calling it alive.
        time.sleep(float(os.environ.get("HERMES_INSTALL_SETTLE", "8")))

    live = live_environment(label)
    if live is None:
        return "FAILED", "not loaded after bootstrap", False
    d = drift(want, live)
    if d:
        return "FAILED", "loaded, but live environment differs: %s" % ", ".join(d), False
    bad = health(label, keepalive)
    if bad:
        return dead(bad)
    return status, who + ("%s; " % why if why else "") + "alive, environment verified", True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--hermes-home", type=Path,
                   default=Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser())
    p.add_argument("--agents-dir", type=Path,
                   default=Path("~/Library/LaunchAgents").expanduser())
    p.add_argument("--profile",
                   help="profile whose gateway runs the dispatcher (default: the one profile "
                        "that sets kanban.dispatch_in_gateway: true; refused if none or several)")
    p.add_argument("--board", default="default")
    p.add_argument("--with", dest="extra", action="append", default=[],
                   choices=OPTIONAL, help="also install an optional job")
    p.add_argument("--project-dir", help="project the bridge serves (with --with bridge)")
    p.add_argument("--check", action="store_true",
                   help="report only: write nothing, load nothing")
    a = p.parse_args(argv)
    a.hermes_home = a.hermes_home.expanduser().resolve()
    a.agents_dir = a.agents_dir.expanduser()

    ok_all = True
    print("hermes-workflows install  HERMES_HOME=%s  LaunchAgents=%s%s"
          % (a.hermes_home, a.agents_dir, "  (check only)" if a.check else ""))
    for job in REQUIRED + OPTIONAL:
        label = PREFIX + job
        if job in OPTIONAL and job not in a.extra:
            print("  %-10s %-32s optional; add --with %s to install it" % ("skipped", label, job))
            continue
        status, detail, ok = install(job, a)
        ok_all &= ok
        print("  %-10s %-32s %s" % (status, label, detail))
    for other in upstream_gateways(a.agents_dir):
        if live_environment(other) is None:
            print("  note: %s.plist is on disk but not loaded; leave it unloaded while "
                  "%sgateway runs" % (other, PREFIX))
    if not a.hermes_home.joinpath("resilience.yaml").is_file():
        print("  note: %s is missing; the escalator runs on defaults"
              % a.hermes_home.joinpath("resilience.yaml"))
    print("result: %s" % ("loop installed and verified" if ok_all
                          else "NOT a working loop -- see the lines above"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
