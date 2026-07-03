"""
hermes-bridge.py — Host-side HTTP control bridge for the Hermes autonomous agent workflow.

WHY THIS RUNS ON THE HOST (not in Docker or a sandbox)
=======================================================
The Hermes agent processes work autonomously, but several operations are structurally
impossible from inside a container or sandboxed environment:

1. git pull / git auth — needs the host's real SSH credential environment, host networking,
   and access to the filesystem at PROJECT_DIR. Container filesystem is isolated; host
   SSH keys and ssh-agent socket live in the host's session.

2. launchctl kickstart/restart — issues lifecycle commands to launchd services running
   under the host user's gui/$(id -u) domain. Impossible from inside a container running
   under a different UID or lacking access to the host launchd socket.

3. Host log file access — the gateway's own stdout/stderr logs live on the host filesystem.
   The bridge resolves them via an allowlist so the agent can tail them without open-ended
   filesystem access.

4. Optional Docker dev-stack up/down — the agent itself may run containerized; bringing
   its sibling services up/down requires the HOST docker CLI with access to the host daemon
   socket.

The Hermes gateway (which may run natively under launchd, or containerized in the Docker
topology) calls this bridge over:
  - http://127.0.0.1:9876       (native topology, loopback)
  - http://host.docker.internal:9876  (Docker topology — see BRIDGE_BIND / security note)

Port 9876 matches the Hermes bridge convention.

Reference: on the maintainer's machine an equivalent host bridge runs under launchd
(com.hermes-workflows.bridge). See docs/architecture/topologies.md for both topologies,
and docs/backend.md for LLM backend configuration details.

Security note: see BRIDGE_BIND config constant below.
"""

import argparse
import hmac
import http.server
import json
import logging
import os
import pathlib
import shlex
import socketserver
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# CONFIG  — all values overridable via environment variables
# ---------------------------------------------------------------------------

BRIDGE_PORT: int = int(os.environ.get("HERMES_BRIDGE_PORT", "9876"))

# SECURITY NOTE — BRIDGE_BIND
# Default: 127.0.0.1 (loopback only). This is safe for the native topology.
#
# Docker topology: containers reach the host via host.docker.internal, which requires
# binding 0.0.0.0 — but that exposes the bridge to your local network. If you do this:
#   - Set HERMES_BRIDGE_TOKEN to a strong shared secret.
#   - Apply a host firewall rule allowing only the container's subnet.
# See docs/backend.md and docs/architecture/topologies.md — the principle is the same:
# local-only by default, explicit opt-in with BRIDGE_BIND=0.0.0.0 only behind a trusted
# boundary (a loopback-bound backend faces the same constraint).
BRIDGE_BIND: str = os.environ.get("HERMES_BRIDGE_BIND", "127.0.0.1")

HERMES_HOME: pathlib.Path = pathlib.Path(
    os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
)

# TODO: Set HERMES_PROJECT_DIR to the repository the agent builds and deploys.
PROJECT_DIR: pathlib.Path = pathlib.Path(
    os.environ.get("HERMES_PROJECT_DIR", os.path.expanduser("~/your-project"))
)

LOG_DIR: pathlib.Path = HERMES_HOME / "logs"

# Optional shared-secret auth. If set, every request to a non-/health endpoint
# must carry the header:  X-Bridge-Token: <secret>
# Leave unset (empty string) to disable auth — recommended only on loopback.
BRIDGE_TOKEN: str = os.environ.get("HERMES_BRIDGE_TOKEN", "")

# Allowlist for /log-tail — short-name -> absolute path.
# NEVER add arbitrary paths here; this allowlist is a path-traversal guard.
# Extend it to add other log files the agent legitimately needs to read.
ALLOWED_LOG_FILES: dict[str, pathlib.Path] = {
    "gateway": LOG_DIR / "gateway.log",
    "gateway-error": LOG_DIR / "gateway.error.log",
    "bridge": LOG_DIR / "hermes-bridge.log",
    "bridge-error": LOG_DIR / "hermes-bridge.error.log",
}

# Allowlist for /service-restart. Provide a comma-separated list in the env var
# to add your own labels. Only exact matches are accepted — no glob/prefix.
_restart_allowlist_env: str = os.environ.get(
    "HERMES_RESTART_ALLOWLIST",
    "com.hermes-workflows.gateway,com.hermes-workflows.gateway-private,com.hermes-workflows.bridge",
)
RESTART_ALLOWLIST: set[str] = {
    label.strip() for label in _restart_allowlist_env.split(",") if label.strip()
}

