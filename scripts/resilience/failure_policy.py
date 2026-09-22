#!/usr/bin/env python3
"""Failure counting is a POLICY, not a fact.

Every non-success outcome funnels through ``_record_task_failure`` in
``hermes_cli/kanban_db.py`` and increments one counter, ``consecutive_failures``
on the CARD.  When it reaches the limit the card auto-blocks with a ``gave_up``
event, and ``recompute_ready`` then refuses to auto-recover it -- by design, to
stop an infinite block/recover/respawn cycle.

The design is right.  The input is wrong.  A card that never got a single token
out of the model has not failed; the machine has.  Counting a machine-caused
abort against the card is how a healthy card leaves the board permanently.

So which outcomes count becomes configurable::

    kanban:
      count_toward_breaker:
        crashed: true
        spawn_failed: false
        timed_out: false
        backend_busy: false

Shipped defaults are CONSERVATIVE -- everything counts, which is exactly
today's behaviour.  A deploy whose workers share one local model turns the
machine-caused ones off.

Stdlib only.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

__all__ = [
    "CONFIG_KEY",
    "KNOWN_OUTCOMES",
    "DEFAULT_COUNT_TOWARD_BREAKER",
    "resolve_policy",
    "should_count",
    "classify_outcome",
]

CONFIG_KEY = "count_toward_breaker"

#: Every non-success outcome ``_record_task_failure`` can be called with, plus
#: our own ``backend_busy`` (an outcome a worker reports when it exits because
#: the model backend never gave it capacity).
KNOWN_OUTCOMES = (
    "crashed",
    "spawn_failed",
    "timed_out",
    "backend_busy",
    "protocol_violation",
)

#: CONSERVATIVE shipped default: identical to current behaviour, everything
#: counts.  Turning an entry off is an explicit operator decision, because it
#: trades "a card can never die from machine trouble" against "a genuinely
#: broken card retries forever".  The escalation agent is what makes that trade
#: safe -- a card that keeps not-counting still gets a human told about it.
DEFAULT_COUNT_TOWARD_BREAKER: Dict[str, bool] = {
    "crashed": True,
    "spawn_failed": True,
    "timed_out": True,
    "backend_busy": True,
    "protocol_violation": True,
}


def resolve_policy(config: Optional[Mapping[str, Any]]) -> Dict[str, bool]:
    """Merge an operator's ``kanban.count_toward_breaker`` over the defaults.

    Accepts the whole config tree, the ``kanban`` sub-tree, or the mapping
    itself -- whichever the caller happens to hold.  Unknown keys are kept
    (a future outcome name should not be silently dropped); non-boolean values
    are coerced with :func:`_as_bool` so ``"false"`` from a loosely written
    YAML behaves as written rather than as truthy.
    """
    policy = dict(DEFAULT_COUNT_TOWARD_BREAKER)
    for entry in _candidate_mappings(config):
        for outcome, value in entry.items():
            policy[str(outcome)] = _as_bool(value)
    return policy


def should_count(outcome: Any, config: Optional[Mapping[str, Any]] = None) -> bool:
    """True when ``outcome`` increments the card's ``consecutive_failures``.

    An outcome nobody configured and that is not in :data:`KNOWN_OUTCOMES`
    counts -- fail safe, the breaker keeps working for outcomes we have not
    thought about.
    """
    policy = resolve_policy(config)
    return policy.get(classify_outcome(outcome), True)


def classify_outcome(outcome: Any) -> str:
    """Normalise an outcome / FailoverReason / event kind to a policy key."""
    if outcome is None:
        return "unknown"
    return str(getattr(outcome, "value", outcome)).strip().lower()


# -- internals --------------------------------------------------------------

def _candidate_mappings(config: Optional[Mapping[str, Any]]):
    """Yield every place the policy mapping could legitimately live."""
    if not isinstance(config, Mapping):
        return
    direct = config.get(CONFIG_KEY)
    if isinstance(direct, Mapping):
        yield direct
    kanban = config.get("kanban")
    if isinstance(kanban, Mapping):
        nested = kanban.get(CONFIG_KEY)
        if isinstance(nested, Mapping):
            yield nested
    # The mapping handed in bare, e.g. resolve_policy({"crashed": False}).
    if direct is None and "kanban" not in config:
        if all(not isinstance(v, Mapping) for v in config.values()):
            yield config


_FALSE_WORDS = {"false", "no", "off", "0", "", "none", "null"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in _FALSE_WORDS
