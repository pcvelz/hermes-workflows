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


if __name__ == "__main__":
    unittest.main()
