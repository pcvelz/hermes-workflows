"""session-scope-guard — in gateway chat, session_search may only read the asking user's own sessions."""
import logging

logger = logging.getLogger("session_scope_guard")

_MESSAGE = ("BLOCKED by session-scope-guard: in chat you may not search past sessions or read another "
            "person's session. Answer from this conversation and from the channel's own posts.")


def _env(name):
    from gateway.session_context import get_session_env
    return get_session_env(name)


def _owner(session_id):
    from hermes_state import SessionDB
    # Fresh handle per lookup (the constructor opens a connection); always closed.
    # Not shared: a per-call open is cheap for a single-row lookup and keeps the
    # handle's thread/lock state out of the hook.
    db = SessionDB()
    try:
        row = db.get_session(session_id) or {}
    finally:
        db.close()
    return row.get("user_id") or None


def _block(reason):
    logger.warning("session-scope-guard: blocked session_search (%s)", reason)
    return {"action": "block", "message": _MESSAGE}


def _on_pre_tool_call(tool_name=None, args=None, **kwargs):
    if tool_name != "session_search":
        return None
    try:
        platform = _env("HERMES_SESSION_PLATFORM")
    except Exception:
        platform = ""
    if not platform:
        return None  # CLI / non-gateway use is not restricted
    try:
        args = args or {}
        asker = _env("HERMES_SESSION_USER_ID")
        if args.get("profile"):
            return _block("profile argument")
        sid = args.get("session_id")
        if not sid:
            return _block("discovery or browse shape cannot be limited to the asker")
        owner = _owner(str(sid))
        if not asker or owner != asker:
            return _block("session not owned by the asker")
        return None
    except Exception:
        return _block("lookup failed, failing closed")


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
