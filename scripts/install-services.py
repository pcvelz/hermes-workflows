#!/usr/bin/env python3
"""install-services.py — render and register the launchd services under services/.

Reads services/services.yaml (or services/services.yaml.example when the real
file is absent). For each service with `launchd: true`:

  1. copies its payload (and keyfile.py for python payloads) into
     $HERMES_HOME/launchd-bin. launchd may not execute files under ~/Documents
     (macOS TCC), so the job always runs a copy.
  2. checks each key file named under `keys:` exists and is non-empty, then
     copies it into $HERMES_HOME/launchd-keys/<service>/<name> (mode 0600).
  3. renders launchd/service.plist.tmpl.example with the service's settings
     merged in, and writes it to ~/Library/LaunchAgents/<label>.plist.
  4. bootout + bootstrap when needed, then verifies the LOADED job with
     `launchctl print`: environment matches the plist, and the job is alive.

--dry-run renders everything into a temporary directory and never writes under
the real HERMES_HOME or LaunchAgents and never calls launchctl. Key files are
still read (and must exist), so a dry run also proves the keys resolve.

Exit 0 only when every launchd service is installed and verified (or rendered,
in a dry run). The launchctl helpers are shared with scripts/install.py.
"""
import argparse
import os
import plistlib
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parents[1]
SERVICES = REPO / "services"
SHARED = SERVICES / "_shared"
TEMPLATE = REPO / "launchd" / "service.plist.tmpl.example"

