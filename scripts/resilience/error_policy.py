#!/usr/bin/env python3
"""Error taxonomy: WAIT-CLASS versus REAL failures.

The upstream agent classifier (``agent/error_classifier.py``) already returns a
``FailoverReason``.  It is close to right, with one gap that matters on a box
whose model backend is a *local queue* rather than a remote provider:

  * A mid-stream disconnect from the local proxy (the queue aborted the stream
    because another caller took the slot) and a request parked behind a model
    swap both land in ``timeout`` today -- mixed in with genuine stalls where
    the model really did hang.

Those two are not failures at all.  They mean "no capacity right now".  This
module gives them their own reason, ``backend_busy``, and splits the whole
taxonomy into two sets:

  WAIT-CLASS  overloaded, server_error, timeout, rate_limit, backend_busy
              The machine could not serve the request.  Retried against a TIME
              BUDGET, never against a count, and never counted against a card.

  REAL        auth_permanent, billing, context_overflow, payload_too_large,
              model_not_found, provider_policy_blocked
              Something about the request or the account is wrong.  Retrying
              cannot help; these keep the classic count-based limit.

Anything else stays ``unknown`` and is treated as REAL (fail safe: we would
rather surface an unknown error than wait twelve hours on it).

Stdlib only -- this module is imported both from inside the agent venv and from
a launchd-context reconciler that has no third-party dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

__all__ = [
    "BACKEND_BUSY",
    "WAIT_CLASS_REASONS",
    "REAL_REASONS",
    "Verdict",
    "is_wait_class",
    "looks_like_backend_busy",
    "is_local_backend",
    "refine",
    "classify",
]

# -- The taxonomy -----------------------------------------------------------

#: Our addition to the upstream ``FailoverReason`` set.  Kept as a plain string
#: so this module never has to import the agent to be useful, and so a value
#: round-trips through JSON (kanban event payloads) unchanged.
BACKEND_BUSY = "backend_busy"

#: Retried against a time budget; never counted against a card.
WAIT_CLASS_REASONS = frozenset({
    "overloaded",
    "server_error",
    "timeout",
    "rate_limit",
    BACKEND_BUSY,
})

#: Retried against a count, then escalated.  Waiting cannot fix these.
REAL_REASONS = frozenset({
    "auth_permanent",
    "billing",
    "context_overflow",
    "payload_too_large",
    "model_not_found",
    "provider_policy_blocked",
})


@dataclass(frozen=True)
class Verdict:
    """The outcome of classifying one API failure."""

    reason: str
    #: True when the caller should WAIT (time budget) rather than count retries.
    wait_class: bool
    #: ``"upstream"`` when the agent classifier's answer was kept as-is,
    #: ``"refined"`` when this module overrode it, ``"fallback"`` when the
    #: agent classifier was not importable and we matched on the message alone.
    source: str
    #: Human-readable justification -- goes into logs and escalation messages.
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "reason": self.reason,
            "wait_class": self.wait_class,
            "source": self.source,
            "detail": self.detail,
        }


def is_wait_class(reason: Any) -> bool:
    """True when ``reason`` (a string or a ``FailoverReason``) is WAIT-CLASS."""
    return _reason_value(reason) in WAIT_CLASS_REASONS


# -- Local-queue abort detection --------------------------------------------
#
# These are the signatures of a LOCAL model queue refusing or dropping a
# request.  They are deliberately narrow: each one is a phrase a local proxy
# (llama-swap / llama.cpp / a queueing shim) emits when it has no capacity, or
# when the request arrived while the router was swapping models.  A remote
# provider does not say any of these.
#
# Matching is substring-on-lowercased-message.  Regexes are only used where a
# phrase needs a wildcard.

_BACKEND_BUSY_PHRASES = (
    # The observed signature: the local proxy killed the stream mid-flight
    # because the slot went to someone else.
    "upstream disconnected mid-stream",
    "upstream disconnected",
    "upstream closed the connection",
    # Request parked behind / racing a model swap.
    "no router for requested model",
    "model is being loaded",
    "model is loading",
    "loading model",
    "swap in progress",
    "model swap",
    # Queue full / every slot taken.
    "all slots are busy",
    "no slot available",
    "server is busy",
    "queue is full",
    "capacity exceeded locally",
    # An admission-control layer explicitly halting callers.
    "halted by veto",
)

_BACKEND_BUSY_PATTERNS = (
    # llama.cpp style: "slots_processing=2/2" with nothing free.
    re.compile(r"slots?_processing=(\d+)/\1"),
    # A bare local gateway 502/503 with no body.
    re.compile(r"\b(502 bad gateway|503 service unavailable)\b"),
)

#: Hosts that mean "the model runs on this machine".
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def is_local_backend(base_url: Optional[str]) -> bool:
    """True when ``base_url`` points at a backend on this machine.

    ``backend_busy`` is a statement about a *local queue*.  A remote provider
    dropping a stream is an ordinary ``timeout`` and must keep its old
    behaviour, so the local check gates the reclassification.
    """
    if not base_url:
        return False
    raw = str(base_url).strip()
    try:
        parsed = urlparse(raw if "//" in raw else f"//{raw}")
        host = (parsed.hostname or "").lower()
    except ValueError:
        host = ""
    if not host:
        host = raw.split("/")[0].split(":")[0].lower()
    return host in _LOCAL_HOSTS or host.endswith(".local")


def looks_like_backend_busy(message: Optional[str]) -> bool:
    """True when the error text carries a local-queue abort signature."""
    if not message:
        return False
    text = str(message).lower()
    if any(phrase in text for phrase in _BACKEND_BUSY_PHRASES):
        return True
    return any(pattern.search(text) for pattern in _BACKEND_BUSY_PATTERNS)


# -- Refinement over the upstream classifier --------------------------------

def refine(
    upstream_reason: Any,
    message: Optional[str],
    *,
    base_url: Optional[str] = None,
    assume_local: bool = False,
) -> Verdict:
    """Refine an upstream ``FailoverReason`` into a :class:`Verdict`.

    The ONLY override performed is ``timeout``/``server_error``/``overloaded``
    -> ``backend_busy`` when the message carries a local-queue abort signature
    and the backend is local.  Every other upstream answer is respected: this
    module widens the taxonomy, it does not second-guess the classifier.

    ``assume_local`` exists for callers (the host-side reconciler) that read a
    finished run out of the board and know the deploy is local-only but have no
    ``base_url`` at hand.
    """
    reason = _reason_value(upstream_reason)
    local = assume_local or is_local_backend(base_url)

    if (
        reason in ("timeout", "server_error", "overloaded")
        and local
        and looks_like_backend_busy(message)
    ):
        return Verdict(
            reason=BACKEND_BUSY,
            wait_class=True,
            source="refined",
            detail=(
                "local backend queue aborted the request "
                f"(upstream classified it as {reason!r})"
            ),
        )

    if reason == BACKEND_BUSY:
        return Verdict(reason, True, "upstream", "local backend reported no capacity")

    if reason in WAIT_CLASS_REASONS:
        return Verdict(reason, True, "upstream", f"{reason} is wait-class")

    if reason in REAL_REASONS:
        return Verdict(reason, False, "upstream", f"{reason} is a real failure")

    # Unknown / transient auth / anything the agent grows later: treat as REAL
    # so an unrecognised error surfaces to a human instead of waiting 12 hours.
    label = reason or "unknown"
    return Verdict(label, False, "upstream", f"{label} treated as a real failure")


def classify(
    error: BaseException,
    *,
    base_url: Optional[str] = None,
    assume_local: bool = False,
    **classifier_kwargs: Any,
) -> Verdict:
    """Classify a live exception, using the real agent classifier when present.

    Falls back to message-only matching when ``agent.error_classifier`` cannot
    be imported (the reconciler runs outside the agent venv), in which case the
    verdict's ``source`` is ``"fallback"``.
    """
    message = str(error)
    upstream = _upstream_reason(error, **classifier_kwargs)
    if upstream is None:
        local = assume_local or is_local_backend(base_url)
        if local and looks_like_backend_busy(message):
            return Verdict(
                BACKEND_BUSY, True, "fallback",
                "local backend queue aborted the request (agent classifier unavailable)",
            )
        return Verdict("unknown", False, "fallback", "agent classifier unavailable")
    return refine(upstream, message, base_url=base_url, assume_local=assume_local)


# -- internals --------------------------------------------------------------

def _reason_value(reason: Any) -> str:
    """Accept a ``FailoverReason``, a plain string, or None."""
    if reason is None:
        return ""
    value = getattr(reason, "value", reason)
    return str(value)


def _upstream_reason(error: BaseException, **kwargs: Any) -> Optional[str]:
    try:
        from agent.error_classifier import classify_api_error  # type: ignore
    except Exception:
        return None
    try:
        classified = classify_api_error(error, **kwargs)
    except Exception:
        return None
    return _reason_value(getattr(classified, "reason", None)) or None
