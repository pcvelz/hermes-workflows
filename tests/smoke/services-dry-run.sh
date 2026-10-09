#!/usr/bin/env bash
# =============================================================================
# tests/smoke/services-dry-run.sh — scripts/install-services.py --dry-run.
#
# Renders services/services.yaml.example against a THROWAWAY HOME with DUMMY key
# files. Touches no real HERMES_HOME, no LaunchAgents, no launchctl. Proves:
#   - the example config renders all launchd services with exit 0;
#   - every plist parses, has no unfilled __X__ placeholder, and points at the
#     staged payload under launchd-bin;
#   - key files are copied into launchd-keys at mode 0600;
#   - a missing key fails the run and names its key path;
#   - no key VALUE reaches a rendered plist or the installer output;
#   - the new files carry no /Users path and no personal launchd label.
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "SMOKE: services dry-run render"

_sv_py="$(pick_python || true)"
if [ -z "$_sv_py" ]; then
  skip "services dry-run — no python interpreter found (set \$PYTHON)"
else
  _sv_repo="$REPO_ROOT"
  _sv_tmp="$(mktemp -d)"
  _sv_home="$_sv_tmp/home"
  _sv_hermes="$_sv_home/.hermes"
  _sv_keys="$_sv_tmp/keys"
  _sv_out="$_sv_tmp/run.txt"
  _sv_dirs="$_sv_tmp/dirs.txt"
  mkdir -p "$_sv_hermes" "$_sv_keys/kanban-ntfy-notifier"
  # Dummy values only. The 'dummy-' marker lets the test prove no value leaks.
  printf '%s\n' 'https://ntfy.dummy.invalid' >"$_sv_keys/kanban-ntfy-notifier/server"
  printf '%s\n' 'dummy-topic-smoke' >"$_sv_keys/kanban-ntfy-notifier/topic"
  printf '%s\n' 'dummy-account-smoke' >"$_sv_keys/kanban-ntfy-notifier/keychain-account"
  printf '%s\n' 'dummy-service-smoke' >"$_sv_keys/kanban-ntfy-notifier/keychain-service"

  _sv_run() {  # _sv_run <outfile>; uses the throwaway HOME and dummy keys
    HOME="$_sv_home" HERMES_HOME="$_sv_hermes" HERMES_KEYS_DIR="$_sv_keys" \
      "$_sv_py" "$_sv_repo/scripts/install-services.py" --dry-run \
      --services-config "$_sv_repo/services/services.yaml.example" >"$1" 2>&1
  }

  # --- 1. render -------------------------------------------------------------
  _sv_run "$_sv_out"
  _sv_rc=$?
  if [ "$_sv_rc" -eq 0 ]; then
    pass "dry run exits 0 against services/services.yaml.example"
  else
    fail "dry run exited $_sv_rc — $(tr '\n' ' ' <"$_sv_out")"
  fi
  _sv_dry="$(sed -n 's/^dry-run output: //p' "$_sv_out" | head -1)"
  [ -n "$_sv_dry" ] && printf '%s\n' "$_sv_dry" >>"$_sv_dirs"

  if [ -n "$_sv_dry" ] && [ -d "$_sv_dry" ]; then
    for _sv_name in cdp-chrome-service config-guard kanban-ntfy-notifier; do
      _sv_plist="$_sv_dry/LaunchAgents/org.example.hermes.$_sv_name.plist"
      if [ -f "$_sv_plist" ]; then
        pass "rendered $_sv_name plist"
      else
        fail "no rendered plist for $_sv_name at $_sv_plist"
      fi
    done
    if grep -q '^  manual .*composio-gmail-mcp' "$_sv_out"; then
      pass "composio-gmail-mcp is listed as manual (not a launchd job)"
    else
      fail "composio-gmail-mcp not reported as manual"
    fi

    # --- 2. plist validity (macOS only) ------------------------------------
    if os_is_linux || ! have plutil; then
      skip "plutil -lint on rendered plists — not on macOS"
    else
      for _sv_p in "$_sv_dry"/LaunchAgents/*.plist; do
        if plutil -lint "$_sv_p" >/dev/null 2>&1; then
          pass "plutil $(basename "$_sv_p")"
        else
          fail "plutil $(basename "$_sv_p") is not well-formed"
        fi
      done
    fi

    # --- 3. structure, placeholders, keys and leaks -------------------------
    if "$_sv_py" -I - "$_sv_dry" "$_sv_hermes" <<'PYEOF'
import glob, os, plistlib, re, stat, sys
dry, hermes = sys.argv[1], sys.argv[2]
bad = []
def need(cond, msg):
    if not cond:
        bad.append(msg)

agents = os.path.join(dry, "LaunchAgents")
for p in sorted(glob.glob(os.path.join(agents, "*.plist"))):
    raw = open(p, "rb").read()
    text = raw.decode("utf-8", "replace")
    need(not re.search(r"__[A-Z0-9_]+__", text), f"{os.path.basename(p)}: unfilled placeholder")
    need("dummy-" not in text, f"{os.path.basename(p)}: a key VALUE leaked into the plist")
    need("ntfy.dummy.invalid" not in text, f"{os.path.basename(p)}: the ntfy server VALUE leaked")
    pl = plistlib.loads(raw)
    need(pl["Label"].startswith("org.example.hermes."), f"{pl['Label']}: label prefix wrong")
    prog_path = pl["ProgramArguments"][1]
    need(prog_path.startswith(os.path.join(dry, "launchd-bin")), f"{pl['Label']}: payload not under launchd-bin")
    need(os.path.isfile(prog_path), f"{pl['Label']}: staged payload missing: {prog_path}")
    env = pl["EnvironmentVariables"]
    need(os.path.realpath(env.get("HERMES_HOME", "")) == os.path.realpath(hermes),
         f"{pl['Label']}: HERMES_HOME env wrong")
    need(env.get("HERMES_KEYS_DIR") == os.path.join(dry, "launchd-keys"), f"{pl['Label']}: HERMES_KEYS_DIR wrong")

    name = pl["Label"].rsplit(".", 1)[1]
    if name == "cdp-chrome-service":
        need(pl["ProgramArguments"][0] == "/bin/bash", "cdp: interpreter must be /bin/bash")
        need(pl.get("KeepAlive") is True, "cdp: KeepAlive must be true")
        need(env.get("CDP_PORT") == "9222", "cdp: CDP_PORT not rendered")
    elif name == "config-guard":
        watch = pl.get("WatchPaths", [])
        need(len(watch) == 3, f"config-guard: expected 3 WatchPaths, got {len(watch)}")
        need(any(w.endswith(os.path.join("profiles", "coding", "config.yaml")) for w in watch),
             "config-guard: coding profile not in WatchPaths")
        need(any(w.endswith(os.path.join("profiles", "private", "config.yaml")) for w in watch),
             "config-guard: private profile not in WatchPaths")
        need(pl["ProgramArguments"][0].endswith(os.path.join("venv", "bin", "python3")),
             "config-guard: interpreter must be the venv python3")
    elif name == "kanban-ntfy-notifier":
        need(pl.get("StartInterval") == 60, "notifier: StartInterval must be 60")
        need(env.get("KEY_TOPIC") == "keys/kanban-ntfy-notifier/topic", "notifier: KEY_TOPIC ref wrong")
        need(env.get("KEY_KEYCHAIN_SERVICE") == "keys/kanban-ntfy-notifier/keychain-service",
             "notifier: KEY_KEYCHAIN_SERVICE ref wrong")
        need(os.path.isfile(os.path.join(dry, "launchd-bin", "keyfile.py")), "notifier: keyfile.py not staged")

# Key copies: present, and mode 0600.
for k in ("server", "topic", "keychain-account", "keychain-service"):
    kp = os.path.join(dry, "launchd-keys", "kanban-ntfy-notifier", k)
    need(os.path.isfile(kp), f"key copy missing: {k}")
    if os.path.isfile(kp):
        mode = stat.S_IMODE(os.stat(kp).st_mode)
        need(mode == 0o600, f"key copy {k} mode is {oct(mode)}, want 0o600")

for f in ("cdp-chrome-service/start-cdp-chrome.sh", "config-guard/check-configs.py"):
    need(os.path.isfile(os.path.join(dry, "launchd-bin", os.path.basename(f))), f"payload not staged: {f}")

if bad:
    print("\n".join(bad))
    sys.exit(1)
PYEOF
    then
      pass "rendered plists: structure, placeholders, keys, no value leaks"
    else
      fail "rendered plist checks failed (see the python output above)"
    fi
  else
    fail "no dry-run output directory reported"
  fi

  # --- 4. a missing key fails the run and names its path ----------------------
  mv "$_sv_keys/kanban-ntfy-notifier/topic" "$_sv_keys/kanban-ntfy-notifier/topic.moved"
  _sv_miss="$_sv_tmp/missing.txt"
  _sv_run "$_sv_miss"
  _sv_mrc=$?
  mv "$_sv_keys/kanban-ntfy-notifier/topic.moved" "$_sv_keys/kanban-ntfy-notifier/topic"
  _sv_mdir="$(sed -n 's/^dry-run output: //p' "$_sv_miss" | head -1)"
  [ -n "$_sv_mdir" ] && printf '%s\n' "$_sv_mdir" >>"$_sv_dirs"
  if [ "$_sv_mrc" -ne 0 ] && grep -q 'keys/kanban-ntfy-notifier/topic' "$_sv_miss"; then
    pass "missing key fails the run and names keys/kanban-ntfy-notifier/topic"
  else
    fail "missing key did not fail by name (rc=$_sv_mrc) — $(tr '\n' ' ' <"$_sv_miss")"
  fi

  # --- 5. installer output carries no key value -------------------------------
  if grep -q 'dummy-' "$_sv_out" "$_sv_miss"; then
    fail "installer output contains a key value"
  else
    pass "installer output contains no key value"
  fi

  # --- 6. the real fallback: no services.yaml -> reads the .example -----------
  if [ ! -f "$_sv_repo/services/services.yaml" ]; then
    _sv_fb="$_sv_tmp/fallback.txt"
    HOME="$_sv_home" HERMES_HOME="$_sv_hermes" HERMES_KEYS_DIR="$_sv_keys" \
      "$_sv_py" "$_sv_repo/scripts/install-services.py" --dry-run >"$_sv_fb" 2>&1
    _sv_fbdir="$(sed -n 's/^dry-run output: //p' "$_sv_fb" | head -1)"
    [ -n "$_sv_fbdir" ] && printf '%s\n' "$_sv_fbdir" >>"$_sv_dirs"
    if grep -q 'services/services.yaml.example' "$_sv_fb"; then
      pass "without services/services.yaml the installer reads the .example"
    else
      fail "installer did not fall back to the .example"
    fi
  else
    skip "fallback-to-.example check — a real services/services.yaml exists"
  fi

  # --- 7. public-safety: new files carry no personal paths or labels ----------
  _sv_leak=""
  for _sv_f in services launchd/service.plist.tmpl.example scripts/install-services.py docs/services.md; do
    [ -e "$_sv_repo/$_sv_f" ] || continue
    if grep -rIl -e '/Users/' -e 'com\.peter' "$_sv_repo/$_sv_f" >/dev/null 2>&1; then
      _sv_leak="$_sv_leak $_sv_f"
    fi
  done
  if [ -z "$_sv_leak" ]; then
    pass "new files are public-safe (no /Users path, no com.peter label)"
  else
    fail "personal path or label in:$_sv_leak"
  fi

  # --- cleanup: only the directories this run created ------------------------
  while IFS= read -r _sv_d; do
    case "$_sv_d" in */hermes-services-dry-run-*) rm -rf "$_sv_d" ;; esac
  done <"$_sv_dirs"
  rm -rf "$_sv_tmp"
fi