sys.path.insert(0, str(SHARED))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import yaml
except ImportError:
    print("install-services: PyYAML is required (python3 -m pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

import keyfile  # noqa: E402
import install as base  # noqa: E402  (launchctl helpers shared with scripts/install.py)

PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")
LABEL_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class Fail(Exception):
    pass


def load_config(path):
    """(label_prefix, services) from the YAML file. Raises Fail."""
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise Fail(f"cannot read {path}: {type(exc).__name__}")
    prefix = str(cfg.get("label_prefix") or "")
    if not LABEL_PREFIX.match(prefix):
        raise Fail(f"{path}: label_prefix is missing or not a valid launchd label prefix")
    services = cfg.get("services") or {}
    if not isinstance(services, dict) or not services:
        raise Fail(f"{path}: no services defined")
    for name, svc in services.items():
        if not SERVICE_NAME.match(str(name)):
            raise Fail(f"{path}: service name {name!r} must be lowercase letters, digits and dashes")
        if not isinstance(svc, dict):
            raise Fail(f"{path}: service {name} must be a mapping")
        if not (SERVICES / str(name)).is_dir():
            raise Fail(f"services/{name}/ does not exist (service {name} is listed in {path.name})")
    return prefix, services


def interpreter(name, svc, hermes_home, override):
    kind = svc.get("interpreter", "bash")
    if kind == "bash":
        return "/bin/bash"
    if kind == "python":
        return override or str(hermes_home / "hermes-agent" / "venv" / "bin" / "python3")
    raise Fail(f"{name}: interpreter must be 'bash' or 'python', not {kind!r}")


def stage_payload(name, svc, out_home):
    """Copy the payload (and keyfile.py for python) into launchd-bin. Returns the copy."""
    rel = svc.get("payload")
    src = SERVICES / name / str(rel or "")
    if not rel or not src.is_file():
        raise Fail(f"{name}: payload services/{name}/{rel} not found")
    bindir = out_home / "launchd-bin"
    bindir.mkdir(parents=True, exist_ok=True)
    dest = bindir / src.name
    shutil.copy2(src, dest)
    if svc.get("interpreter") == "python":
        shutil.copy2(SHARED / "keyfile.py", bindir / "keyfile.py")
    return dest


def stage_keys(name, svc, out_home):
    """Validate and copy each key under `keys:`. Returns the KEY_<NAME> env map.

    Only paths reach the plist. A value is read here to prove the key is
    present and non-empty, then left in its file; it is never printed.
    """
    env = {}
    for logical, ref in (svc.get("keys") or {}).items():
        try:
            service, key = keyfile.check_ref(str(ref))
            keyfile.read_key(str(ref))
        except keyfile.KeyFileError as exc:
            raise Fail(f"{name}: {exc}")
        if service != name:
            raise Fail(f"{name}: key {ref} must live under keys/{name}/")
        dest_dir = out_home / "launchd-keys" / name
        dest_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(dest_dir, 0o700)
        dest = dest_dir / key
        shutil.copyfile(keyfile.key_file(str(ref)), dest)
        os.chmod(dest, 0o600)
        env["KEY_" + re.sub(r"[^A-Z0-9]", "_", str(logical).upper())] = str(ref)
    return env


def render(name, svc, label, hermes_home, out_home, program, payload, key_env, log):
    """(plist_xml_text, plist_dict) from the template plus this service's settings."""
    text = TEMPLATE.read_text(encoding="utf-8")
    values = {
        "__LABEL__": label,
        "__PROGRAM__": program,
        "__PAYLOAD__": str(payload),
        "__HERMES_HOME__": str(hermes_home),
        "__HERMES_KEYS_DIR__": str(out_home / "launchd-keys"),
        "__LOG__": str(log),
    }
    for key, value in values.items():
        text = text.replace(key, escape(value))
    pl = plistlib.loads(text.encode("utf-8"))
    pl["Label"] = label
    pl["ProgramArguments"] = [program, str(payload)] + [str(x) for x in (svc.get("args") or [])]
    env = dict(pl.get("EnvironmentVariables", {}))
    env.update({str(k): str(v) for k, v in (svc.get("env") or {}).items()})
    env.update(key_env)
    pl["EnvironmentVariables"] = env
    pl["RunAtLoad"] = bool(svc.get("run_at_load", True))
    pl["KeepAlive"] = bool(svc.get("keep_alive", False))
    if "start_interval" in svc:
        pl["StartInterval"] = int(svc["start_interval"])
    if svc.get("watch_profiles"):
        pl["WatchPaths"] = [str(hermes_home / "config.yaml")] + [
            str(hermes_home / "profiles" / str(p) / "config.yaml") for p in svc["watch_profiles"]
        ]
    body = plistlib.dumps(pl, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")
    left = sorted(set(PLACEHOLDER.findall(body)))
    if left:
        raise Fail(f"{name}: unfilled placeholder(s) in the template: {', '.join(left)}")
    return body, pl


def install_service(name, svc, a):
    """(status, detail, ok). Raises Fail on a precondition or a failed verification."""
    label = f"{a.prefix}.{name}"
    program = interpreter(name, svc, a.hermes_home, a.python)
    note = ""
    if not Path(program).is_file():
        if not a.dry_run:
            raise Fail(f"{name}: interpreter {program} not found (install hermes-agent first, or pass --python)")
        note = f"interpreter {program} absent (a real install would refuse); "
    payload = stage_payload(name, svc, a.out_home)
    key_env = stage_keys(name, svc, a.out_home)
    log = a.out_home / "logs" / f"{label}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    body, pl = render(name, svc, label, a.hermes_home, a.out_home, program, payload, key_env, log)
    a.agents_dir.mkdir(parents=True, exist_ok=True)
    path = a.agents_dir / f"{label}.plist"

    if a.dry_run:
        path.write_text(body, encoding="utf-8")
        return "rendered", f"{note}{path}", True

    want = pl["EnvironmentVariables"]
    on_disk = path.read_text(encoding="utf-8") if path.exists() else None
    live = base.live_environment(label)
    if on_disk == body and live is not None and not base.drift(want, live):
        status, why, reload = "unchanged", "", False
    else:
        if on_disk is None:
            status, why = "registered", ""
        elif on_disk != body:
            status, why = "updated", "plist rewritten"
        elif live is None:
            status, why = "registered", "was on disk but not loaded"
        else:
            status, why = "reloaded", "live environment drifted: " + ", ".join(base.drift(want, live))
        path.write_text(body, encoding="utf-8")
        reload = True

    if reload:
        if live is not None:
            base.launchctl("bootout", f"{base.domain()}/{label}")
        # A bootout can take a moment; bootstrap then fails with "5: I/O error".
        for _ in range(5):
            r = base.launchctl("bootstrap", base.domain(), str(path))
            if r.returncode == 0:
                break
            time.sleep(1)
        if r.returncode != 0:
            raise Fail(f"not loaded: bootstrap said {r.stderr.strip() or r.returncode}")
        time.sleep(float(os.environ.get("SERVICES_INSTALL_SETTLE", "8")))

    live = base.live_environment(label)
    if live is None:
        raise Fail("not loaded after bootstrap")
    d = base.drift(want, live)
    if d:
        raise Fail(f"loaded, but live environment differs: {', '.join(d)}")
    bad = base.health(label, bool(pl.get("KeepAlive")))
    if bad:
        tail = base.stderr_tail(pl)
        raise Fail(bad + (f" -- stderr: {tail}" if tail else ""))
    why_txt = f"{why}; " if why else ""
    return status, f"{note}{why_txt}alive, environment verified", True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--services-config", type=Path,
                   help="YAML to use (default: services/services.yaml if present, else services/services.yaml.example)")
    p.add_argument("--hermes-home", type=Path,
                   default=Path(os.environ.get("HERMES_HOME") or "~/.hermes"),
                   help="Hermes home (default: $HERMES_HOME or ~/.hermes)")
    p.add_argument("--agents-dir", type=Path, default=Path("~/Library/LaunchAgents"))
    p.add_argument("--only", action="append", default=[], metavar="SERVICE",
                   help="install only this service (repeatable)")
    p.add_argument("--python", help="interpreter for python services "
                                    "(default: <hermes-home>/hermes-agent/venv/bin/python3)")
    p.add_argument("--dry-run", action="store_true",
                   help="render into a temporary directory; never write under the real "
                        "HERMES_HOME or LaunchAgents and never call launchctl")
    a = p.parse_args(argv)

    cfg = a.services_config
    if cfg is None:
        real = SERVICES / "services.yaml"
        cfg = real if real.is_file() else SERVICES / "services.yaml.example"
    try:
        a.prefix, services = load_config(cfg)
    except Fail as exc:
        print(f"install-services: {exc}", file=sys.stderr)
        return 1
    unknown = [n for n in a.only if n not in services]
    if unknown:
        print(f"install-services: unknown service(s): {', '.join(unknown)}", file=sys.stderr)
        return 1

    a.hermes_home = a.hermes_home.expanduser().resolve()
    if a.dry_run:
        tmp = Path(tempfile.mkdtemp(prefix="hermes-services-dry-run-"))
        a.out_home = tmp
        a.agents_dir = tmp / "LaunchAgents"
    else:
        a.out_home = a.hermes_home
        a.agents_dir = a.agents_dir.expanduser()

    print(f"install-services  config={cfg}  HERMES_HOME={a.hermes_home}  "
          f"LaunchAgents={a.agents_dir}{'  (dry run)' if a.dry_run else ''}")
    if a.dry_run:
        print(f"dry-run output: {tmp}")

    ok_all = True
    for name, svc in services.items():
        if a.only and name not in a.only:
            continue
        if not svc.get("launchd"):
            print(f"  {'manual':<10} {name:<26} not a launchd service; run by hand "
                  f"(services/{name}/README.md)")
            continue
        label = f"{a.prefix}.{name}"
        try:
            status, detail, ok = install_service(name, svc, a)
        except Fail as exc:
            status, detail, ok = "FAILED", str(exc), False
        ok_all &= ok
        print(f"  {status:<10} {label:<44} {detail}")

    if a.dry_run:
        print(f"result: {'dry run rendered cleanly' if ok_all else 'dry run FAILED -- see the lines above'}")
    else:
        print(f"result: {'services installed and verified' if ok_all else 'NOT all services verified -- see the lines above'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
