#!/usr/bin/env python3
"""
tests/smoke/test_dispatcher.py — pure-function unit tests for the per-profile
dispatcher (hooks/per-profile-dispatcher/handler.py).

These exercise ONLY the deterministic, I/O-free logic functions:
  - deps_resolved        (promote-when-deps-resolve)
  - worker_is_stale      (heartbeat / startup / runtime detection + priority)
  - cooldown_remaining   (normal / fast-retry / after-fails cooldown math)
  - idle_profiles        (busy-profile exclusion, order preservation)
  - pick_task_for        (assignee preference + FIFO fallback)
  - state_fingerprint    (order-insensitive stable hash, change detection)

No backend adapters are touched (those raise NotImplementedError by design),
so there is zero risk to the live install. Stdlib unittest only — runnable as:
    python tests/smoke/test_dispatcher.py
"""
import importlib.util
import pathlib
import sys
import unittest

# --- Locate and import handler.py by path (no package install needed) -------
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
HANDLER_PATH = REPO_ROOT / "hooks" / "per-profile-dispatcher" / "handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("hermes_dispatcher", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module (Python 3.12+/3.14).
    sys.modules["hermes_dispatcher"] = mod
    spec.loader.exec_module(mod)
    return mod


H = _load_handler()
Task = H.Task
Worker = H.Worker
ProfileSpawn = H.ProfileSpawn
TaskState = H.TaskState


def task(tid, state, deps=None, assignee=None, fail_count=0, last_failed_at=None):
    return Task(id=tid, state=state, deps=deps or [], assignee=assignee,
                fail_count=fail_count, last_failed_at=last_failed_at)


class TestDepsResolved(unittest.TestCase):
    def test_no_deps_is_resolved(self):
        t = task("a", TaskState.TODO)
        self.assertTrue(H.deps_resolved(t, {"a": t}))

    def test_all_deps_done(self):
        d1 = task("d1", TaskState.DONE)
        d2 = task("d2", TaskState.DONE)
        t = task("a", TaskState.TODO, deps=["d1", "d2"])
        idx = {"d1": d1, "d2": d2, "a": t}
        self.assertTrue(H.deps_resolved(t, idx))

    def test_one_dep_not_done(self):
        d1 = task("d1", TaskState.DONE)
        d2 = task("d2", TaskState.IN_PROGRESS)
        t = task("a", TaskState.TODO, deps=["d1", "d2"])
        idx = {"d1": d1, "d2": d2, "a": t}
        self.assertFalse(H.deps_resolved(t, idx))

    def test_missing_dep_is_unresolved(self):
        # Dangling reference must NOT promote.
        t = task("a", TaskState.TODO, deps=["ghost"])
        self.assertFalse(H.deps_resolved(t, {"a": t}))


class TestWorkerStale(unittest.TestCase):
    NOW = 1_000_000.0

    def test_healthy_recent_heartbeat(self):
        w = Worker(profile="coder", task_id="t1",
                   started_at=self.NOW - 100, last_heartbeat=self.NOW - 10)
        self.assertIsNone(H.worker_is_stale(w, self.NOW))

    def test_stale_heartbeat(self):
        w = Worker(profile="coder", task_id="t1",
                   started_at=self.NOW - 1000,
                   last_heartbeat=self.NOW - (H.HEARTBEAT_STALE_SECONDS + 1))
        self.assertEqual(H.worker_is_stale(w, self.NOW), "heartbeat")

    def test_startup_never_heartbeat(self):
        # Spawned, no heartbeat ever, past the startup grace window.
        w = Worker(profile="coder", task_id="t1",
                   started_at=self.NOW - (H.STARTUP_GRACE_SECONDS + 1),
                   last_heartbeat=None)
        self.assertEqual(H.worker_is_stale(w, self.NOW), "startup")

    def test_startup_within_grace_is_healthy(self):
        w = Worker(profile="coder", task_id="t1",
                   started_at=self.NOW - (H.STARTUP_GRACE_SECONDS - 5),
                   last_heartbeat=None)
        self.assertIsNone(H.worker_is_stale(w, self.NOW))

    def test_runtime_exceeded_with_fresh_heartbeat(self):
        # Heartbeat is fresh (so check 1 passes) but total runtime exceeded.
        w = Worker(profile="coder", task_id="t1",
                   started_at=self.NOW - (H.MAX_RUNTIME_SECONDS + 1),
                   last_heartbeat=self.NOW - 5)
        self.assertEqual(H.worker_is_stale(w, self.NOW), "runtime")

    def test_heartbeat_priority_over_runtime(self):
        # Both heartbeat-stale AND runtime-exceeded: heartbeat wins (priority).
        w = Worker(profile="coder", task_id="t1",
                   started_at=self.NOW - (H.MAX_RUNTIME_SECONDS + 1),
                   last_heartbeat=self.NOW - (H.HEARTBEAT_STALE_SECONDS + 1))
        self.assertEqual(H.worker_is_stale(w, self.NOW), "heartbeat")


