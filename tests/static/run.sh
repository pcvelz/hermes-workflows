#!/usr/bin/env bash
# =============================================================================
# tests/static/run.sh — STATIC layer.
#
# No network, no model, no live install. Proves every committed artifact is
# syntactically valid and internally consistent:
#   1. bash -n on every repo *.sh / *.example.sh
#   2. python -m py_compile on every repo *.py
#   3. import hooks/per-profile-dispatcher/handler.py (catches import errors)
#   4. YAML validity (yaml.safe_load) on every *.yaml / *.yaml.example
#   5. JSON validity on cron/jobs.json.example (+ any *.json / *.json.example)
#   6. plutil -lint on launchd plists (SKIP on Linux / when plutil absent)
#   7. docker compose config -q on docker/docker-compose.yml (SKIP if no docker)
#   8. relative-markdown-link existence check across all docs
#
# Exit status: non-zero if any [FAIL] occurred. Missing optional tools -> SKIP.
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

cd "$REPO_ROOT"

PY="$(pick_python || true)"

# Files we never want to scan (caches, vcs).
_PRUNE=( -name .git -o -name __pycache__ -o -name node_modules )

# Collect repo files of a given pattern, excluding pruned dirs. NUL-safe.
collect() {  # collect <find-args...>
  find . \( "${_PRUNE[@]}" \) -prune -o -type f \( "$@" \) -print
}

# ---------------------------------------------------------------------------
section "1. bash -n — shell syntax"
# ---------------------------------------------------------------------------
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if bash -n "$f" 2>/tmp/_bashn.$$; then
    pass "bash -n $f"
  else
    fail "bash -n $f — $(tr '\n' ' ' </tmp/_bashn.$$)"
  fi
done < <(collect -name '*.sh' -o -name '*.sh.example' -o -name '*.example.sh')
rm -f /tmp/_bashn.$$

# ---------------------------------------------------------------------------
section "2. python -m py_compile — python syntax"
# ---------------------------------------------------------------------------
if [ -z "$PY" ]; then
  skip "py_compile — no python interpreter found (set \$PYTHON)"
else
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if "$PY" -m py_compile "$f" 2>/tmp/_pyc.$$; then
      pass "py_compile $f"
    else
      fail "py_compile $f — $(tr '\n' ' ' </tmp/_pyc.$$)"
    fi
  done < <(collect -name '*.py')
  rm -f /tmp/_pyc.$$
fi

# ---------------------------------------------------------------------------
section "3. import handler.py — dispatcher module imports cleanly"
# ---------------------------------------------------------------------------
HANDLER="hooks/per-profile-dispatcher/handler.py"
if [ -z "$PY" ]; then
  skip "import handler.py — no python interpreter"
elif [ ! -f "$HANDLER" ]; then
  fail "import handler.py — file missing: $HANDLER"
else
  if "$PY" - "$HANDLER" <<'PYEOF' 2>/tmp/_imp.$$
import importlib.util, sys, pathlib
p = pathlib.Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location("hermes_dispatcher", p)
mod = importlib.util.module_from_spec(spec)
# Register before exec so @dataclass can resolve the module (Python 3.12+/3.14).
sys.modules["hermes_dispatcher"] = mod
spec.loader.exec_module(mod)
# Smoke: the pure functions the unit tests depend on must exist.
for name in ("deps_resolved", "worker_is_stale", "cooldown_remaining",
             "idle_profiles", "pick_task_for", "state_fingerprint"):
    assert hasattr(mod, name), f"missing pure function: {name}"
PYEOF
  then
    pass "import $HANDLER (pure functions present)"
  else
    fail "import $HANDLER — $(tr '\n' ' ' </tmp/_imp.$$)"
  fi
  rm -f /tmp/_imp.$$
fi

# ---------------------------------------------------------------------------
section "4. YAML validity — yaml.safe_load"
# ---------------------------------------------------------------------------
if [ -z "$PY" ]; then
  skip "YAML validity — no python interpreter"
elif ! "$PY" -c "import yaml" 2>/dev/null; then
  skip "YAML validity — PyYAML not importable (pip install pyyaml)"
else
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if "$PY" - "$f" <<'PYEOF' 2>/tmp/_yaml.$$
import sys, yaml
# Some example files are multi-doc; safe_load_all tolerates single + multi.
with open(sys.argv[1]) as fh:
    list(yaml.safe_load_all(fh))
PYEOF
    then
      pass "yaml $f"
    else
      fail "yaml $f — $(tr '\n' ' ' </tmp/_yaml.$$)"
    fi
  done < <(collect -name '*.yaml' -o -name '*.yaml.example' -o -name '*.yml' -o -name '*.yml.example')
  rm -f /tmp/_yaml.$$
fi

# ---------------------------------------------------------------------------
section "5. JSON validity"
# ---------------------------------------------------------------------------
if [ -z "$PY" ]; then
  skip "JSON validity — no python interpreter"
