#!/usr/bin/env python3
"""check-backends.py — verify profile configs match config/backends.yaml.

config/backends.yaml is the single source of truth for which backend each
Hermes profile uses: its model, the llama-swap tier it rides (or a remote
endpoint), and optionally its delegation target. This script resolves every
profile's tier to a URL and compares it with the profile's config.yaml:

  model.default               == the profile's model
  model.base_url              == the tier URL (remote: same host)
  delegation.model/base_url   == the listed delegation (only when listed)

A trailing /v1 and a trailing slash are ignored when comparing URLs. Profiles
without a config.yaml are warned about and skipped. Top-level keys other than
`tiers` and `profiles` (e.g. a bridge block) are ignored.

Exit status: 0 all match, 1 mismatch, 2 backends.yaml missing or invalid.

Usage:
  python3 scripts/check-backends.py [BACKENDS_YAML] [--profiles-dir DIR]
  Defaults: config/backends.yaml, and <backends dir>/profiles.
"""
import argparse
import os
import sys
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is expected to be present
    sys.exit("check-backends: PyYAML is required (pip install pyyaml)")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def norm_url(url):
    u = str(url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[: -len("/v1")]
    return u


def host_of(url):
    return urlparse(str(url or "").strip()).netloc.lower()


def expected(spec, tiers):
    """(url, is_remote) for a backend spec: a remote endpoint or a tier URL."""
    if "remote" in spec:
        return spec["remote"], True
    return tiers[spec["tier"]], False


def describe(spec):
    return f"remote {spec['remote']}" if "remote" in spec else f"tier {spec['tier']}"


def same_endpoint(want, got, is_remote):
    if is_remote:
        return bool(host_of(want)) and host_of(want) == host_of(got)
    return norm_url(want) == norm_url(got)


def check_backend(where, spec, tiers, errors):
    if not isinstance(spec, dict):
        errors.append(f"{where}: must be a mapping")
    elif ("tier" in spec) == ("remote" in spec):
        errors.append(f"{where}: needs exactly one of `tier` or `remote`")
    elif "tier" in spec and spec["tier"] not in tiers:
        errors.append(f"{where}: unknown tier {spec['tier']!r}")


def validate(backends):
    """Return a list of human-readable errors; empty means usable."""
    tiers = backends.get("tiers")
    if not isinstance(tiers, dict) or not tiers:
        return ["`tiers` must be a non-empty mapping of tier name -> URL"]
    profiles = backends.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        return ["`profiles` must be a non-empty mapping"]
    errors = []
    for name, spec in profiles.items():
        where = f"profiles.{name}"
        if not isinstance(spec, dict) or not spec.get("model"):
            errors.append(f"{where}: needs a `model`")
            continue
        check_backend(where, spec, tiers, errors)
        if "delegation" in spec:
            d = spec["delegation"]
            if not isinstance(d, dict) or not d.get("model"):
                errors.append(f"{where}.delegation: needs a `model`")
            else:
                check_backend(f"{where}.delegation", d, tiers, errors)
    return errors


def check_profile(name, spec, tiers, cfg):
    """Diff lines for one profile against its parsed config.yaml."""
    diffs = []
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    url, is_remote = expected(spec, tiers)
    if str(model_cfg.get("default")) != str(spec["model"]):
        diffs.append(
            f"{name}: model.default  backend={spec['model']!r}  "
            f"config={model_cfg.get('default')!r}"
        )
    if not same_endpoint(url, model_cfg.get("base_url"), is_remote):
        diffs.append(
            f"{name}: model.base_url  backend={url!r} ({describe(spec)})  "
            f"config={model_cfg.get('base_url')!r}"
        )
    if "delegation" in spec:
        d = spec["delegation"]
        dcfg = cfg.get("delegation") if isinstance(cfg.get("delegation"), dict) else {}
        durl, d_remote = expected(d, tiers)
        if str(dcfg.get("model")) != str(d["model"]):
            diffs.append(
                f"{name}: delegation.model  backend={d['model']!r}  "
                f"config={dcfg.get('model')!r}"
            )
        if not same_endpoint(durl, dcfg.get("base_url"), d_remote):
            diffs.append(
                f"{name}: delegation.base_url  backend={durl!r} ({describe(d)})  "
                f"config={dcfg.get('base_url')!r}"
            )
    return diffs


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check profile configs against backends.yaml.")
    ap.add_argument(
        "backends",
        nargs="?",
        default=os.path.join(REPO_ROOT, "config", "backends.yaml"),
        help="path to backends.yaml (default: config/backends.yaml)",
    )
    ap.add_argument(
        "--profiles-dir",
        help="directory holding <profile>/config.yaml (default: <backends dir>/profiles)",
    )
    args = ap.parse_args(argv)
    backends_path = os.path.abspath(args.backends)
    profiles_dir = os.path.abspath(
        args.profiles_dir or os.path.join(os.path.dirname(backends_path), "profiles")
    )

    try:
        with open(backends_path, "r", encoding="utf-8") as f:
            backends = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"check-backends: cannot load {backends_path}: {exc}", file=sys.stderr)
        return 2
    errors = validate(backends) if isinstance(backends, dict) else ["document must be a mapping"]
    if errors:
        print(f"check-backends: {backends_path} is invalid:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    tiers = backends["tiers"]
    diffs, checked, skipped = [], 0, 0
    for name, spec in backends["profiles"].items():
        path = os.path.join(profiles_dir, name, "config.yaml")
        if not os.path.isfile(path):
            print(f"check-backends: WARN — no {path}; profile '{name}' skipped", file=sys.stderr)
            skipped += 1
            continue
        checked += 1
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as exc:
            diffs.append(f"{name}: config.yaml does not parse ({exc})")
            continue
        if not isinstance(cfg, dict):
            cfg = {}
        diffs.extend(check_profile(name, spec, tiers, cfg))

    if diffs:
        print(f"check-backends: MISMATCH — {len(diffs)} difference(s) between {backends_path} and {profiles_dir}")
        for d in diffs:
            print(f"  - {d}")
        return 1
    print(f"check-backends: OK — {checked} profile(s) match {backends_path} ({skipped} skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
