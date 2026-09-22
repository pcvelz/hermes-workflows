#!/usr/bin/env python3
"""The communication agent.

The rule this module exists to enforce:

    **No card may ever sit silently blocked or given up.**

Whenever a card would be blocked, has given up, or is stranded with nobody able
to run it, a human is told -- over Mattermost (preferred), Telegram, or ntfy.
The message always carries the six things a human needs in order to act without
opening a terminal to find out what happened:

  1. card id and title
  2. which board
  3. what happened, in plain words
  4. the last error
  5. the exact command to resume it
  6. a pointer to the worker log

Secrets
-------
This module invents NO new secret and hardcodes NO token.  Every channel
resolves its credentials in the same order:

  1. an explicit value in the escalation config (operators who keep secrets in
     their own config store)
  2. an environment variable
  3. the macOS login Keychain -- the precedent set by the kanban ntfy notifier,
     chosen because a launchd job has no TTY and would hang forever on a vault
     unlock prompt
  4. for Mattermost only: ``MATTERMOST_TOKEN`` / ``MATTERMOST_URL`` already
     present in the gateway profile's env file, read at send time and never
     logged

If a channel cannot find its credentials it raises :class:`EscalationError`
with a message naming exactly which of the four sources it tried.  It never
falls back to "send nothing" silently -- a channel that cannot deliver is
itself an escalation-worthy condition, and :func:`escalate` reports it.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

__all__ = [
    "CHANNELS",
    "EscalationError",
    "Escalation",
    "render",
    "Sender",
    "MattermostSender",
    "TelegramSender",
    "NtfySender",
    "NullSender",
    "build_sender",
    "escalate",
]

CHANNELS = ("mattermost", "telegram", "ntfy", "none")

_HTTP_TIMEOUT = 15


class EscalationError(RuntimeError):
    """A human could not be reached."""


# -- the message ------------------------------------------------------------

#: Plain-words explanations, keyed by the kanban event / outcome that triggered
#: the escalation.  Deliberately written for a human reading a phone
#: notification at a traffic light, not for a log grepper.
_SITUATIONS = {
    "gave_up": (
        "The card hit its consecutive-failure limit and was taken off the "
        "board. It will NOT come back on its own."
    ),
    "blocked": (
        "The card is blocked and is waiting for a human. Nothing will pick it "
        "up until someone unblocks it."
    ),
    "crashed": (
        "The worker died without finishing the card."
    ),
    "timed_out": (
        "The worker ran past its runtime cap and was reaped before finishing."
    ),
    "spawn_failed": (
        "The dispatcher could not start a worker for this card."
    ),
    "backend_busy": (
        "The worker waited for model capacity until its wait budget ran out "
        "and stopped without completing the card. This is the machine's "
        "fault, not the card's."
    ),
    "budget_exhausted": (
        "The worker spent its entire model_wait_budget waiting for the model "
        "backend and never got capacity. This is the machine's fault, not the "
        "card's."
    ),
    "stranded": (
        "The card is ready but nobody can run it -- no worker has claimed it "
        "for far longer than the dispatch interval, and the dispatcher is not "
        "merely busy."
    ),
    "stalled": (
        "The card looks healthy and is going nowhere. Its worker is alive and "
        "still heartbeating, but it has made no progress for a long time -- "
        "a heartbeat means the process lives, not that the work moves. "
        "Left alone it will sit here until the runtime cap reaps it."
    ),
    "done_without_accept": (
        "This card reached done without your accept. On this board accept is "
        "the only door to done, so something else closed it: either you used "
        "the runtime's own complete, or an agent found a way. If it was you, "
        "there is nothing to do. If it was not, look now."
    ),
    "ledger_missing": (
        "The accept ledger for this board is missing or corrupt. It is the record of "
        "every card you accepted; without it an accepted card cannot be told from "
        "one that escaped, so accept and reopen are refused until it is restored "
        "from backup. Do not create an empty one."
    ),
    "illegal_move": (
        "This card changed column in a way the board's table does not allow. "
        "The harness blocks agents, so this came from somewhere it cannot "
        "reach: the dispatcher, or a command run by hand. Check the card; "
        "reopen it or move it back if the move was wrong."
    ),
    "harness_not_loaded": (
        "A Hermes profile can reach the kanban tools but the harness is not "
        "loaded in it, so nothing stops it completing, unblocking or archiving any "
        "card. This is the exact state the harness exists to prevent. Link the "
        "gated harness into the profile before it runs again."
    ),
    "handoff_form_missing": (
        "This board has a specification but no hand-off form, so the harness "
        "accepts any hand-off summary: no field, file or failed check is "
        "demanded. Add the `handoff:` key to the board file to close this."
    ),
    "ledger_shrank": (
        "The accept ledger for this board holds fewer accepts than it once did. It "
        "only ever grows, so it was truncated, or deleted and re-created. Every "
        "accept that vanished now looks like an escaped card. Restore it from "
        "backup."
    ),
    "agent_review_lane": (
        "This card is in the runtime's own review lane and is assigned to a "
        "real agent profile. The dispatcher will spawn that agent as a "
        "reviewer, and it can merge and set done without you. This board "
        "never puts a card there, so something else did."
    ),
    "backend_not_serving": (
        "A model backend is not serving. Workers asked this endpoint for this "
        "model and have received nothing for minutes; each says so in a note "
        "on its card. A backend's own status can read ready while every request "
        "to it dies, so check that the model answers, not that it is listed. "
        "The workers keep retrying in place: this is a wait, and it costs the "
        "cards nothing."
    ),
    "resolved": (
        "Cleared -- this card is moving again. No action needed; this is the "
        "closing line for the alert you got earlier."
    ),
    "protocol_violation": (
        "The worker exited without following the hand-off protocol, so the "
        "card has no verdict."
    ),
}

_TITLE_MARKS = {
    "gave_up": "✖",           # heavy multiplication x
    "blocked": "⏸",           # pause
    "crashed": "✖",
    "timed_out": "⏱",         # stopwatch
    "spawn_failed": "⚠",      # warning
    "backend_busy": "⏳",      # hourglass
    "budget_exhausted": "⏳",
    "stranded": "⚠",
    "stalled": "🛑",           # alive, but going nowhere
    "done_without_accept": "🚪",
    "agent_review_lane": "🚨",
    "ledger_missing": "🚨",
    "ledger_shrank": "🚨",
    "harness_not_loaded": "🚨",
    "handoff_form_missing": "⚠",
    "illegal_move": "⛔",
    "backend_not_serving": "🔌",
    "resolved": "✅",
    "protocol_violation": "⚠",
}


@dataclass
class Escalation:
    """Everything a human needs to act, assembled once and rendered per channel."""

    card_id: str
    title: str
    board: str
    #: Event / outcome name -- keys into the plain-words table.
    kind: str
    last_error: str = ""
    #: The exact command to put the card back in play.  Defaulted from
    #: ``kind`` when not supplied; an explicit value always wins.
    resume_command: str = ""
    #: Path to the worker log, as a human would open it.
    worker_log: str = ""
    #: Anything worth one extra line (failure counts, budget spent, ...).
    facts: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.resume_command:
            self.resume_command = default_resume_command(self.card_id, self.kind)

    @property
    def situation(self) -> str:
        return _SITUATIONS.get(
            self.kind, f"The card ended in an unexpected state ({self.kind})."
        )

    def as_dict(self) -> dict:
        return {
            "card_id": self.card_id,
            "title": self.title,
            "board": self.board,
            "kind": self.kind,
            "situation": self.situation,
            "last_error": self.last_error,
            "resume_command": self.resume_command,
            "worker_log": self.worker_log,
            "facts": dict(self.facts),
        }


def default_resume_command(card_id: str, kind: str) -> str:
    """The exact command that puts this card back in play.

    ``gave_up`` and ``blocked`` both leave the card at ``blocked`` with a
    tripped counter, so both need the unblock -- which is also what clears
    ``consecutive_failures``.  A stranded card is already ``ready``; it needs
    the dispatcher poked, not the card touched.
    """
    ident = shlex.quote(str(card_id))
    if kind == "resolved":
        return ""  # nothing to resume -- this is the all-clear
    if kind == "done_without_accept":
        return f"hermes kanban show {ident}   # see who closed it"
    if kind == "agent_review_lane":
        return (f"hermes kanban reassign {ident} user   "
                "# get it out of the agent lane before the dispatcher spawns a reviewer")
    if kind == "stranded":
        return f"hermes kanban show {ident}   # card is ready; check the dispatcher"
    if kind == "stalled":
        # The card is RUNNING with a live claim, so unblock is the wrong verb.
        # Releasing the claim is what puts it back in play; the hung worker is
        # then the operator's to kill, which is why the pid is in the facts.
        return f"hermes kanban reclaim {ident} --reason 'worker alive but idle'"
    return f"hermes kanban unblock {ident}"


def render(esc: Escalation) -> "RenderedMessage":
    """Render an :class:`Escalation` into a title and a body.

    One rendering for every channel: the body is short, plain-text, and
    readable as-is in a Mattermost post, a Telegram message, and an ntfy push.
    Markdown is limited to backticks, which degrade gracefully everywhere.
    """
    mark = _TITLE_MARKS.get(esc.kind, "⚠")
    title = f"{mark} kanban {esc.card_id} {esc.kind}"

    lines = [
        f"{mark} *{esc.card_id}* -- {esc.title or '(no title)'}",
        f"board: {esc.board or '(default)'}",
        "",
        esc.situation,
    ]
    if esc.last_error:
        lines += ["", f"last error: `{_one_line(esc.last_error, 300)}`"]
    if esc.facts:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(esc.facts.items()))
        lines.append(f"detail: {detail}")
    if esc.resume_command:
        lines += ["", "resume it with:", f"`{esc.resume_command}`"]
    if esc.worker_log:
        lines += ["", f"worker log: `{esc.worker_log}`"]

    return RenderedMessage(title=title, body="\n".join(lines))


@dataclass(frozen=True)
class RenderedMessage:
    title: str
    body: str


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


# -- credential resolution --------------------------------------------------

def _keychain(service: str, account: str) -> Optional[str]:
    """Read a generic password from the macOS login Keychain.

    Returns None on any failure (not macOS, not seeded, locked) so the caller
    can move on to the next source and report all of them together.
    """
    security = "/usr/bin/security"
    if not os.path.exists(security):
        return None
    try:
        proc = subprocess.run(
            [security, "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _env_file_value(path: Optional[str], key: str) -> Optional[str]:
    """Read ``KEY=value`` out of an env file without importing or logging it."""
    if not path:
        return None
    try:
        text = Path(os.path.expanduser(path)).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'") or None
    return None


def _resolve(
    label: str,
    *,
    config_value: Optional[str] = None,
    env_var: Optional[str] = None,
    keychain: Optional[Sequence[str]] = None,
    env_file: Optional[str] = None,
    env_file_key: Optional[str] = None,
) -> str:
    """Resolve one credential, reporting every source tried when it fails."""
    tried = []
    if config_value:
        return str(config_value)
    tried.append("config")
    if env_var:
        value = os.environ.get(env_var)
        if value:
            return value
        tried.append(f"${env_var}")
    if keychain:
        service, account = keychain
        value = _keychain(service, account)
        if value:
            return value
        tried.append(f"keychain(service={service}, account={account})")
    if env_file and env_file_key:
        value = _env_file_value(env_file, env_file_key)
        if value:
            return value
        tried.append(f"{env_file}:{env_file_key}")
    raise EscalationError(
        f"{label} not found. Tried, in order: {', '.join(tried)}. "
        "Seed one of them; this module never carries a token of its own."
    )


# -- channels ---------------------------------------------------------------

class Sender:
    """A delivery channel.  ``send`` raises :class:`EscalationError` on failure."""

    name = "abstract"

    def send(self, message: RenderedMessage) -> None:  # pragma: no cover
        raise NotImplementedError


class NullSender(Sender):
    """``channel: none`` -- records, delivers nothing.

    Kept explicit rather than allowing a missing channel to mean silence: an
    operator who disables escalation has to say so, and the sent messages stay
    inspectable in tests and dry runs.
    """

    name = "none"

    def __init__(self, cfg: Optional[Mapping[str, Any]] = None, **_: Any) -> None:
        self.sent: list = []

    def send(self, message: RenderedMessage) -> None:
        self.sent.append(message)


class MattermostSender(Sender):
    """Preferred channel: a post into a Mattermost channel via the bot token.

    Reuses the gateway bot that already exists -- no new bot, no new token.
    The token is read at send time from the gateway profile's env file if it is
    not in the environment or the Keychain, and is never written to a log.
    """

    name = "mattermost"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.channel_id = cfg.get("channel_id") or os.environ.get(
            "HERMES_ESCALATION_MATTERMOST_CHANNEL_ID", ""
        )
        if not self.channel_id:
            raise EscalationError(
                "escalation.mattermost.channel_id is required (or "
                "$HERMES_ESCALATION_MATTERMOST_CHANNEL_ID). It names the channel "
                "the human actually reads."
            )
        env_file = cfg.get("env_file") or os.environ.get("HERMES_GATEWAY_ENV_FILE")
        self.url = _resolve(
            "Mattermost URL",
            config_value=cfg.get("url"),
            env_var="MATTERMOST_URL",
            keychain=("hermes-escalation", "mattermost-url"),
            env_file=env_file, env_file_key="MATTERMOST_URL",
        ).rstrip("/")
        self._token = _resolve(
            "Mattermost bot token",
            config_value=cfg.get("token"),
            env_var="MATTERMOST_TOKEN",
            keychain=("hermes-escalation", "mattermost-token"),
            env_file=env_file, env_file_key="MATTERMOST_TOKEN",
        )

    def send(self, message: RenderedMessage) -> None:
        payload = json.dumps({
            "channel_id": self.channel_id,
            "message": f"**{message.title}**\n\n{message.body}",
        }).encode()
        request = urllib.request.Request(
            f"{self.url}/api/v4/posts",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        _post(request, self.name)


class TelegramSender(Sender):
    """Fallback channel: a DM to the owner's chat via an existing bot."""

    name = "telegram"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self._token = _resolve(
            "Telegram bot token",
            config_value=cfg.get("bot_token"),
            env_var="HERMES_ESCALATION_TELEGRAM_BOT_TOKEN",
            keychain=("hermes-escalation", "telegram-bot-token"),
        )
        self.chat_id = str(_resolve(
            "Telegram chat id",
            config_value=cfg.get("chat_id"),
            env_var="HERMES_ESCALATION_TELEGRAM_CHAT_ID",
            keychain=("hermes-escalation", "telegram-chat-id"),
        ))
        self.api_base = str(
            cfg.get("api_base") or "https://api.telegram.org"
        ).rstrip("/")

    def send(self, message: RenderedMessage) -> None:
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": f"{message.title}\n\n{message.body}",
            "disable_web_page_preview": True,
        }).encode()
        request = urllib.request.Request(
            f"{self.api_base}/bot{self._token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        _post(request, self.name)


class NtfySender(Sender):
    """Push channel, identical in auth and privacy model to the kanban ntfy
    notifier already running on this box -- same self-hosted server, same
    Keychain-resolved bearer token, same "unguessable topic is the identity"
    posture.  It is reused rather than reimplemented."""

    name = "ntfy"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.server = str(
            cfg.get("server") or os.environ.get("NTFY_SERVER") or ""
        ).rstrip("/")
        if not self.server:
            raise EscalationError(
                "escalation.ntfy.server is required (or $NTFY_SERVER)."
            )
        self.topic = cfg.get("topic") or os.environ.get("NTFY_TOPIC", "")
        if not self.topic:
            raise EscalationError(
                "escalation.ntfy.topic is required (or $NTFY_TOPIC)."
            )
        self._token = _resolve(
            "ntfy access token",
            config_value=cfg.get("token"),
            env_var="NTFY_TOKEN",
            keychain=(
                cfg.get("keychain_service") or "ntfy-token",
                cfg.get("keychain_account") or "ntfy",
            ),
        )

    def send(self, message: RenderedMessage) -> None:
        request = urllib.request.Request(
            f"{self.server}/{self.topic}",
            data=message.body.encode(),
            headers={
                # ntfy's Title header is header-encoded; keep it ASCII.
                "Title": message.title.encode("ascii", "ignore").decode(),
                "Authorization": f"Bearer {self._token}",
                "Priority": "high",
            },
            method="POST",
        )
        _post(request, self.name)


_SENDERS = {
    "mattermost": MattermostSender,
    "telegram": TelegramSender,
    "ntfy": NtfySender,
    "none": NullSender,
}


def build_sender(config: Optional[Mapping[str, Any]]) -> Sender:
    """Build the configured channel's sender.

    ``config`` is the ``escalation:`` block (or the whole config tree).  An
    unset channel is an ERROR, not a silent ``none``: this whole module exists
    because silence is the failure mode we are removing.
    """
    cfg = _escalation_config(config)
    channel = str(cfg.get("channel") or "").strip().lower()
    if not channel:
        raise EscalationError(
            "escalation.channel is not set. Choose one of "
            f"{', '.join(CHANNELS)} -- 'none' is a valid, explicit choice."
        )
    if channel not in _SENDERS:
        raise EscalationError(
            f"unknown escalation.channel {channel!r}; expected one of "
            f"{', '.join(CHANNELS)}"
        )
    channel_cfg = cfg.get(channel) if isinstance(cfg.get(channel), Mapping) else {}
    return _SENDERS[channel](channel_cfg)


def escalate(
    esc: Escalation,
    config: Optional[Mapping[str, Any]] = None,
    *,
    sender: Optional[Sender] = None,
) -> Dict[str, Any]:
    """Tell a human.  Returns a result record; never raises on send failure.

    A failed send is reported (``delivered=False`` plus ``error``) rather than
    raised, because the caller is almost always a loop over several stranded
    cards and one unreachable channel must not stop the rest.  The caller is
    expected to log the record -- a card that could not be escalated is the one
    thing worse than a card that was.
    """
    message = render(esc)
    record: Dict[str, Any] = {
        "card_id": esc.card_id,
        "kind": esc.kind,
        "title": message.title,
        "body": message.body,
        "delivered": False,
        "channel": None,
        "error": None,
    }
    try:
        target = sender if sender is not None else build_sender(config)
        record["channel"] = target.name
        target.send(message)
        record["delivered"] = True
    except EscalationError as exc:
        record["error"] = str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


# -- internals --------------------------------------------------------------

def _escalation_config(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    nested = config.get("escalation")
    if isinstance(nested, Mapping):
        return nested
    return config


def _post(request: urllib.request.Request, channel: str) -> None:
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            if response.status >= 300:
                raise EscalationError(
                    f"{channel} rejected the message: HTTP {response.status}"
                )
    except urllib.error.HTTPError as exc:
        # Body may name the real problem (bad channel id, revoked token).
        try:
            detail = _one_line(exc.read().decode("utf-8", "replace"), 200)
        except Exception:
            detail = ""
        raise EscalationError(
            f"{channel} rejected the message: HTTP {exc.code} {detail}".strip()
        ) from exc
    except urllib.error.URLError as exc:
        raise EscalationError(f"{channel} unreachable: {exc.reason}") from exc
    except OSError as exc:
        raise EscalationError(f"{channel} send failed: {exc}") from exc
