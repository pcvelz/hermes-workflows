"""Browser guard for claude-code-bridge: a PreToolUse hook (stdlib only).

Copied into each channel directory (only when the channel has a ``browser_guard``) and run from
there by Claude Code. It reads the hook JSON (``tool_name``, ``tool_input``) from stdin and the
guard config ``browser_guard.json`` written by ``session.prepare_dir``.

Exit codes (the PreToolUse contract): 0 = allow, 2 = block; the reason goes to stderr, where Claude
Code shows it to the model. A network call is spaced from the previous one by sleeping the
remainder of ``min_interval_s``. Any unexpected error fails closed (exit 2).

Optional ``ensure_command`` (e.g. ``bash /path/to/ensure-cdp-chrome.sh``): run — fail-open, bounded
by ``ensure_timeout_s`` (default 45 s) — for every guarded browser tool call, before the call is
allowed. It covers both "count this as real browser use" (the command stamps a use-marker an idle
watchdog trusts) and "make the browser reachable again" (the command kickstarts an on-demand
browser the idle watchdog stopped), so a guarded tool never fails just because the browser was
unloaded between uses.

State: ``browser_guard_state.json`` next to this file, ``{"calls": [unix time of each network call]}``,
written atomically and pruned to the last 24 h. A missing or corrupt state file counts as empty.
"""

import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "browser_guard.json"
STATE = HERE / "browser_guard_state.json"
HOUR = 3600
DAY = 86400
BLOCK = 2


def load_state(path):
    """Recorded network-call timestamps; anything unreadable or malformed is an empty list."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    calls = raw.get("calls") if isinstance(raw, dict) else None
    if not isinstance(calls, list):
        return []
    return [float(c) for c in calls
            if isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c)]


def save_state(path, calls):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"calls": calls}), encoding="utf-8")
    os.replace(tmp, path)


def run_ensure(command, cfg):
    """Best-effort run of the configured ensure_command. Never raises, never
    blocks the tool: the browser call itself reports its own error when the
    command could not bring the browser up."""
    timeout = cfg.get("ensure_timeout_s")
    try:
        timeout = float(timeout) if timeout else 45.0
        if not (0 < timeout <= 300):
            timeout = 45.0
    except (TypeError, ValueError):
        timeout = 45.0
    try:
        subprocess.run(
            shlex.split(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except Exception:
        pass


def guard(data, cfg, state_path, now=time.time, sleep=time.sleep):
    """Decide one tool call. Returns ``(exit_code, stderr_reason)``; sleeps for interval spacing."""
    if cfg.get("misconfigured"):
        return BLOCK, "browser guard is misconfigured; report the results you have"
    name = data.get("tool_name")
    if not isinstance(name, str):
        return 0, ""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    if name in (cfg.get("deny_tools") or []):
        return BLOCK, "this browser action is not available in this chat; use the other tools"
    url = tool_input.get("url")
    if isinstance(url, str) and any(re.search(p, url) for p in cfg.get("deny_url_patterns") or []):
        return BLOCK, "that page is not available in this chat; use the other tools"

    if not re.fullmatch(cfg.get("tools") or "", name):
        return 0, ""

    # Real browser tool call: give the host a chance to count it as use and to
    # bring the browser back up after an idle unload. Fail-open by design.
    ensure = cfg.get("ensure_command")
    if isinstance(ensure, str) and ensure.strip():
        run_ensure(ensure.strip(), cfg)

    network = cfg.get("network_tools") or ""
    if not network or not re.fullmatch(network, name):
        return 0, ""

    t = now()
    calls = [c for c in load_state(state_path) if t - c < DAY]
    per_hour = cfg.get("max_per_hour")
    per_day = cfg.get("max_per_day")
    if per_hour and sum(1 for c in calls if t - c < HOUR) >= per_hour:
        return BLOCK, "browser budget reached for this hour; report the results you have"
    if per_day and len(calls) >= per_day:
        return BLOCK, "browser budget reached for today; report the results you have"

    interval = float(cfg.get("min_interval_s") or 0)
    if calls and interval > 0:
        wait = max(calls) + interval - t
        if wait > 0:
            sleep(wait)
            t = now()
    calls = [c for c in calls if t - c < DAY] + [t]
    save_state(state_path, calls)
    return 0, ""


def main():
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            raise ValueError("hook input is not an object")
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("guard config is not an object")
        code, reason = guard(data, cfg, STATE)
    except Exception:
        code, reason = BLOCK, "browser guard could not check this action; report the results you have"
    if reason:
        sys.stderr.write(reason + "\n")
    sys.exit(code)


if __name__ == "__main__":
    main()
