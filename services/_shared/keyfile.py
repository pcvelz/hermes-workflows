"""keyfile.py — read account-bound values from key files (stdlib only).

Every account-bound value (a secret, an account / server / channel id, a host,
a keychain item name) lives in ONE file at the fixed path

    keys/<service>/<name>

relative to the repo root. Setting HERMES_KEYS_DIR replaces the `keys/`
directory itself, so the same reference resolves to $HERMES_KEYS_DIR/<service>/<name>.
Configs and plists carry only the reference string, never the value.

read_key() strips surrounding whitespace and returns the value. A missing or
empty key raises KeyFileError naming the key path. Values are never logged.
"""
import os
import re

KEY_RE = re.compile(r"^keys/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class KeyFileError(RuntimeError):
    pass


def check_ref(ref):
    """Validate a key reference and return (service, name). Raises KeyFileError."""
    m = KEY_RE.match(ref or "")
    if not m or m.group(1) in (".", "..") or m.group(2) in (".", ".."):
        raise KeyFileError(f"invalid key reference {ref!r}: expected keys/<service>/<name>")
    return m.group(1), m.group(2)


def keys_root():
    """The directory that stands in for `keys/`: $HERMES_KEYS_DIR, else <repo>/keys."""
    override = os.environ.get("HERMES_KEYS_DIR", "").strip()
    return os.path.expanduser(override) if override else os.path.join(_REPO_ROOT, "keys")


def key_file(ref):
    """Absolute path a key reference resolves to (the file may not exist)."""
    service, name = check_ref(ref)
    return os.path.join(keys_root(), service, name)


def read_key(ref):
    """Return the stripped value of the key file at `ref`.

    Raises KeyFileError naming the key path when the file is missing or empty.
    """
    path = key_file(ref)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        raise KeyFileError(
            f"missing key {ref} (looked for {path}). Create that file with the value, "
            "or set HERMES_KEYS_DIR to the directory that holds keys/."
        )
    if not value:
        raise KeyFileError(f"key {ref} is empty ({path})")
    return value


def read_key_env(var, default_ref):
    """Key named by env var `var` (a key reference), else `default_ref`."""
    return read_key(os.environ.get(var, "").strip() or default_ref)