else
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if "$PY" -c "import json,sys; json.load(open(sys.argv[1]))" "$f" 2>/tmp/_json.$$; then
      pass "json $f"
    else
      fail "json $f — $(tr '\n' ' ' </tmp/_json.$$)"
    fi
  done < <(collect -name '*.json' -o -name '*.json.example')
  rm -f /tmp/_json.$$
fi

# ---------------------------------------------------------------------------
section "6. plutil -lint — launchd plists (macOS only)"
# ---------------------------------------------------------------------------
if os_is_linux; then
  skip "plutil -lint — not on Linux (launchd is macOS-only)"
elif ! have plutil; then
  skip "plutil -lint — plutil not found"
else
  _found_plist=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    _found_plist=1
    if plutil -lint "$f" >/tmp/_plist.$$ 2>&1; then
      pass "plutil $f"
    else
      fail "plutil $f — $(tr '\n' ' ' </tmp/_plist.$$)"
    fi
  done < <(collect -name '*.plist' -o -name '*.plist.example')
  rm -f /tmp/_plist.$$
  [ "$_found_plist" -eq 0 ] && skip "plutil -lint — no plist files found"
fi

# ---------------------------------------------------------------------------
section "7. docker compose config — docker-compose.yml"
# ---------------------------------------------------------------------------
COMPOSE="docker/docker-compose.yml"
if ! have docker; then
  skip "docker compose config — docker not installed"
elif [ ! -f "$COMPOSE" ]; then
  skip "docker compose config — $COMPOSE not present"
elif ! docker compose version >/dev/null 2>&1; then
  skip "docker compose config — 'docker compose' plugin unavailable"
else
  # Check #7 validates compose SYNTAX/STRUCTURE only — it does not (and must
  # not) assert real secret values. docker-compose.yml intentionally guards
  # deploy-critical vars with ${VAR:?...} so a real `up` without a real .env
  # fails loudly; that guard stays intact. On a clean checkout (no .env)
  # those guards also trip `docker compose config`, so we feed throwaway
  # placeholder values via a scratch --env-file for this validation call
  # only — real deploys still require real values.
  DC_ENV_FILE="$(mktemp)"
  cat >"$DC_ENV_FILE" <<'EOF'
SEARXNG_SECRET=ci-validate-only-not-a-real-secret
HINDSIGHT_DB_PASSWORD=ci-validate-only-not-a-real-secret
EOF
  if docker compose --env-file "$DC_ENV_FILE" -f "$COMPOSE" config -q >/tmp/_dc.$$ 2>&1; then
    pass "docker compose config $COMPOSE"
  else
    fail "docker compose config $COMPOSE — $(tr '\n' ' ' </tmp/_dc.$$)"
  fi
  rm -f /tmp/_dc.$$ "$DC_ENV_FILE"
fi

# ---------------------------------------------------------------------------
section "8. relative markdown links — existence check"
# ---------------------------------------------------------------------------
# For every [text](path) in every *.md, if the target is a RELATIVE local path
# (not http(s)://, not mailto:, not a #anchor), the file must exist relative to
# the markdown file's directory. Strips trailing #anchor fragments.
if [ -z "$PY" ]; then
  skip "markdown links — no python interpreter"
else
  "$PY" - "$REPO_ROOT" <<'PYEOF' >/tmp/_md.$$ 2>&1
import os, re, sys, pathlib
root = pathlib.Path(sys.argv[1])
link_re = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
broken, checked = [], 0
SKIP_PREFIX = ("http://", "https://", "mailto:", "tel:", "#", "<")
for md in root.rglob("*.md"):
    if any(part in (".git", "node_modules", "__pycache__") for part in md.parts):
        continue
    text = md.read_text(encoding="utf-8", errors="replace")
    # Strip fenced code blocks (```...```) and inline code spans (`...`) so that
    # documentation EXAMPLES of link syntax are not treated as real links.
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", "", text)
    for m in link_re.finditer(text):
        target = m.group(1).strip()
        if not target or target.startswith(SKIP_PREFIX):
            continue
        # Drop anchor and query fragments.
        target = target.split("#", 1)[0].split("?", 1)[0]
        if not target:
            continue
        if target.startswith("/"):
            # Absolute-from-repo-root convention.
            resolved = (root / target.lstrip("/"))
        else:
            resolved = (md.parent / target)
        checked += 1
        if not resolved.exists():
            broken.append(f"{md.relative_to(root)} -> {m.group(1)}")
print(f"CHECKED={checked}")
if broken:
    print("BROKEN=" + str(len(broken)))
    for b in broken:
        print("  " + b)
    sys.exit(1)
sys.exit(0)
PYEOF
  if [ $? -eq 0 ]; then
    pass "markdown links — $(grep -o 'CHECKED=[0-9]*' /tmp/_md.$$ | head -1) all resolve"
  else
    fail "markdown links — broken: $(tr '\n' ' ' </tmp/_md.$$)"
  fi
  rm -f /tmp/_md.$$
fi

# ---------------------------------------------------------------------------
# Per-file summary (the top-level runner prints the grand total).
# ---------------------------------------------------------------------------
section "STATIC summary"
info "PASS=$PASS_COUNT  FAIL=$FAIL_COUNT  SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
