"""cdp-chrome-service lifecycle against the REAL Chrome for Testing binary (macOS only).

The service is lazy-loaded: consumers start it on demand and the use-marker records REAL
browser use. The service itself must never stamp that marker (a start alone would otherwise
hold the browser up for the whole idle window), a missing marker must still let it stop, and a
SIGTERM (a consumer's release: ``launchctl kill SIGTERM``) must stop Chrome and exit 0.
"""
import os
import platform
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "services" / "cdp-chrome-service" / "start-cdp-chrome.sh"
CACHE = Path.home() / "Library" / "Caches" / "ms-playwright"
BINARIES = sorted(CACHE.glob(
    "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def up(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return b"webSocketDebuggerUrl" in r.read()
    except Exception:
        return False


@unittest.skipUnless(platform.system() == "Darwin" and BINARIES, "needs the Playwright Chrome for Testing")
class ServiceLifecycleTests(unittest.TestCase):
    def start(self, **env):
        self.tmp = tempfile.mkdtemp(prefix="cdp-svc-test-")
        self.port = free_port()
        self.marker = os.path.join(self.tmp, "last-use")
        full = dict(os.environ, CDP_PORT=str(self.port), CDP_USE_MARKER=self.marker,
                    CDP_POLL_SECONDS="1", **{k: str(v) for k, v in env.items()})
        # Own process group, so cleanup reaps Chrome too: SIGKILL on the script alone would
        # orphan the browser (launchd reaps the job's group; a test must do the same).
        proc = subprocess.Popen(["bash", str(SCRIPT)], env=full, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        self.addCleanup(self.reap, proc)
        deadline = time.time() + 30
        while time.time() < deadline and not up(self.port):
            time.sleep(0.3)
        self.assertTrue(up(self.port), "Chrome for Testing did not come up")
        return proc

    def reap(self, proc):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                continue
            deadline = time.time() + 10
            while time.time() < deadline and up(self.port):
                time.sleep(0.3)
            if not up(self.port):
                return

    def test_start_does_not_stamp_the_use_marker_and_still_stops_when_idle(self):
        proc = self.start(CDP_IDLE_SECONDS=4)
        self.assertFalse(os.path.exists(self.marker), "the service stamped its own use-marker")
        self.assertEqual(proc.wait(timeout=40), 0)
        self.assertFalse(up(self.port), "Chrome still answers after the idle stop")

    def test_sigterm_release_stops_chrome_and_exits_zero(self):
        proc = self.start(CDP_IDLE_SECONDS=600)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 0)
        deadline = time.time() + 10
        while time.time() < deadline and up(self.port):
            time.sleep(0.3)
        self.assertFalse(up(self.port), "Chrome still answers after the release")


FAKE_CHROME = """#!/bin/bash
if [[ "${1:-}" == "--version" ]]; then echo "Google Chrome for Testing 120.0.0.0"; exit 0; fi
echo $$ > "$FAKE_CHROME_PID_FILE"
trap '' TERM
while :; do sleep 1; done
"""


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class StopEscalationTests(unittest.TestCase):
    """A Chrome that ignores SIGTERM must not hang the script: it escalates to SIGKILL after
    CDP_STOP_GRACE_SECONDS (regression: an unbounded `wait` left the launchd job running 18 h)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cdp-fake-test-")
        binary = Path(self.tmp) / "cache" / "chromium-9999" / "chrome-mac-arm64" / \
            "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"
        binary.parent.mkdir(parents=True)
        binary.write_text(FAKE_CHROME)
        binary.chmod(0o755)
        self.pid_file = Path(self.tmp) / "fake.pid"
        self.port = free_port()
        self.env = dict(os.environ, CDP_CACHE_DIR=str(Path(self.tmp) / "cache"), CDP_PORT=str(self.port),
                        CDP_USE_MARKER=os.path.join(self.tmp, "last-use"), CDP_STOP_GRACE_SECONDS="2",
                        FAKE_CHROME_PID_FILE=str(self.pid_file))
        self.addCleanup(self.cleanup)
        self.procs = []

    def cleanup(self):
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        if self.pid_file.exists():
            try:
                os.kill(int(self.pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass

    def launch(self, cmd, **env):
        proc = subprocess.Popen(cmd, env=dict(self.env, **env), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(proc)
        return proc

    def fake_pid(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.pid_file.exists() and self.pid_file.read_text().strip():
                return int(self.pid_file.read_text())
            time.sleep(0.1)
        self.fail("fake browser did not start")

    def assert_stops(self, proc, within):
        pid = self.fake_pid()
        start = time.time()
        self.assertEqual(proc.wait(timeout=within), 0)
        self.assertLess(time.time() - start, within)
        self.assertFalse(pid_alive(pid), "fake browser survived the stop")

    def test_sigterm_release_kills_a_chrome_that_ignores_sigterm(self):
        proc = self.launch(["bash", str(SCRIPT)], CDP_IDLE_SECONDS="600")
        pid = self.fake_pid()
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        proc.send_signal(signal.SIGTERM)  # a second TERM during the grace period must not hang it
        self.assertEqual(proc.wait(timeout=6), 0)
        self.assertFalse(pid_alive(pid), "fake browser survived the stop")

    def test_idle_stop_kills_a_chrome_that_ignores_sigterm(self):
        docroot = Path(self.tmp) / "www"
        (docroot / "json").mkdir(parents=True)
        (docroot / "json" / "list").write_text('[{"id": "a", "url": "about:blank"}]')
        self.launch(["python3", "-m", "http.server", str(self.port), "--bind", "127.0.0.1",
                     "--directory", str(docroot)])
        proc = self.launch(["bash", str(SCRIPT)], CDP_IDLE_SECONDS="1", CDP_POLL_SECONDS="1")
        self.assert_stops(proc, 15)


if __name__ == "__main__":
    unittest.main()
