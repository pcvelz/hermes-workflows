#!/usr/bin/env python3
"""Patience is a duration, not a retry count.

``agent.api_max_retries`` answers "how many times do I try again?".  That is
the wrong question for a worker whose model lives behind a shared local queue:
when an interactive session holds the box for ten hours, the honest answer is
"keep asking until either capacity comes back or my budget runs out".  Raising
the count from 3 to 30 only buys ~55 minutes of jittered backoff and makes the
same silent death less likely, not impossible.

So WAIT-CLASS failures (see :mod:`error_policy`) are retried against
``agent.model_wait_budget`` -- a wall-clock budget -- and REAL failures keep
the count limit.

The invariant
-------------
``model_wait_budget`` MUST be strictly less than the card's ``max_runtime``.

The card runtime cap is the OUTER bound: it is what a human reasoned about when
they said "this card gets four hours".  A wait budget at or above it would let
the worker burn the entire card allowance sitting in backoff and then be reaped
as ``timed_out`` -- the exact silent death this module exists to prevent, just
with a different label.  Keeping the budget inside the cap guarantees the
worker gives up waiting *first*, in a controlled way, with an escalation.

:func:`validate_budget` asserts it; :class:`WaitBudget` asserts it on
construction unless explicitly told not to.

The worker keeps its session in flight while waiting.  The existing backoff
loop already touches activity every ~30 s, which keeps the heartbeat fresh and
the claim extended -- nothing extra is needed to survive a long wait.

Stdlib only.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

# These modules are a flat, stdlib-only set that must import identically from
# three places: the agent venv, a launchd-context copy under HERMES_HOME, and
# the repo test suite.  Putting our own directory first is the one form that
# works in all three without a package install.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from error_policy import Verdict, is_wait_class  # noqa: E402

__all__ = [
    "DEFAULT_WAIT_BUDGET_SECONDS",
    "WaitBudgetInvariantError",
    "parse_duration",
    "validate_budget",
    "WaitBudget",
]

#: Shipped default: 30 minutes.  Conservative -- long enough to ride out a
#: model swap or a busy stretch, short enough that a misconfigured deploy does
#: not hang a card for hours.  A box with a heavily shared local backend wants
#: far more (12h is reasonable); see docs/resilience.md.
DEFAULT_WAIT_BUDGET_SECONDS = 30 * 60


class WaitBudgetInvariantError(ValueError):
    """Raised when ``model_wait_budget`` is not inside the card runtime cap."""


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: Any, *, default: Optional[float] = None) -> float:
    """Parse ``30m`` / ``12h`` / ``1800`` / ``1.5h`` into seconds.

    A bare number is seconds, matching every other duration key in the Hermes
    config (``gateway_timeout``, ``dispatch_stale_timeout_seconds``, ...).
    """
    if value is None:
        if default is None:
            raise ValueError("duration is required and no default was given")
        return float(default)
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        match = _DURATION_RE.match(str(value))
        if not match:
            raise ValueError(f"not a duration: {value!r}")
        seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
    if seconds < 0:
        raise ValueError(f"duration must not be negative: {value!r}")
    return seconds


def validate_budget(budget_seconds: float, max_runtime_seconds: Optional[float]) -> None:
    """Assert the invariant ``model_wait_budget < max_runtime``.

    ``max_runtime_seconds`` of ``None`` means the caller has no card cap in
    hand (an interactive session, a unit test); the invariant is vacuous and
    nothing is checked.
    """
    if max_runtime_seconds is None:
        return
    if budget_seconds >= max_runtime_seconds:
        raise WaitBudgetInvariantError(
            "agent.model_wait_budget must be STRICTLY LESS than the card's "
            f"max_runtime: budget={_fmt(budget_seconds)} >= "
            f"max_runtime={_fmt(max_runtime_seconds)}. The card runtime cap is "
            "the outer bound; a budget at or above it lets the worker spend the "
            "whole card allowance in backoff and then be reaped as timed_out."
        )


@dataclass
class WaitDecision:
    """Why the loop should keep going, or stop."""

    retry: bool
    #: ``"wait_budget"`` | ``"retry_count"`` | ``"budget_exhausted"``
    #: | ``"retries_exhausted"``
    basis: str
    remaining_seconds: float
    detail: str

    def __bool__(self) -> bool:  # convenience: `if decision:`
        return self.retry


class WaitBudget:
    """A wall-clock budget for WAIT-CLASS retries.

    One instance per API-call block.  :meth:`start` stamps the clock;
    :meth:`should_retry` is the replacement for ``retry_count < max_retries``.

    ``clock`` is injectable so tests can exhaust a twelve-hour budget
    instantly.
    """

    def __init__(
        self,
        budget_seconds: Any = DEFAULT_WAIT_BUDGET_SECONDS,
        *,
        max_runtime_seconds: Optional[Any] = None,
        clock: Callable[[], float] = time.monotonic,
        enforce_invariant: bool = True,
    ) -> None:
        self.budget_seconds = parse_duration(
            budget_seconds, default=DEFAULT_WAIT_BUDGET_SECONDS
        )
        self.max_runtime_seconds = (
            None if max_runtime_seconds is None
            else parse_duration(max_runtime_seconds)
        )
        if enforce_invariant:
            validate_budget(self.budget_seconds, self.max_runtime_seconds)
        self._clock = clock
        self._started_at: Optional[float] = None
        #: Number of WAIT-CLASS attempts made -- reported, never used as a limit.
        self.wait_attempts = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "WaitBudget":
        """Stamp the clock.  Idempotent: re-calling does NOT extend the budget."""
        if self._started_at is None:
            self._started_at = self._clock()
        return self

    def reset(self) -> None:
        """Forget the clock.  Called after a SUCCESSFUL call: the next stretch
        of unavailability gets a fresh budget, exactly like ``retry_count = 0``
        after a success."""
        self._started_at = None
        self.wait_attempts = 0

    # -- state --------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started_at is not None

    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, self._clock() - self._started_at)

    def remaining(self) -> float:
        return max(0.0, self.budget_seconds - self.elapsed())

    def exhausted(self) -> bool:
        return self.started and self.remaining() <= 0.0

    # -- the decision -------------------------------------------------------

    def should_retry(
        self,
        verdict: Any,
        retry_count: int,
        max_retries: int,
    ) -> WaitDecision:
        """Replacement for ``retry_count < max_retries``.

        * WAIT-CLASS -> keep going while the budget holds.  ``retry_count`` is
          IGNORED; it is bookkeeping for the log line, not a limit.
        * REAL       -> the classic count limit, untouched.

        ``verdict`` may be a :class:`error_policy.Verdict`, a ``FailoverReason``
        or a plain reason string.
        """
        reason, wait_class = _unpack(verdict)

        if not wait_class:
            if retry_count < max_retries:
                return WaitDecision(
                    True, "retry_count", self.remaining(),
                    f"{reason} is a real failure: attempt "
                    f"{retry_count + 1}/{max_retries}",
                )
            return WaitDecision(
                False, "retries_exhausted", self.remaining(),
                f"{reason} is a real failure and {max_retries} retries are spent",
            )

        self.start()
        self.wait_attempts += 1
        remaining = self.remaining()
        if remaining > 0:
            return WaitDecision(
                True, "wait_budget", remaining,
                f"{reason} is wait-class: waiting for model capacity, "
                f"{_fmt(remaining)} of {_fmt(self.budget_seconds)} budget left "
                f"(attempt {self.wait_attempts}, retry count not applied)",
            )
        return WaitDecision(
            False, "budget_exhausted", 0.0,
            f"{reason} is wait-class but the {_fmt(self.budget_seconds)} "
            f"model_wait_budget is exhausted after {self.wait_attempts} attempts",
        )

    def cap_sleep(self, wait_time: float) -> float:
        """Clamp a backoff sleep so it never overshoots the budget.

        Without this a 120 s backoff at 10 s remaining would report exhaustion
        110 s late -- harmless, but it makes the escalation message lie about
        when the worker gave up.
        """
        if not self.started:
            return max(0.0, wait_time)
        return max(0.0, min(float(wait_time), self.remaining()))

    def as_dict(self) -> dict:
        return {
            "budget_seconds": self.budget_seconds,
            "max_runtime_seconds": self.max_runtime_seconds,
            "elapsed_seconds": round(self.elapsed(), 3),
            "remaining_seconds": round(self.remaining(), 3),
            "wait_attempts": self.wait_attempts,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"WaitBudget(budget={_fmt(self.budget_seconds)}, "
            f"remaining={_fmt(self.remaining())}, attempts={self.wait_attempts})"
        )


# -- internals --------------------------------------------------------------

def _unpack(verdict: Any) -> Tuple[str, bool]:
    if isinstance(verdict, Verdict):
        return verdict.reason, verdict.wait_class
    reason = str(getattr(verdict, "value", verdict) or "unknown")
    return reason, is_wait_class(reason)


def _fmt(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unbounded"
    seconds = float(seconds)
    if seconds >= 3600:
        return f"{seconds / 3600:.2f}h".replace(".00h", "h")
    if seconds >= 60:
        return f"{seconds / 60:.1f}m".replace(".0m", "m")
    return f"{seconds:.0f}s"
