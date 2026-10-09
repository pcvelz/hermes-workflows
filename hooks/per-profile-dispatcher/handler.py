"""
Per-profile dispatcher hook — hermes-workflows scaffold.

Runs a 60-second tick loop that:
  1. Promotes tasks  todo -> ready  when their dependencies are resolved.
  2. Reaps stalled workers (no heartbeat / never started / exceeded max runtime).
  3. Dispatches one task per idle profile per tick (respecting cooldowns).
  4. Diffs the combined task+worker state on every tick and notifies ONLY when
     something changed — the "silent unless there is news" pattern.

IMPORTANT — THIS IS THE OPTIONAL CUSTOM DISPATCHER. The PRIMARY, PROVEN path is
the built-in hermes-agent dispatcher: set `kanban.dispatch_in_gateway: true` in
config.yaml and run the gateway (or `hermes kanban dispatch` for a single tick).
That native loop was demonstrated end-to-end — board -> spawned worker -> verdict
-> done, with no human in the loop. See examples/autonomous-loop/ and
docs/operating.md § "Running the autonomous loop".

Reach for THIS file only when you OUTGROW the native dispatcher and need custom
orchestration logic the config keys don't express — per-profile cooldowns,
bespoke fast-retry / failure-escalation windows, or a custom "notify only on
state change" channel (the external-dispatcher pattern). You then bind the
adapter functions below to the SAME `hermes kanban` commands the native loop uses.

Enable EITHER the native dispatcher OR this one — never both against the same
HERMES_HOME. To use this one, set `kanban.dispatch_in_gateway: false`.

All kanban reads/writes, worker management, and notification calls are isolated
behind clearly-marked TODO adapter functions (see section "BIND THESE TO YOUR
BACKEND"). The pure-logic functions are fully implemented and unit-testable
without any I/O — wire the adapters to your backend and the loop runs.

Dependencies: Python 3.10+ stdlib only (asyncio, dataclasses, enum, hashlib,
logging, time, typing). No third-party packages, no hermes imports.

Usage:
  python hooks/per-profile-dispatcher/handler.py

  In a real deploy, invoke this as a hermes hook or a launchd-managed
  long-running process. See docs/architecture/topologies.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# All thresholds are module-level constants so they are easy to change and can
# be referenced in unit tests. Comments tie each value back to the canonical
# spec and to the corresponding config.yaml key where one exists.
# =============================================================================

# Tick cadence — matches kanban.dispatch_interval_seconds: 60 in config.yaml.
TICK_SECONDS: int = 60

# Reap a worker if no heartbeat received within this window.
# Canonical spec: "reap workers stale >15 m heartbeat".
HEARTBEAT_STALE_SECONDS: int = 15 * 60          # 900 s = 15 minutes

# Reap a worker that was spawned but never produced its first heartbeat within
# this grace window (covers slow cold-starts).
# Canonical spec: "reap if >60 m without startup".
STARTUP_GRACE_SECONDS: int = 60 * 60            # 3600 s = 60 minutes

# Reap a worker that has been running longer than this regardless of heartbeats.
# Matches kanban.dispatch_stale_timeout_seconds: 14400 in config.yaml.
MAX_RUNTIME_SECONDS: int = 4 * 60 * 60          # 14400 s = 4 hours

# --- Cooldown windows (applied between dispatches to the same profile) ---

# Normal inter-spawn cooldown after a successful dispatch.
# Canonical spec: "10 m normal cooldown".
COOLDOWN_NORMAL_SECONDS: int = 10 * 60          # 600 s

# Short cooldown for a task that just failed — gives it a quick retry.
# Canonical spec: "2 m fast-retry".
COOLDOWN_FAST_RETRY_SECONDS: int = 2 * 60       # 120 s

# Long cooldown applied when a task has accumulated 3+ failures.
# Canonical spec: "30 m after 3+ failures".
COOLDOWN_AFTER_FAILS_SECONDS: int = 30 * 60     # 1800 s

# Age window within which a recently-failed task qualifies for the fast retry.
# If last_failed_at is older than this, fall back to the normal cooldown.
FAST_RETRY_MAX_AGE_SECONDS: int = 5 * 60        # 5 minutes

# Number of failures that trigger the long (30-minute) cooldown.
FAILURE_THRESHOLD: int = 3

# Profiles managed by this dispatcher.
# Edit to match your deployed profile names; these must correspond to
# $HERMES_HOME/profiles/<name>/ directories.
PROFILES: list[str] = ["orchestrator", "coding", "planner", "qa-tester"]


# =============================================================================
# DATA MODELS
# =============================================================================

class TaskState(Enum):
    """Lifecycle states for a kanban task."""
    TODO = "todo"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class Task:
    """A single kanban task as seen by the dispatcher."""
    id: str
    state: TaskState
    deps: list[str] = field(default_factory=list)
    # None means unassigned — dispatch to default_assignee or any idle profile.
    assignee: Optional[str] = None
    fail_count: int = 0
    last_failed_at: Optional[float] = None   # Unix timestamp


@dataclass
class Worker:
    """A running hermes gateway process handling a task."""
    profile: str
    task_id: str
    # None until the worker process confirms it started.
    started_at: Optional[float] = None
    # Monotonically updated by the worker process at intervals.
    last_heartbeat: Optional[float] = None


@dataclass
class ProfileSpawn:
    """Per-profile bookkeeping for cooldown calculation."""
    last_spawn_at: Optional[float] = None
    # fail_count of the last task dispatched to this profile — used to pick
    # the appropriate cooldown window.
    last_fail_count: int = 0


@dataclass
class DispatchContext:
    """Mutable state carried across ticks by the run loop."""
    # Hash/fingerprint of (task states + worker assignments) from the previous
    # tick. Notify only when this changes.
    last_fingerprint: str = ""
    # Per-profile cooldown bookkeeping.
    spawns: dict[str, ProfileSpawn] = field(default_factory=dict)

    def spawn_for(self, profile: str) -> ProfileSpawn:
        """Return (creating if absent) the ProfileSpawn for a profile."""
        if profile not in self.spawns:
            self.spawns[profile] = ProfileSpawn()
        return self.spawns[profile]


# =============================================================================
# === BIND THESE TO YOUR BACKEND ===
#
# Each function below is a stub that raises NotImplementedError. Replace the
# body with calls to your kanban store, process manager, and notifier.
#
# Contract notes:
#   - All functions are async; use `await` appropriately in the tick loop.
#   - None of these functions should raise on "not found" — return empty
#     collections / False instead.
#   - The dispatcher calls these from a single-threaded asyncio event loop;
#     thread safety inside the implementations is your responsibility.
# =============================================================================

async def fetch_tasks() -> list[Task]:
    """
    Read all tasks from the kanban board.

    TODO: Bind to the SAME kanban the native loop uses. The proven read path is
    the `hermes kanban` CLI — e.g. parse `hermes kanban list` (optionally
    `--status <col>`) / `hermes kanban show <task_id>`, a `sqlite3 -readonly`
    read against `$HERMES_HOME/kanban.db`, or the kanban HTTP API.

    Returns:
        All tasks in all states. The dispatcher filters by state internally.
    """
    raise NotImplementedError(
        "fetch_tasks: bind this to your kanban backend. "
        "See docs/architecture/README.md for the reference data-flow."
    )


async def fetch_workers() -> list[Worker]:
    """
    Read currently active worker processes and their heartbeat metadata.

    TODO: Replace with a process registry read, a sidecar heartbeat file,
    or a hermes session API call. Each Worker must carry its last heartbeat
    timestamp and spawn time so the reaper can apply the three stale checks.

    Returns:
        All workers currently considered active (not yet reaped).
    """
    raise NotImplementedError(
        "fetch_workers: bind this to your process/heartbeat registry. "
        "Workers that have already been reaped must NOT appear here."
    )


async def promote_task(task_id: str) -> None:
    """
    Transition task `task_id` from TODO to READY on the kanban board.

    TODO: Bind to the proven promote path — `hermes kanban promote <task_id>`
    (or a state.db write / kanban API call). Must be idempotent — calling it on
    an already-READY task should be a no-op.

    Args:
        task_id: Identifier of the task to promote.
    """
    raise NotImplementedError(
        f"promote_task({task_id!r}): bind this to your kanban write path."
    )


async def dispatch_task(profile: str, task_id: str) -> bool:
    """
    Spawn a hermes gateway worker for `profile` to handle `task_id`.

    TODO: Spawn the worker the SAME way the native loop does — the proven
    single-tick spawn is `hermes kanban dispatch` (it reclaims stale, promotes
    ready, and SPAWNS a worker per ready task, marking it running). For custom
    per-profile control, shell out to `hermes kanban dispatch` (or launch the
    hermes process directly via subprocess / launchd / the hermes API) and mark
    the task IN_PROGRESS. Return True on success, False if the spawn failed (the
    tick loop will bump fail_count).

    Args:
        profile:  Name of the hermes profile to run (e.g. "coding").
        task_id:  Identifier of the task to assign.

    Returns:
        True if the worker was successfully spawned, False otherwise.
    """
    raise NotImplementedError(
        f"dispatch_task(profile={profile!r}, task_id={task_id!r}): "
        "bind this to your process spawner / hermes API."
    )


async def reap_worker(worker: Worker, reason: str) -> None:
    """
    Kill or clean up a stalled worker and requeue its task.

    TODO: Bind to the proven reclaim path — `hermes kanban reclaim <task_id>`
    releases the worker's claim and returns the task to ready for retry (or
    `hermes kanban block <task_id>` to park it). Terminate the worker process,
    update the board accordingly, and remove the worker from the active registry.

    Args:
        worker: The Worker record to reap.
        reason: Human-readable reason string from worker_is_stale()
                (e.g. "heartbeat", "startup", "runtime").
    """
    raise NotImplementedError(
        f"reap_worker(profile={worker.profile!r}, reason={reason!r}): "
        "bind this to your process manager and kanban failure-handling path."
    )


async def notify(message: str) -> None:
    """
    Send a change-summary notification to the human operator.

    TODO: Wire to Telegram (via the orchestrator profile's Telegram toolset),
    Mattermost, a webhook, or any other notification channel. This is called
    AT MOST ONCE PER TICK and ONLY when the state fingerprint changed — the
    orchestrator profile owns the actual Telegram binding in a real deploy.

    Args:
        message: Concise text summary of what changed this tick.
    """
    raise NotImplementedError(
        "notify: bind this to Telegram, Mattermost, or your notifier. "
        "The orchestrator profile config overlay wires Telegram in a real deploy."
    )


# =============================================================================
# PURE LOGIC FUNCTIONS
# Fully implemented, deterministic, no I/O. Safe to unit-test in isolation.
# =============================================================================

def deps_resolved(task: Task, tasks_by_id: dict[str, Task]) -> bool:
    """
    Return True if all of `task`'s dependencies are in the DONE state.

    A missing dependency (not in tasks_by_id) is treated as unresolved to
    prevent accidental promotion of tasks with dangling references.

    Args:
        task:         The task whose dependencies we are checking.
        tasks_by_id:  Lookup dict {task_id: Task} for all known tasks.
    """
    for dep_id in task.deps:
        dep = tasks_by_id.get(dep_id)
        if dep is None or dep.state != TaskState.DONE:
            return False
    return True


def worker_is_stale(worker: Worker, now: float) -> Optional[str]:
    """
    Check whether a worker should be reaped. Returns the reason string if
    stale, or None if the worker is healthy.

    Checks are applied in priority order:
      1. "heartbeat"  — last_heartbeat is set but too old.
      2. "startup"    — worker was spawned but never sent a first heartbeat.
      3. "runtime"    — worker has run longer than MAX_RUNTIME_SECONDS.

    Args:
        worker: The Worker to evaluate.
        now:    Current Unix timestamp (time.time()).

    Returns:
        One of "heartbeat", "startup", "runtime", or None.
    """
    # Check 1: missing heartbeat from an otherwise started worker.
    if worker.last_heartbeat is not None:
        if now - worker.last_heartbeat > HEARTBEAT_STALE_SECONDS:
            return "heartbeat"

    # Check 2: worker spawned but never reached its first heartbeat.
    elif worker.started_at is not None:
        if now - worker.started_at > STARTUP_GRACE_SECONDS:
            return "startup"

    # Check 3: total runtime exceeded regardless of heartbeat health.
    if worker.started_at is not None:
        if now - worker.started_at > MAX_RUNTIME_SECONDS:
            return "runtime"

    return None


def cooldown_remaining(spawn: ProfileSpawn, now: float) -> float:
    """
    Return the number of seconds until the profile may be dispatched again.
    Returns 0.0 if the profile is ready to accept a new task immediately.

    Cooldown window selection (from the canonical spec):
      - last_fail_count >= FAILURE_THRESHOLD  →  30-minute cooldown.
      - last_fail_count > 0 AND last failure was recent (< FAST_RETRY_MAX_AGE) →
        2-minute fast-retry cooldown.
      - Otherwise  →  10-minute normal cooldown.

    Args:
        spawn: Bookkeeping record for the profile (set after last spawn).
        now:   Current Unix timestamp.

    Returns:
        Seconds remaining in cooldown, or 0.0 if none.
    """
    if spawn.last_spawn_at is None:
        return 0.0

    elapsed = now - spawn.last_spawn_at

    if spawn.last_fail_count >= FAILURE_THRESHOLD:
        window = COOLDOWN_AFTER_FAILS_SECONDS
    elif spawn.last_fail_count > 0 and elapsed < FAST_RETRY_MAX_AGE_SECONDS:
        window = COOLDOWN_FAST_RETRY_SECONDS
    else:
        window = COOLDOWN_NORMAL_SECONDS

    remaining = window - elapsed
    return max(0.0, remaining)


def idle_profiles(profiles: list[str], workers: list[Worker]) -> list[str]:
    """
    Return profiles that currently have no active (running) worker.

    A profile is idle if no Worker in `workers` has that profile name.
    Order is preserved so dispatch priority follows the PROFILES list order.

    Args:
        profiles: The full list of managed profile names.
        workers:  Currently active workers (after reaping).

    Returns:
        Subset of `profiles` with no worker currently assigned.
    """
    busy = {w.profile for w in workers}
    return [p for p in profiles if p not in busy]


def pick_task_for(profile: str, ready_tasks: list[Task]) -> Optional[Task]:
    """
    Select the best READY task for `profile`, using FIFO ordering with an
    assignee preference: assignee-matched tasks are preferred over unassigned.

    Args:
        profile:     The profile that would receive the task.
        ready_tasks: All tasks currently in the READY state.

    Returns:
        The chosen Task, or None if no suitable task exists.
    """
    # Prefer tasks explicitly assigned to this profile.
    assigned = [t for t in ready_tasks if t.assignee == profile]
    if assigned:
        return assigned[0]   # FIFO — list ordering from fetch_tasks()

    # Fall back to unassigned tasks.
    unassigned = [t for t in ready_tasks if not t.assignee]
    return unassigned[0] if unassigned else None


def state_fingerprint(tasks: list[Task], workers: list[Worker]) -> str:
    """
    Produce a short, stable hash of the combined task-state and worker-
    assignment snapshot. Used to detect changes between ticks.

    The fingerprint captures:
      - Each task's id and state.
      - Each worker's profile and task_id.

    It is intentionally NOT sensitive to ordering (sets are sorted) so that
    list ordering variations in fetch_tasks / fetch_workers do not produce
    false-change notifications.

    Args:
        tasks:   All tasks (any state).
        workers: All active workers.

    Returns:
        A hex-encoded MD5-length string (16 bytes, 32 hex chars).
    """
    task_part = "|".join(
        sorted(f"{t.id}:{t.state.value}" for t in tasks)
    )
    worker_part = "|".join(
        sorted(f"{w.profile}:{w.task_id}" for w in workers)
    )
    raw = f"{task_part}#{worker_part}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# =============================================================================
# TICK — single execution of the dispatch loop body
# =============================================================================

async def tick(ctx: DispatchContext) -> None:
    """
    Execute one 60-second tick of the dispatch loop.

    Steps:
      1. Fetch current tasks and workers from the backend adapters.
      2. Promote: any TODO task whose deps are all DONE moves to READY.
      3. Reap: stale workers are killed and their tasks re-queued.
      4. Dispatch: for each idle profile (after reaping) with no cooldown,
         pick a READY task and spawn a worker.
      5. Diff + notify: build the new state fingerprint; if it differs from
         the previous tick's fingerprint, send a change-summary notification.

    The entire body is wrapped in a broad try/except so one bad tick (e.g.
    a transient backend error) does not kill the run loop.

    Args:
        ctx: Mutable DispatchContext carrying cross-tick state.
    """
    try:
        now = time.time()

        # ----------------------------------------------------------------
        # Step 1: Fetch
        # ----------------------------------------------------------------
        tasks: list[Task] = await fetch_tasks()
        workers: list[Worker] = await fetch_workers()

        tasks_by_id: dict[str, Task] = {t.id: t for t in tasks}
        ready_tasks = [t for t in tasks if t.state == TaskState.READY]
        todo_tasks = [t for t in tasks if t.state == TaskState.TODO]

        # ----------------------------------------------------------------
        # Step 2: Promote TODO -> READY
        # ----------------------------------------------------------------
        promoted_ids: list[str] = []
        for task in todo_tasks:
            if deps_resolved(task, tasks_by_id):
                await promote_task(task.id)
                promoted_ids.append(task.id)
                # Optimistically add to ready_tasks so step 4 can dispatch
                # tasks promoted in this same tick without waiting a cycle.
                task.state = TaskState.READY
                ready_tasks.append(task)
                logger.info("Promoted task %s: todo -> ready", task.id)

        # ----------------------------------------------------------------
        # Step 3: Reap stale workers
        # ----------------------------------------------------------------
        reaped_reasons: list[tuple[str, str, str]] = []   # (profile, task, reason)
        surviving_workers: list[Worker] = []
        for worker in workers:
            reason = worker_is_stale(worker, now)
            if reason:
                await reap_worker(worker, reason)
                reaped_reasons.append((worker.profile, worker.task_id, reason))
                logger.warning(
                    "Reaped worker profile=%s task=%s reason=%s",
                    worker.profile, worker.task_id, reason,
                )
            else:
                surviving_workers.append(worker)
        workers = surviving_workers

        # ----------------------------------------------------------------
        # Step 4: Dispatch — one task per idle profile per tick
        # ----------------------------------------------------------------
        dispatched: list[tuple[str, str]] = []   # (profile, task_id)
        for profile in idle_profiles(PROFILES, workers):
            spawn = ctx.spawn_for(profile)
            remaining = cooldown_remaining(spawn, now)
            if remaining > 0:
                logger.debug(
                    "Profile %s in cooldown for %.0fs", profile, remaining
                )
                continue

            task = pick_task_for(profile, ready_tasks)
            if task is None:
                logger.debug("No ready task for profile %s", profile)
                continue

            success = await dispatch_task(profile, task.id)
            if success:
                spawn.last_spawn_at = now
                spawn.last_fail_count = task.fail_count
                dispatched.append((profile, task.id))
                # Remove from ready_tasks so we don't dispatch the same task
                # to a second profile within this tick.
                ready_tasks = [t for t in ready_tasks if t.id != task.id]
                logger.info("Dispatched task %s -> profile %s", task.id, profile)
            else:
                # Dispatch failed — bump fail bookkeeping for cooldown.
                task.fail_count += 1
                task.last_failed_at = now
                spawn.last_fail_count = task.fail_count
                spawn.last_spawn_at = now   # apply cooldown even on failure
                logger.error(
                    "dispatch_task failed profile=%s task=%s fail_count=%d",
                    profile, task.id, task.fail_count,
                )

        # ----------------------------------------------------------------
        # Step 5: Diff + notify (silent unless state changed)
        # ----------------------------------------------------------------
        # Re-fetch workers to capture the newly dispatched ones in the
        # fingerprint — or use a lightweight optimistic update if re-fetching
        # is expensive. Here we call fetch_workers() again for accuracy.
        current_workers = await fetch_workers()
        current_tasks = await fetch_tasks()
        fp = state_fingerprint(current_tasks, current_workers)

        if fp != ctx.last_fingerprint:
            ctx.last_fingerprint = fp

            # Build a concise human-readable change summary.
            parts: list[str] = []
            if promoted_ids:
                parts.append(f"Promoted {len(promoted_ids)}: {', '.join(promoted_ids)}")
            if reaped_reasons:
                summary = "; ".join(
                    f"{p}/{t} ({r})" for p, t, r in reaped_reasons
                )
                parts.append(f"Reaped {len(reaped_reasons)}: {summary}")
            if dispatched:
                summary = "; ".join(f"{p}<-{t}" for p, t in dispatched)
                parts.append(f"Dispatched {len(dispatched)}: {summary}")

            # Escalation signals (repeated failures) worth a human ping.
            high_fail_tasks = [
                t for t in current_tasks if t.fail_count >= FAILURE_THRESHOLD
            ]
            if high_fail_tasks:
                ids = ", ".join(t.id for t in high_fail_tasks)
                parts.append(f"ESCALATE — {len(high_fail_tasks)} task(s) with "
                             f">={FAILURE_THRESHOLD} failures: {ids}")

            message = "Dispatcher tick: " + " | ".join(parts) if parts else "State changed."
            await notify(message)
            logger.info("Notification sent: %s", message)
        else:
            # State unchanged — stay silent ("only ping on change" rule).
            logger.debug("State unchanged, no notification sent.")

    except Exception:
        # Broad catch ensures a transient error (network, DB lock, etc.) does
        # not crash the entire run loop. Log and continue to the next tick.
        logger.exception("Unhandled error in tick(); continuing loop.")


# =============================================================================
# RUN LOOP
# =============================================================================

async def run(stop_event: Optional[asyncio.Event] = None) -> None:
    """
    Main async run loop. Ticks every TICK_SECONDS until `stop_event` is set
    (or indefinitely if stop_event is None — use Ctrl-C / SIGTERM in prod).

    Note on drift: asyncio.sleep() provides "close enough" 60-second cadence
    for a dispatch loop. For stricter cadence, subtract elapsed tick time from
    the sleep duration. The dispatcher is tolerant of a few-second drift.

    Args:
        stop_event: Optional asyncio.Event; set it to request a clean shutdown.
    """
    ctx = DispatchContext()
    logger.info(
        "Dispatcher started. Tick interval: %d s. Profiles: %s",
        TICK_SECONDS, PROFILES,
    )

    while stop_event is None or not stop_event.is_set():
        await tick(ctx)
        await asyncio.sleep(TICK_SECONDS)

    logger.info("Dispatcher stop_event set — shutting down cleanly.")


# =============================================================================
# ENTRYPOINT
# =============================================================================

if __name__ == "__main__":
    # Configure root logger for standalone execution.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # In a real deploy this is invoked as a hermes hook or a launchd-managed
    # long-running process (RunAtLoad=true, KeepAlive=true).
    # See docs/architecture/topologies.md for native launchd setup.
    asyncio.run(run())