# Timeouts for subprocess calls.
# NOTE: the first request to a cold backend may be slow; use a generous client timeout
# (>=120 s) for any operation that round-trips through the agent/model. The bridge
# itself uses these per-operation timeouts.
TIMEOUT_BUILD_DEPLOY: int = int(os.environ.get("HERMES_BUILD_TIMEOUT", "300"))
TIMEOUT_GIT: int = int(os.environ.get("HERMES_GIT_TIMEOUT", "120"))
TIMEOUT_RESTART: int = int(os.environ.get("HERMES_RESTART_TIMEOUT", "60"))
TIMEOUT_LOG_TAIL: int = 30

# Max stdout/stderr captured from subprocess calls (last N bytes, to avoid OOM).
_MAX_OUTPUT_BYTES: int = 8 * 1024  # 8 KB

# Maximum lines allowed in /log-tail (clamped 1..2000).
_LOG_TAIL_MAX_LINES: int = 2000


# ---------------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------------

def _configure_logging(log_dir: pathlib.Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "hermes-bridge.log"

    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path),
        logging.StreamHandler(sys.stderr),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


logger = logging.getLogger("hermes-bridge")


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _tail_bytes(text: str, max_bytes: int = _MAX_OUTPUT_BYTES) -> str:
    """Return the last max_bytes of text (UTF-8 safe)."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", errors="replace")


def run(
    cmd: list[str],
    cwd: pathlib.Path | None = None,
    timeout: int = TIMEOUT_BUILD_DEPLOY,
) -> dict:
    """
    Run cmd as a subprocess (never shell=True) and return a structured result.

    Returns dict with keys:
      ok          — True if returncode == 0
      returncode  — int
      stdout      — last ~8 KB of stdout (string)
      stderr      — last ~8 KB of stderr (string)
      cmd         — shell-quoted command string (for logs/display)
      duration_s  — elapsed seconds (float)
    """
    cmd_str = " ".join(shlex.quote(c) for c in cmd)
    logger.info("RUN: %s  (cwd=%s, timeout=%ds)", cmd_str, cwd or PROJECT_DIR, timeout)
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.monotonic() - t0
        ok = result.returncode == 0
        stdout = _tail_bytes(result.stdout)
        stderr = _tail_bytes(result.stderr)
        logger.info(
            "DONE: %s  rc=%d  %.1fs  stdout=%d chars  stderr=%d chars",
            cmd_str,
            result.returncode,
            duration,
            len(stdout),
            len(stderr),
        )
        return {
            "ok": ok,
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "cmd": cmd_str,
            "duration_s": round(duration, 3),
        }
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - t0
        logger.error("TIMEOUT: %s after %.1fs", cmd_str, duration)
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "",
            "cmd": cmd_str,
            "duration_s": round(duration, 3),
            "error": f"timeout after {timeout}s",
        }
    except FileNotFoundError as exc:
        duration = time.monotonic() - t0
        logger.error("NOT FOUND: %s — %s", cmd_str, exc)
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
            "cmd": cmd_str,
            "duration_s": round(duration, 3),
            "error": f"executable not found: {exc}",
        }


def json_response(handler: "BridgeHandler", status: int, obj: dict) -> None:
    """Write a JSON HTTP response."""
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def require_token(handler: "BridgeHandler") -> bool:
    """
    Validate X-Bridge-Token header when BRIDGE_TOKEN is configured.
    Returns True if auth passes (or is disabled). Sends 401 and returns False on failure.
    """
    if not BRIDGE_TOKEN:
        return True
    provided = handler.headers.get("X-Bridge-Token", "")
    if hmac.compare_digest(provided, BRIDGE_TOKEN):
        return True
    logger.warning("AUTH FAIL from %s", handler.client_address)
    json_response(handler, 401, {"error": "unauthorized", "hint": "Set X-Bridge-Token header"})
    return False


def safe_log_path(name: str) -> pathlib.Path | None:
    """
    Resolve a log file name via the ALLOWED_LOG_FILES allowlist only.
    Returns None if name is not in the allowlist (path-traversal guard).
    """
    return ALLOWED_LOG_FILES.get(name)


def _read_post_body(handler: "BridgeHandler") -> dict:
    """Read and parse the JSON POST body; returns {} on empty or parse error."""
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        return {}
    raw = handler.rfile.read(content_length)
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# ENDPOINT HANDLERS
# ---------------------------------------------------------------------------

def handle_health(handler: "BridgeHandler", _qs: dict, _body: dict) -> None:
    """GET /health — liveness probe (no auth required)."""
    json_response(handler, 200, {
        "status": "ok",
        "service": "hermes-bridge",
        "port": BRIDGE_PORT,
        "hermes_home": str(HERMES_HOME),
        "project_dir": str(PROJECT_DIR),
        "time": datetime.now(timezone.utc).isoformat(),
    })


def handle_build(handler: "BridgeHandler", _qs: dict, _body: dict) -> None:
    """
    POST /build — run your project's build command.

    TODO: Replace the placeholder below with your real build command.
          Examples:
            run(["make", "build"], cwd=PROJECT_DIR)
            run(["npm", "run", "build"], cwd=PROJECT_DIR)
            run([str(PROJECT_DIR / "scripts" / "build.sh")])
    """
    if not require_token(handler):
        return
    # TODO: Replace with your actual build command.
    result = run(["echo", "TODO: configure your build command in hermes-bridge.py"], timeout=TIMEOUT_BUILD_DEPLOY)
    json_response(handler, 200, result)


def handle_deploy(handler: "BridgeHandler", _qs: dict, body: dict) -> None:
    """
    POST /deploy — run your project's deploy command.
    Body (optional): {"target": "staging" | "prod"}

    TODO: Replace the placeholder below with your real deploy command.
          Examples:
            run(["make", f"deploy-{target}"], cwd=PROJECT_DIR)
            run(["./scripts/deploy.sh", target], cwd=PROJECT_DIR)
    """
    if not require_token(handler):
        return
    target = body.get("target", "staging")
    if target not in ("staging", "prod", "production"):
        json_response(handler, 400, {"error": f"unknown target: {target!r}"})
        return
    # TODO: Replace with your actual deploy command.
    result = run(
        ["echo", f"TODO: configure your deploy command for target={target}"],
        timeout=TIMEOUT_BUILD_DEPLOY,
    )
    result["target"] = target
    json_response(handler, 200, result)


def handle_git_pull(handler: "BridgeHandler", _qs: dict, body: dict) -> None:
    """
    POST /git-pull — fetch + fast-forward pull and report the resulting HEAD SHA.
    Body (optional): {"branch": "main"}

    Used by the redeploy-on-sha-drift cron (see cron/jobs.json.example) to detect
    whether the deployed code has drifted from the remote HEAD.
    """
    if not require_token(handler):
        return
    branch: str | None = body.get("branch") or None

    # Step 1: fetch all remotes.
    fetch_result = run(["git", "-C", str(PROJECT_DIR), "fetch", "--all"], timeout=TIMEOUT_GIT)
    if not fetch_result["ok"]:
        fetch_result["step"] = "fetch"
        json_response(handler, 200, fetch_result)
        return

    # Step 2: fast-forward pull.
    pull_cmd = ["git", "-C", str(PROJECT_DIR), "pull", "--ff-only"]
    if branch:
        pull_cmd += ["origin", branch]
    pull_result = run(pull_cmd, timeout=TIMEOUT_GIT)

    # Step 3: capture HEAD SHA regardless of pull result.
    sha_result = run(
        ["git", "-C", str(PROJECT_DIR), "rev-parse", "HEAD"],
        timeout=30,
    )
    head_sha = sha_result["stdout"].strip() if sha_result["ok"] else "unknown"

    pull_result["head_sha"] = head_sha
    pull_result["fetch"] = {"ok": fetch_result["ok"], "stdout": fetch_result["stdout"]}
    json_response(handler, 200, pull_result)


def handle_service_restart(handler: "BridgeHandler", _qs: dict, body: dict) -> None:
    """
    POST /service-restart — restart a launchd service via launchctl kickstart.
    Body: {"label": "com.hermes-workflows.gateway"}

    Security: only labels in RESTART_ALLOWLIST are accepted. The bridge and gateway
    must run in the same gui/$(id -u) launchd domain for this to work. If you run
    the bridge under a different user, kickstart will fail — this is a known constraint
    documented in the bridge README.
    """
    if not require_token(handler):
        return
    label: str = body.get("label", "").strip()
    if not label:
        json_response(handler, 400, {"error": "label is required"})
        return
    if label not in RESTART_ALLOWLIST:
        logger.warning("RESTART REJECTED — label not in allowlist: %r", label)
        json_response(handler, 400, {
            "error": f"label not in allowlist: {label!r}",
            "allowlist": sorted(RESTART_ALLOWLIST),
            "hint": "Set HERMES_RESTART_ALLOWLIST env var (comma-separated) to add labels.",
        })
        return

    # Determine the current user ID for the gui/<uid> launchd domain.
    uid = os.getuid()
    result = run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
        timeout=TIMEOUT_RESTART,
    )
    result["label"] = label
    json_response(handler, 200, result)


def handle_log_tail(handler: "BridgeHandler", qs: dict, _body: dict) -> None:
    """
    GET /log-tail?name=<name>&lines=<n>
    name  — key from ALLOWED_LOG_FILES allowlist (path-traversal guard)
    lines — number of lines to tail (clamped 1..2000, default 200)
    """
    if not require_token(handler):
        return
    name = qs.get("name", [""])[0]
    try:
        lines = int(qs.get("lines", ["200"])[0])
    except (ValueError, IndexError):
        lines = 200
    lines = max(1, min(lines, _LOG_TAIL_MAX_LINES))

    log_path = safe_log_path(name)
    if log_path is None:
        json_response(handler, 404, {
            "error": f"log name not in allowlist: {name!r}",
            "available": list(ALLOWED_LOG_FILES.keys()),
        })
        return

    result = run(["tail", "-n", str(lines), str(log_path)], timeout=TIMEOUT_LOG_TAIL)
    json_response(handler, 200, {
        "name": name,
        "path": str(log_path),
        "lines_requested": lines,
        "content": result["stdout"],
        "ok": result["ok"],
    })


def handle_devstack_up(handler: "BridgeHandler", _qs: dict, _body: dict) -> None:
    """
    POST /dev-stack/up — bring up the optional Docker dev-stack.

    TODO: Replace <COMPOSE_FILE> with the path to your docker-compose file.
          Example: run(["docker", "compose", "-f", "/path/to/compose.yaml", "up", "-d"])

    This endpoint degrades gracefully when docker is not installed — returns
    ok=false with a descriptive hint rather than an error.

    Note: docker compose is an OPTIONAL topology. See docs/architecture/topologies.md.
    """
    if not require_token(handler):
        return
    import shutil
    if not shutil.which("docker"):
        json_response(handler, 200, {
            "ok": False,
            "error": "docker not found",
            "hint": "dev-stack is optional — install Docker or use the native topology. See docs/architecture/topologies.md.",
        })
        return
    # TODO: Replace with your actual docker compose file path.
    result = run(
        ["docker", "compose", "-f", "TODO_REPLACE_WITH_COMPOSE_PATH", "up", "-d"],
        timeout=TIMEOUT_BUILD_DEPLOY,
    )
    json_response(handler, 200, result)


def handle_devstack_down(handler: "BridgeHandler", _qs: dict, _body: dict) -> None:
    """
    POST /dev-stack/down — shut down the optional Docker dev-stack.

    TODO: Replace <COMPOSE_FILE> with the path to your docker-compose file.
    """
    if not require_token(handler):
        return
    import shutil
    if not shutil.which("docker"):
        json_response(handler, 200, {
            "ok": False,
            "error": "docker not found",
            "hint": "dev-stack is optional. See docs/architecture/topologies.md.",
        })
        return
    # TODO: Replace with your actual docker compose file path.
    result = run(
        ["docker", "compose", "-f", "TODO_REPLACE_WITH_COMPOSE_PATH", "down"],
        timeout=TIMEOUT_BUILD_DEPLOY,
    )
    json_response(handler, 200, result)


# ---------------------------------------------------------------------------
# HTTP HANDLER
# ---------------------------------------------------------------------------

# Routes: (METHOD, path) -> handler_fn(handler, qs_dict, body_dict)
ROUTES: dict[tuple[str, str], callable] = {
    ("GET",  "/health"):         handle_health,
    ("POST", "/build"):          handle_build,
    ("POST", "/deploy"):         handle_deploy,
    ("POST", "/git-pull"):       handle_git_pull,
    ("POST", "/service-restart"): handle_service_restart,
    ("GET",  "/log-tail"):       handle_log_tail,
    ("POST", "/dev-stack/up"):   handle_devstack_up,
    ("POST", "/dev-stack/down"): handle_devstack_down,
}

# Known paths (for 405 Method Not Allowed vs 404 Not Found).
_KNOWN_PATHS: set[str] = {path for (_, path) in ROUTES}


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    """Thin HTTP dispatcher — each request is routed via ROUTES."""

    def log_message(self, fmt: str, *args: object) -> None:
        """Route BaseHTTPRequestHandler access log through logging instead of stderr."""
        logger.info("HTTP %s — " + fmt, self.address_string(), *args)

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        body: dict = {}
        if method == "POST":
            body = _read_post_body(self)

        key = (method, path)
        handler_fn = ROUTES.get(key)

        if handler_fn is not None:
            try:
                handler_fn(self, qs, body)
            except Exception as exc:
                logger.exception("UNHANDLED ERROR in %s %s: %s", method, path, exc)
                json_response(self, 500, {"error": str(exc), "type": type(exc).__name__})
        elif path in _KNOWN_PATHS:
            json_response(self, 405, {"error": "method not allowed", "allowed": "see docs"})
        else:
            json_response(self, 404, {"error": "not found", "path": path})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


class ThreadingBridgeServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """
    Threaded HTTP server so a slow build/deploy call does not block /health probes.
    Each request is handled in its own daemon thread.
    """
    daemon_threads = True


# ---------------------------------------------------------------------------
# MAIN / BOOTSTRAP
# ---------------------------------------------------------------------------

def main() -> None:
    global PROJECT_DIR, BRIDGE_PORT, BRIDGE_BIND
    parser = argparse.ArgumentParser(
        description="hermes-bridge — host-side HTTP bridge for the Hermes agent workflow"
    )
    parser.add_argument(
        "--port", type=int, default=BRIDGE_PORT,
        help=f"Port to listen on (default: {BRIDGE_PORT}, env: HERMES_BRIDGE_PORT)"
    )
    parser.add_argument(
        "--bind", default=BRIDGE_BIND,
        help=f"Bind address (default: {BRIDGE_BIND!r}, env: HERMES_BRIDGE_BIND). "
             "Set 0.0.0.0 ONLY behind a trusted boundary for Docker topology — see module docstring."
    )
    parser.add_argument(
        "--project-dir", type=pathlib.Path, default=PROJECT_DIR,
        help=f"Project directory for build/deploy/git ops (default: {PROJECT_DIR}, env: HERMES_PROJECT_DIR)"
    )
    args = parser.parse_args()

    # Apply CLI overrides.
    PROJECT_DIR = args.project_dir
    BRIDGE_PORT = args.port
    BRIDGE_BIND = args.bind

    _configure_logging(LOG_DIR)

    auth_status = "ENABLED (X-Bridge-Token)" if BRIDGE_TOKEN else "DISABLED (set HERMES_BRIDGE_TOKEN to enable)"
    bind_warning = (
        "  *** WARNING: binding 0.0.0.0 exposes privileged host ops to the LAN.\n"
        "  *** Set HERMES_BRIDGE_TOKEN and a host firewall rule. See docs/architecture/topologies.md."
        if BRIDGE_BIND == "0.0.0.0" else ""
    )

    banner = (
        f"\n{'='*60}\n"
        f"  hermes-bridge starting\n"
        f"  Bind:         {BRIDGE_BIND}:{BRIDGE_PORT}\n"
        f"  HERMES_HOME:  {HERMES_HOME}\n"
        f"  PROJECT_DIR:  {PROJECT_DIR}\n"
        f"  Token auth:   {auth_status}\n"
        f"  Endpoints:    GET /health  POST /build /deploy /git-pull\n"
        f"                POST /service-restart  GET /log-tail\n"
        f"                POST /dev-stack/up /dev-stack/down\n"
        f"  Quick check:  curl -s http://{BRIDGE_BIND}:{BRIDGE_PORT}/health\n"
        f"{'='*60}"
    )
    if bind_warning:
        banner += f"\n{bind_warning}"

    logger.info(banner)

    server = ThreadingBridgeServer((BRIDGE_BIND, BRIDGE_PORT), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("hermes-bridge shutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
