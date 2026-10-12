"""claude-code-bridge worker: one in-flight message per channel, FIFO for the rest.

``tick`` is one pass over every channel: collect the reply of the in-flight turn,
enforce the wait budget, start the session when work is queued, inject the next
message when the session is idle, and stop idle sessions. All session work goes
through the ``session`` module so tests can replace it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from typing import Any, Callable

from . import blocked, chat_hooks, config, poster, session, spool

logger = logging.getLogger("claude_code_bridge")

WAIT_FAILURE_TEXT = "Sorry, I could not answer this one."

# A failed session start is retried after 2 s, 4 s, 8 s, ... capped at 5 min; success resets it.
START_RETRY_BASE_S = 2
START_RETRY_MAX_S = 300

# A reply may hold several posts: a line holding only this marker ends one post and starts
# the next. Each part is posted in order on the same thread; empty parts are dropped.
POST_SPLIT_MARKER = "---post---"
_POST_SPLIT_RE = re.compile(r"^[ \t]*" + re.escape(POST_SPLIT_MARKER) + r"[ \t]*$", re.MULTILINE)

_thread_lock = threading.Lock()
_thread: threading.Thread | None = None


def format_message(item: dict[str, Any]) -> str:
    """Prefix the text with its local time and sender: ``[YYYY-MM-DD HH:MM · user] text``.

    Uses the item's epoch ``ts``; falls back to now when it is missing.
    """
    ts = item.get("ts")
    stamp = time.time() if ts is None else float(ts)
    when = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    return f"[{when} · {item.get('user') or 'unknown'}] {item.get('text', '')}"


DEBRIEF_PREFIX = "[operator debrief — answer the operator, not the chat user]"


def _operator(cfg: dict[str, Any], cid: str) -> str:
    """The channel's operator channel id ("" when unset): its own ``operator_channel`` override, else the global one.
    A value equal to the bridged channel itself counts as unset: nothing is ever posted back into the bridged channel."""
    operator = str(config.for_channel(cfg, cid).get("operator_channel") or "").strip()
    return "" if operator == cid else operator


def _file_debrief(cfg: dict[str, Any], cid: str, text: str) -> Any:
    """Write a debrief answer to ``<channel dir>/debrief/<time_ns>.md`` (the default sink; the operator reads it there).
    Logs the path only, never the content."""
    folder = spool.channel_dir(cfg, cid) / "debrief"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.time_ns()}.md"
    path.write_text(text, encoding="utf-8")
    logger.info("claude-code-bridge: debrief answer for channel %s written to %s", cid, path)
    return path


def _route_debrief(cfg: dict[str, Any], cid: str, item: dict[str, Any]) -> None:
    """A debrief turn was injected: the answer goes to the operator channel (when one is set and in the same team), on
    the bridged channel's platform (the operator channel's own spool state carries it, as senders.send reads it there),
    and the operator's ``!debrief`` post is deleted (best-effort; the operator's own post)."""
    operator = _operator(cfg, cid)
    if operator:
        try:
            spool.save_state(cfg, operator, {**spool.load_state(cfg, operator), "platform": item.get("platform")})
        except ValueError:
            logger.warning("claude-code-bridge: operator_channel is not a usable channel id; answers are filed only")
    if item.get("post_id"):
        poster.delete_post(str(item["post_id"]))


def inject_text(cfg: dict[str, Any], cid: str, item: dict[str, Any]) -> str:
    """The text typed into the session for one chat message. A debrief: the operator prefix, then the question.
    Chat via hooks on: the rendered prompt_context, then the message text; off: the usual ``[time · sender] text`` line."""
    if item.get("control") == "debrief":
        return f"{DEBRIEF_PREFIX}\n{item.get('text', '')}"
    hooks = config.chat_hooks_texts(config.for_channel(cfg, cid))
    if hooks is None:
        return format_message(item)
    return chat_hooks.prompt_text(hooks, spool.channel_dir(cfg, cid), item)


def _reply_root(cfg: dict[str, Any], inflight: dict[str, Any]) -> str | None:
    """Where a reply goes: the user's own thread if they wrote in one; the user's post only
    when ``reply_in_thread`` is set; otherwise top-level (None)."""
    if inflight.get("root_id"):
        return inflight["root_id"]
    if cfg.get("reply_in_thread") and inflight.get("post_id"):
        return inflight["post_id"]
    return None


def _apply_reset(cfg: dict[str, Any], cid: str, now: float) -> None:
    """Handle queued ``/reset`` control items before any chat message on the channel.

    Stops the session, drops the inbox up to and including the last reset (messages sent
    after it are kept for the new session), clears the outbox and resets the session state
    so the next message starts a NEW Claude Code conversation. Posts nothing to chat.
    """
    paths = spool.pending(cfg, cid)
    last = None
    last_data: dict[str, Any] = {}
    for i, path in enumerate(paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("control") == "reset":
            last = i
            last_data = data
    if last is None:
        return
    session.stop(cfg, cid)
    for path in paths[: last + 1]:
        path.unlink(missing_ok=True)
    for path in (spool.channel_dir(cfg, cid) / "outbox").glob("*.json"):
        path.unlink(missing_ok=True)
    state = spool.load_state(cfg, cid)
    reset_ts = float(last_data.get("ts") or now)
    if last_data.get("clear"):
        _clear_channel(cid, state.get("last_reset_ts"), reset_ts, str(last_data.get("post_id") or ""))
    state.update(session_id=None, started_once=False, inflight=None, inflight_since=None, last_activity=now,
                 start_failures=0, start_retry_at=None, last_reset_ts=reset_ts)
    spool.save_state(cfg, cid, state)
    logger.info("claude-code-bridge: reset session on channel %s", cid)


CLEAR_FALLBACK_S = 24 * 3600  # "!reset clear" with no stored reset point deletes only what is newer than this


def _clear_channel(cid: str, previous_ts: Any, reset_ts: float, reset_post_id: str = "") -> None:
    """``!reset clear``: delete every post in the channel since the previous reset (all users, the bot's own
    included; system posts kept), up to and including the ``!reset clear`` post itself: messages sent after it
    survive (they belong to the new session). The upper bound is that post's own ``create_at``; when it is not in
    the listing, the reset's ``ts``. With no stored reset point, only posts from the last 24 h. Best-effort: a
    failed listing or delete is logged and not retried. Posts nothing to chat."""
    since = float(previous_ts) if previous_ts is not None else reset_ts - CLEAR_FALLBACK_S
    posts = poster.channel_posts_since(cid, int(since * 1000))
    if posts is None:
        logger.warning("claude-code-bridge: /reset clear could not list channel %s; nothing deleted", cid)
        return
    until = int(reset_ts * 1000)
    for post in posts:
        if reset_post_id and str(post.get("id") or "") == reset_post_id:
            until = int(post.get("create_at") or until)
            break
    deleted = failed = 0
    for post in posts:
        if str(post.get("type") or "").startswith("system_"):
            continue
        if int(post.get("create_at") or 0) > until:
            continue  # sent after the !reset clear post: kept
        if poster.delete_post(str(post.get("id") or "")):
            deleted += 1
        else:
            failed += 1
            logger.warning("claude-code-bridge: /reset clear could not delete a post on channel %s", cid)
    logger.info("claude-code-bridge: /reset clear on channel %s deleted %d post(s), %d failed", cid, deleted, failed)


USER_INPUT_START = "<!-- @user-input:start -->"
USER_INPUT_END = "<!-- @user-input:end -->"
_NONE_YET = {"(none yet)", "- (none yet)"}


def record_user_input(cfg: dict[str, Any], cid: str, item: dict[str, Any]) -> bool:
    """Append one line for an injected user message to the channel's ``@user-input`` block.

    Active only when the channel sets ``record_user_input: {file: <name>, exclude_users: [...]}``.
    Skipped for control items and excluded senders. The line is
    ``- YYYY-MM-DD HH:MM @user-input (<user>): <text>``, written inside the block delimited by
    ``USER_INPUT_START`` / ``USER_INPUT_END`` in ``<channel dir>/<file>``; a ``(none yet)`` line is
    replaced. A line already present is not written twice (a re-queued item keeps its timestamp).
    Missing markers or an unreadable file: nothing is written, the problem is logged. Returns True
    when the line is recorded or already present.
    """
    channels = cfg.get("channels")
    override = channels.get(cid) if isinstance(channels, dict) else None
    rule = override.get("record_user_input") if isinstance(override, dict) else None
    if not isinstance(rule, dict) or not rule.get("file"):
        return False
    if item.get("control"):
        return False
    excluded = rule.get("exclude_users") or []
    if isinstance(excluded, str):
        excluded = [excluded]
    if str(item.get("user")) in {str(u) for u in excluded}:
        return False
    path = spool.channel_dir(cfg, cid) / str(rule["file"])
    stamp = datetime.fromtimestamp(float(item.get("ts") or time.time())).strftime("%Y-%m-%d %H:%M")
    text = " ".join(str(item.get("text") or "").split())
    line = f"- {stamp} @user-input ({item.get('user') or 'unknown'}): {text}"
    try:
        doc = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("claude-code-bridge: cannot read %s (%s); user input not recorded", path.name, type(exc).__name__)
        return False
    start = doc.find(USER_INPUT_START)
    end = doc.find(USER_INPUT_END, start + 1) if start >= 0 else -1
    if start < 0 or end < 0:
        logger.warning("claude-code-bridge: @user-input markers missing in %s on channel %s; nothing recorded",
                       path.name, cid)
        return False
    kept = [ln for ln in doc[start + len(USER_INPUT_START):end].splitlines()
            if ln.strip() and ln.strip() not in _NONE_YET]
    if line in kept:
        return True
    new_doc = doc[:start] + USER_INPUT_START + "\n" + "\n".join(kept + [line]) + "\n" + doc[end:]
    try:
        spool._atomic_write(path, new_doc)
    except OSError as exc:
        logger.warning("claude-code-bridge: writing %s failed (%s); user input not recorded", path.name, type(exc).__name__)
        return False
    return True


# A real post failure is retried after 2 s, 4 s, 8 s, ... capped at 5 min. At the attempt cap the
# reply is parked: it stays in the outbox and is posted after the next restart.
POST_RETRY_BASE_S = 2
POST_RETRY_MAX_S = 300
MAX_REPLY_POST_ATTEMPTS = 20


def split_post_parts(text: str) -> list[str]:
    """The non-empty parts of a reply, split at ``---post---`` lines."""
    return [part.strip() for part in _POST_SPLIT_RE.split(text or "") if part.strip()]


def _post_reply(post_fn: Callable[[str, Any, str], Any], cid: str, thread_root: Any, text: str) -> tuple[list[str], bool]:
    """Post each part of a reply in order, on the same thread.

    Returns ``(remaining, not_ready)``: the parts that were not posted (empty when every part went
    out) and whether the stop was a missing adapter (a wait) rather than a failed post. A stopped
    part ends the run, so the caller can keep exactly the unposted remainder for a retry.
    """
    parts = split_post_parts(text)
    for index, part in enumerate(parts):
        ok = _post_one(post_fn, cid, thread_root, part)
        if ok is not True:
            return parts[index:], ok is None
    return [], False


def _post_one(post_fn: Callable[[str, Any, str], Any], cid: str, thread_root: Any, text: str) -> bool | None:
    """Post one part. Returns True when posted, None when the adapter is not live yet, else False.

    ``None`` (the poster's "no live adapter") is a wait, not a failed post. A raised exception or a
    falsy return value counts as a failed post.
    """
    try:
        ok = post_fn(cid, thread_root, text)
    except Exception as exc:  # the reply must survive a crashing poster
        logger.warning("claude-code-bridge: reply post raised %s on channel %s", type(exc).__name__, cid)
        return False
    if ok is None:
        return None
    if not ok:
        logger.warning("claude-code-bridge: reply post failed on channel %s", cid)
    return bool(ok)


def _after_failed_post(data: dict[str, Any], now: float) -> bool:
    """Count one real post failure on ``data`` (in place) and set its retry time. True = cap reached."""
    attempts = int(data.get("post_attempts") or 0) + 1
    data["post_attempts"] = attempts
    if attempts >= MAX_REPLY_POST_ATTEMPTS:
        return True
    data["retry_at"] = now + min(POST_RETRY_BASE_S * 2 ** (attempts - 1), POST_RETRY_MAX_S)
    return False


def _after_not_ready(cid: str, data: dict[str, Any], now: float, budget_s: float) -> bool:
    """Hold ``data`` (in place) while its adapter is missing; no attempt is used. True = wait budget spent."""
    if data.get("not_ready_since") is None:
        data["not_ready_since"] = now
        logger.warning("claude-code-bridge: adapter not live for channel %s; reply held in outbox", cid)
    return now - float(data["not_ready_since"]) > budget_s


def _park(cid: str, data: dict[str, Any], thread_root: Any, reason: str) -> dict[str, Any]:
    """Turn an undeliverable reply into a parked notice (in place). Notices are never taken as
    replies, and parked notices are skipped until the next restart releases them."""
    data.update(notice=True, parked=True, thread_root=thread_root, retry_at=None, not_ready_since=None)
    logger.error("claude-code-bridge: reply on channel %s kept in outbox, not delivered (%s); "
                 "it is posted after the next restart", cid, reason)
    return data


def _unpark_all(cfg: dict[str, Any]) -> None:
    """Release every parked reply for delivery (once per process start: a restart is a fresh try)."""
    released = 0
    for cid in spool.channels(cfg):
        for path in (spool.channel_dir(cfg, cid) / "outbox").glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not data.get("parked"):
                continue
            data.pop("parked", None)
            data.update(post_attempts=0, retry_at=None, not_ready_since=None)
            spool._atomic_write(path, json.dumps(data, ensure_ascii=False))
            released += 1
    if released:
        logger.info("claude-code-bridge: %d parked reply(ies) released for delivery after restart", released)


def _start_session(cfg: dict[str, Any], cid: str, state: dict[str, Any], now: float) -> bool:
    """Start the session and record the launch fingerprint it was started with. Saves state."""
    started = session.start(cfg, cid, state)
    if started:
        state["launch_fingerprint"] = session.fingerprint(cfg, cid)
        state["last_activity"] = now
        state["start_failures"] = 0
        state["start_retry_at"] = None
        spool.save_state(cfg, cid, state)
        return True
    failures = int(state.get("start_failures") or 0) + 1
    delay = min(START_RETRY_BASE_S * 2 ** (failures - 1), START_RETRY_MAX_S)
    state["start_failures"] = failures
    state["start_retry_at"] = now + delay
    spool.save_state(cfg, cid, state)
    logger.warning("claude-code-bridge: session start failed on channel %s; retry in %d s", cid, delay)
    return False


def _recover_inflight(cfg: dict[str, Any], cid: str, state: dict[str, Any], now: float) -> None:
    """Resume an in-flight turn whose session is gone: relaunch on the same Claude conversation, inject once.

    ``inflight_redelivered_at`` is saved BEFORE the injection, so a later session loss never injects the
    message a second time (the wait budget then decides). A failed injection was not delivered: the
    message goes back to the front of the queue and the marker is cleared. Relaunch obeys the start backoff.
    """
    if state.get("inflight_redelivered_at") is not None:
        return
    if now < float(state.get("start_retry_at") or 0):
        return  # backing off after a failed start; retried when the timer expires
    inflight = state["inflight"]
    if not _start_session(cfg, cid, state, now):
        return
    state["inflight_redelivered_at"] = now
    spool.save_state(cfg, cid, state)
    if session.inject(cfg, cid, inject_text(cfg, cid, inflight)):
        logger.info("claude-code-bridge: resumed in-flight turn on channel %s after session loss", cid)
        return
    logger.warning("claude-code-bridge: redelivery inject failed on channel %s; message re-queued", cid)
    state["inflight"] = None
    state["inflight_since"] = None
    state["inflight_redelivered_at"] = None
    spool.enqueue(cfg, cid, inflight, name="0")  # sorts before every queued item: it was the oldest


BACKEND_PROBE_URL = "http://127.0.0.1:8003/v1/models"  # the background tier Claude Code runs on; overridable
BACKEND_PROBE_TIMEOUT_S = 3


def _backend_up(cfg: dict[str, Any]) -> bool:
    """True when the model backend answers its model list. Any error counts as down."""
    url = str(cfg.get("backend_probe_url") or BACKEND_PROBE_URL)
    try:
        with urllib.request.urlopen(url, timeout=BACKEND_PROBE_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except Exception:  # refused, timed out, HTTP error: all mean the backend cannot serve a turn now
        return False


def _recover_backend_outage(cfg: dict[str, Any], cid: str, state: dict[str, Any], now: float) -> None:
    """A turn in flight across a backend outage is redelivered once after the backend is back.

    The Claude Code session can survive an outage with its request hung, so it never goes idle and
    queued messages wait behind it for the whole wait budget. Once the backend answers again, a
    turn that was started before the outage and is still in flight gets a fresh session on the same
    conversation (``--resume``) and its message is injected once more. ``inflight_redelivered_at``
    guards the second injection, so it never repeats.
    """
    down_at = state.get("backend_down_at")
    if down_at is None or not _backend_up(cfg):
        return
    state["backend_down_at"] = None
    inflight = state.get("inflight")
    if not inflight or state.get("inflight_redelivered_at") is not None:
        return
    if float(inflight.get("ts") or inflight.get("received_at") or 0) > float(down_at):
        return  # the turn started during the outage: it was never sent to a live backend
    if session.exists(cfg, cid) and session.is_idle(cfg, cid):
        return  # idle, not hung: the turn ended without a reply, and a relaunch would only duplicate the message
    logger.warning("claude-code-bridge: backend back after outage on channel %s with a turn still in flight; "
                   "relaunching session and redelivering once", cid)
    session.stop(cfg, cid)
    state["started_once"] = True
    if not _start_session(cfg, cid, state, now):
        return
    state["inflight_redelivered_at"] = now
    spool.save_state(cfg, cid, state)
    if not session.inject(cfg, cid, inject_text(cfg, cid, inflight)):
        logger.warning("claude-code-bridge: outage redelivery inject failed on channel %s; message re-queued", cid)
        state["inflight"] = None
        state["inflight_since"] = None
        state["inflight_redelivered_at"] = None
        spool.enqueue(cfg, cid, inflight, name="0")


def _note_backend(cfg: dict[str, Any], cid: str, state: dict[str, Any], now: float) -> None:
    """Record when the backend went down while a turn is in flight; no turn in flight clears the mark."""
    if not state.get("inflight"):
        if state.get("backend_down_at") is not None:
            state["backend_down_at"] = None
        return
    if state.get("backend_down_at") is None and not _backend_up(cfg):
        state["backend_down_at"] = now
        logger.warning("claude-code-bridge: backend unreachable while a turn is in flight on channel %s", cid)


def _post_notices(cfg: dict[str, Any], cid: str, post_fn: Callable[[str, Any, str], Any], now: float) -> None:
    """Post outbox items marked ``"notice": true`` top-level, whether or not a message is in flight.

    A notice never touches the session state. A missing adapter holds it with no attempt used; a
    failed post backs off and is parked at the attempt cap. Parked notices wait for a restart.
    """
    budget_s = float(cfg.get("wait_budget_minutes", 360)) * 60
    outbox = spool.channel_dir(cfg, cid) / "outbox"
    for path in sorted(outbox.glob("*.json"), key=lambda p: (len(p.stem), p.stem)):
        if not spool.is_notice(path):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("parked") or now < float(data.get("retry_at") or 0):
            continue
        thread_root = data.get("thread_root")
        remaining, not_ready = _post_reply(post_fn, cid, thread_root, str(data.get("text", "")))
        if not remaining:  # empty remainder = all posted
            path.unlink(missing_ok=True)
            continue
        data["text"] = ("\n" + POST_SPLIT_MARKER + "\n").join(remaining)
        if not_ready:
            parked = _after_not_ready(cid, data, now, budget_s)
        else:
            parked = _after_failed_post(data, now)
        if parked:
            data = _park(cid, data, thread_root, "adapter not live within the wait budget" if not_ready
                         else "post failed at the attempt cap")
        spool._atomic_write(path, json.dumps(data, ensure_ascii=False))


BLOCKED_ACCEPT_MAX_PRESSES = 3  # the first Enter plus two retries on an unchanged dialog, then notify
BLOCKED_NOTE = ("The dialog '{question}' was declined because nobody can answer it here. "
                "Finish the task without that step, and say in your reply what was not done.")


def _dialog_text(cfg: dict[str, Any], cid: str, found: dict[str, Any], mode: str) -> str:
    """The operator notice for a blocked dialog: channel name, question and options read from the pane."""
    name = poster.channel_name(cid) or cid
    lines = [f"Blocked prompt on {name}: {found['question']}"] + [f"  {option}" for option in found["options"]]
    if found["exempt"]:
        lines.append(f"Not answered automatically ({found['exempt']}); someone has to answer it in the session.")
    elif mode == "decline":
        lines.append(f"Nobody can answer it here; it is declined (Escape) after {int(float(cfg.get('blocked_timeout_s', 600)))} s.")
    elif mode == "accept":
        lines.append("The bridge could not get past it; nothing was answered.")
    else:
        lines.append("Nothing is answered automatically.")
    return "\n".join(lines)


def _notify_operator(cfg: dict[str, Any], cid: str, found: dict[str, Any], mode: str) -> bool:
    """Post the blocked dialog to ``operator_channel``. True = done (posted, or nowhere to post it); False = retry.

    Never posts to the bridged channel itself. An unset operator channel is a WARNING, not an error.
    """
    operator = _operator(cfg, cid)
    if not operator:
        logger.warning("claude-code-bridge: channel %s is waiting on a dialog (%s) but operator_channel is not set; "
                       "nothing posted", cid, found["question"])
        return True
    if not poster.same_team(cid, operator):
        return True  # another team, or unknown: never posted (a WARNING was logged); not retried
    max_chars = int(config.for_channel(cfg, cid).get("max_reply_chars", 15000))
    return bool(poster.post(operator, None, _dialog_text(cfg, cid, found, mode), max_chars))


def _check_blocked(cfg: dict[str, Any], cid: str, state: dict[str, Any], now: float) -> None:
    """Blocked-prompt state machine for one channel (state changed in place; the caller saves it).

    A dialog counts once its option-block signature has stayed the same for ``blocked_settle_s``. Then, by mode:
    notify posts it once; decline also presses Escape after ``blocked_timeout_s`` (from first sight) and queues a
    note for the next idle session; accept presses Enter on the first option, at most three times, a settle
    window apart, then notifies. Exempt dialogs are never answered; they are only notified.
    """
    mode = blocked.mode(config.for_channel(cfg, cid))
    if mode == "off":
        state.pop("blocked_candidate", None)
        state.pop("blocked", None)
        return
    pane = session.capture(cfg, cid) if session.exists(cfg, cid) else ""
    found = blocked.detect(pane if isinstance(pane, str) else "")
    if found is None:  # no dialog (answered, declined, or never there): start over
        state.pop("blocked_candidate", None)
        state.pop("blocked", None)
        return
    settle = float(cfg.get("blocked_settle_s", 60))
    timeout = float(cfg.get("blocked_timeout_s", 600))
    cand = state.get("blocked_candidate")
    if not isinstance(cand, dict) or cand.get("sig") != found["sig"]:
        state["blocked_candidate"] = {"sig": found["sig"], "since": now}
        return
    since = float(cand["since"])
    if now - since < settle:
        return
    current = state.get("blocked")
    if not isinstance(current, dict) or current.get("sig") != found["sig"]:
        current = {"sig": found["sig"], "since": since, "notified": False, "declined": False,
                   "presses": 0, "last_press_at": None, "fallback": False}
        state["blocked"] = current
        logger.info("claude-code-bridge: channel %s is waiting on a dialog: %s", cid, found["question"])
    if mode == "accept" and found["exempt"] is None and not current["fallback"]:
        last = current["last_press_at"]
        if last is not None and now - float(last) < settle:
            return  # the last Enter gets a full settle window before another decision
        if current["presses"] >= BLOCKED_ACCEPT_MAX_PRESSES:
            current["fallback"] = True  # the dialog survived every Enter: notify instead
        elif session.press(cfg, cid, "Enter"):
            current["presses"] += 1
            current["last_press_at"] = now
            logger.info("claude-code-bridge: accepted dialog on channel %s (press %d of %d): %s", cid,
                        current["presses"], BLOCKED_ACCEPT_MAX_PRESSES, found["question"])
            return
        else:
            return  # the key was not sent: retried on the next tick
    if (mode in ("notify", "decline") or current["fallback"] or found["exempt"] is not None) and not current["notified"]:
        current["notified"] = _notify_operator(cfg, cid, found, mode)
    if mode == "decline" and not current["declined"] and now - since >= timeout:
        if session.press(cfg, cid, "Escape"):
            current["declined"] = True
            state["blocked_note"] = BLOCKED_NOTE.format(question=found["question"])
            logger.warning("claude-code-bridge: declined a dialog on channel %s after %d s (Escape): %s",
                           cid, int(timeout), found["question"])


def _tick_channel(cfg: dict[str, Any], cid: str, post_fn: Callable[[str, Any, str], Any], now: float) -> None:
    _apply_reset(cfg, cid, now)
    _post_notices(cfg, cid, post_fn, now)
    state = spool.load_state(cfg, cid)
    inflight = state.get("inflight")
    budget_s = float(cfg.get("wait_budget_minutes", 360)) * 60
    if inflight or state.get("blocked") or state.get("blocked_candidate"):
        _check_blocked(cfg, cid, state, now)  # a dialog can only block a turn in flight
        spool.save_state(cfg, cid, state)  # kept even when a later step returns early
    _note_backend(cfg, cid, state, now)  # a turn in flight across a backend outage is recovered below

    # 1-3: collect the reply of the in-flight turn, or enforce the wait budget. The reply guard is not here:
    # the Stop hook blocks a give-up reply before it reaches the outbox (stop_hook.py).
    # A reply with no turn in flight is a self-initiated turn (a background task finished and woke the session):
    # it goes to the thread the channel last replied in.
    reply = spool.take_reply(cfg, cid)
    last_reply = state.get("last_reply_to")
    self_initiated = bool(not inflight and reply is not None and isinstance(last_reply, dict))
    # Chat via hooks: an interim item (a message sent mid-turn by chat_send.py) is posted but does not end the
    # turn; the turn ends with the Stop hook's final item (or its turn_end marker).
    interim = reply is not None and bool(reply.get("interim"))
    # An operator debrief turn answers the operator channel, top-level, never the bridged channel.
    debrief_turn = bool(inflight and inflight.get("control") == "debrief")
    operator = _operator(cfg, cid) if debrief_turn else ""
    reply_cid = operator if debrief_turn else cid
    if inflight or self_initiated or interim:
        if debrief_turn:
            thread_root = None
        elif inflight:
            thread_root = _reply_root(cfg, inflight)
        else:
            thread_root = last_reply.get("root_id") if isinstance(last_reply, dict) else None
        if reply is not None and now < float(reply.get("retry_at") or 0):
            spool.put_reply(cfg, cid, reply)  # backing off after a failed post; retried when the timer expires
        elif reply is not None:
            if debrief_turn and not reply.get("filed"):
                # Filed once, on the first attempt: the spool file is the answer; a post to the operator is a copy.
                _file_debrief(cfg, cid, str(reply.get("text", "")))
                reply["filed"] = True
                reply["to_operator"] = bool(operator) and poster.same_team(cid, operator)
            if debrief_turn and not reply.get("to_operator"):
                remaining, not_ready = [], False  # filed only: nothing goes to chat, and nothing to another team
            else:
                remaining, not_ready = _post_reply(post_fn, reply_cid, thread_root, reply.get("text", ""))
            if not remaining:
                if not interim:
                    state["inflight"] = None
                    state["inflight_since"] = None
                if debrief_turn and not interim:
                    spool.set_debrief(cfg, cid, False)  # the debrief turn is over
                state["last_activity"] = now
                state["last_reply_to"] = {"root_id": thread_root}
                if self_initiated:
                    logger.info("claude-code-bridge: delivered self-initiated turn on channel %s", cid)
            else:
                # Keep only the parts not yet posted, so a retry never repeats a delivered part.
                reply["text"] = ("\n" + POST_SPLIT_MARKER + "\n").join(remaining)
                if not_ready:
                    parked = _after_not_ready(cid, reply, now, budget_s)
                else:
                    parked = _after_failed_post(reply, now)
                if parked:
                    _park(cid, reply, thread_root, "adapter not live within the wait budget" if not_ready
                          else "post failed at the attempt cap")
                    if not interim:
                        state["inflight"] = None
                        state["inflight_since"] = None
                    state["last_activity"] = now
                if not parked and not not_ready:
                    logger.warning("claude-code-bridge: will retry reply on channel %s in %d s (attempt %d of %d)",
                                   cid, int(reply["retry_at"] - now), int(reply["post_attempts"]) + 1,
                                   MAX_REPLY_POST_ATTEMPTS)
                spool.put_reply(cfg, cid, reply)  # kept in the outbox until posted, retried, or parked
        elif inflight and now - float(state.get("inflight_since") or 0) > budget_s:
            post_fn(cid, thread_root, WAIT_FAILURE_TEXT)
            state["inflight"] = None
            state["inflight_since"] = None
    elif reply is not None:
        logger.info("claude-code-bridge: dropped a reply with no in-flight message on channel %s", cid)

    # 3b: the in-flight turn's session is gone (machine reboot, tmux server died): resume it once.
    if state.get("inflight") and reply is None and not session.exists(cfg, cid):
        _recover_inflight(cfg, cid, state, now)

    # 3b2: the backend was down during the turn and is back, and the turn is still unfinished: the session
    # is likely hung on the lost request. Relaunch on the same conversation and redeliver once.
    if state.get("inflight") and reply is None:
        _recover_backend_outage(cfg, cid, state, now)

    # 3c: a decline note goes in as soon as the session is idle (in flight or not), so the turn ends in chat.
    note = state.get("blocked_note")
    if note and session.exists(cfg, cid) and session.is_idle(cfg, cid):
        if session.inject(cfg, cid, note):
            state["blocked_note"] = None
            logger.info("claude-code-bridge: decline note injected on channel %s", cid)

    # 4: nothing in flight -> start the session if needed, then inject the next message.
    if not state.get("inflight"):
        queued = spool.pending(cfg, cid)
        if queued:
            if not session.exists(cfg, cid):
                if now < float(state.get("start_retry_at") or 0):
                    return  # backing off after a failed start; retried when the timer expires
                if state.get("session_id") is None:
                    state["session_id"] = str(uuid.uuid4())
                if not _start_session(cfg, cid, state, now):
                    return
            elif session.fingerprint(cfg, cid) != state.get("launch_fingerprint"):
                # Launch settings changed since this session started (or are unknown): restart it
                # with --resume so the conversation continues under the new settings.
                logger.info("claude-code-bridge: launch config changed; restarting session (context kept) on channel %s", cid)
                session.stop(cfg, cid)
                state["started_once"] = True
                if not _start_session(cfg, cid, state, now):
                    return
            if session.is_idle(cfg, cid):
                path = queued[0]
                item = spool.pop(path)
                record_user_input(cfg, cid, item)
                is_debrief = item.get("control") == "debrief"
                spool.set_debrief(cfg, cid, is_debrief)  # before the inject: the turn may send or finish at once
                # Per-turn counts are reset BEFORE the inject, so a fast turn's own sends/blocks are never wiped.
                spool.reset_reply_guard_count(cfg, cid)  # the Stop hook counts nudges per turn
                spool.reset_chat_turn(cfg, cid)  # and the chat-via-hooks sent/block counts
                if session.inject(cfg, cid, inject_text(cfg, cid, item)):
                    state["platform"] = item.get("platform") or state.get("platform")  # where replies go
                    state["inflight"] = item
                    state["inflight_since"] = now
                    state["inflight_redelivered_at"] = None  # a new turn may be redelivered once, too
                    if is_debrief:
                        _route_debrief(cfg, cid, item)
                    state["last_activity"] = now
                else:
                    spool.set_debrief(cfg, cid, False)
                    spool.enqueue(cfg, cid, item, name=path.stem)
                    logger.warning("claude-code-bridge: inject failed on channel %s; message re-queued", cid)

        # 5: idle exit once nothing is queued or in flight.
        elif (
            session.exists(cfg, cid)
            and now - float(state.get("last_activity") or 0) > float(cfg.get("idle_exit_minutes", 60)) * 60
        ):
            session.stop(cfg, cid)
            logger.info("claude-code-bridge: stopped idle session on channel %s", cid)

    # 6: persist.
    spool.save_state(cfg, cid, state)


def tick(cfg: dict[str, Any], post_fn: Callable[[str, Any, str], Any], now: float | None = None) -> None:
    """One worker pass over every channel. Each channel is isolated; errors are logged."""
    current = time.time() if now is None else now
    for cid in spool.channels(cfg):
        try:
            _tick_channel(cfg, cid, post_fn, current)
        except Exception:
            logger.exception("claude-code-bridge: worker error on channel %s", cid)


def start_thread(get_cfg: Callable[[], dict[str, Any]], post_fn: Callable[[str, Any, str], Any]) -> threading.Thread:
    """Start the daemon worker loop once; later calls return the running thread."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        try:
            _unpark_all(get_cfg())
        except Exception:
            logger.exception("claude-code-bridge: releasing parked replies failed")

        def _loop() -> None:
            while True:
                poll = 2.0
                try:
                    cfg = get_cfg()
                    poll = float(cfg.get("poll_seconds", 2))
                    tick(cfg, post_fn)
                except Exception:
                    logger.exception("claude-code-bridge: worker loop error")
                time.sleep(poll)

        _thread = threading.Thread(target=_loop, name="claude-code-bridge-worker", daemon=True)
        _thread.start()
        return _thread