class TestCooldownRemaining(unittest.TestCase):
    NOW = 2_000_000.0

    def test_no_prior_spawn(self):
        self.assertEqual(H.cooldown_remaining(ProfileSpawn(), self.NOW), 0.0)

    def test_normal_cooldown_active(self):
        sp = ProfileSpawn(last_spawn_at=self.NOW - 100, last_fail_count=0)
        rem = H.cooldown_remaining(sp, self.NOW)
        self.assertAlmostEqual(rem, H.COOLDOWN_NORMAL_SECONDS - 100)

    def test_normal_cooldown_elapsed(self):
        sp = ProfileSpawn(last_spawn_at=self.NOW - (H.COOLDOWN_NORMAL_SECONDS + 10),
                          last_fail_count=0)
        self.assertEqual(H.cooldown_remaining(sp, self.NOW), 0.0)

    def test_fast_retry_window(self):
        # Recent failure (<5m) with fail_count between 1 and threshold-1.
        sp = ProfileSpawn(last_spawn_at=self.NOW - 30, last_fail_count=1)
        rem = H.cooldown_remaining(sp, self.NOW)
        self.assertAlmostEqual(rem, H.COOLDOWN_FAST_RETRY_SECONDS - 30)

    def test_after_failures_long_cooldown(self):
        sp = ProfileSpawn(last_spawn_at=self.NOW - 60,
                          last_fail_count=H.FAILURE_THRESHOLD)
        rem = H.cooldown_remaining(sp, self.NOW)
        self.assertAlmostEqual(rem, H.COOLDOWN_AFTER_FAILS_SECONDS - 60)

    def test_old_failure_falls_back_to_normal(self):
        # fail_count>0 but elapsed beyond fast-retry age window -> normal window.
        elapsed = H.FAST_RETRY_MAX_AGE_SECONDS + 10
        sp = ProfileSpawn(last_spawn_at=self.NOW - elapsed, last_fail_count=1)
        rem = H.cooldown_remaining(sp, self.NOW)
        # Normal window minus elapsed (still positive since 10m > age window edge).
        self.assertAlmostEqual(rem, H.COOLDOWN_NORMAL_SECONDS - elapsed)


class TestIdleProfiles(unittest.TestCase):
    def test_all_idle(self):
        profiles = ["orchestrator", "coder", "planner"]
        self.assertEqual(H.idle_profiles(profiles, []), profiles)

    def test_excludes_busy_preserves_order(self):
        profiles = ["orchestrator", "coder", "planner", "qa-tester"]
        workers = [Worker(profile="coder", task_id="t1")]
        self.assertEqual(
            H.idle_profiles(profiles, workers),
            ["orchestrator", "planner", "qa-tester"],
        )


class TestPickTaskFor(unittest.TestCase):
    def test_prefers_assigned_fifo(self):
        ready = [
            task("t1", TaskState.READY, assignee=None),
            task("t2", TaskState.READY, assignee="coder"),
            task("t3", TaskState.READY, assignee="coder"),
        ]
        chosen = H.pick_task_for("coder", ready)
        self.assertEqual(chosen.id, "t2")  # first assignee match (FIFO)

    def test_falls_back_to_unassigned(self):
        ready = [
            task("t1", TaskState.READY, assignee="planner"),
            task("t2", TaskState.READY, assignee=None),
        ]
        chosen = H.pick_task_for("coder", ready)
        self.assertEqual(chosen.id, "t2")

    def test_none_when_empty(self):
        self.assertIsNone(H.pick_task_for("coder", []))


class TestStateFingerprint(unittest.TestCase):
    def test_order_insensitive(self):
        t1 = task("a", TaskState.READY)
        t2 = task("b", TaskState.DONE)
        w1 = Worker(profile="coder", task_id="a")
        fp1 = H.state_fingerprint([t1, t2], [w1])
        fp2 = H.state_fingerprint([t2, t1], [w1])
        self.assertEqual(fp1, fp2)

    def test_changes_on_state_change(self):
        before = [task("a", TaskState.READY)]
        after = [task("a", TaskState.IN_PROGRESS)]
        self.assertNotEqual(
            H.state_fingerprint(before, []),
            H.state_fingerprint(after, []),
        )

    def test_changes_on_worker_change(self):
        tasks = [task("a", TaskState.IN_PROGRESS)]
        fp_no_worker = H.state_fingerprint(tasks, [])
        fp_worker = H.state_fingerprint(tasks, [Worker(profile="coder", task_id="a")])
        self.assertNotEqual(fp_no_worker, fp_worker)

    def test_stable_hex_length(self):
        fp = H.state_fingerprint([task("a", TaskState.DONE)], [])
        self.assertEqual(len(fp), 32)
        int(fp, 16)  # must be valid hex


if __name__ == "__main__":
    unittest.main(verbosity=2)
