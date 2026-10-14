"""reply-language — tell the model which language to answer in, by the channel's team.

``pre_llm_call`` hook. The map team -> language comes from the profile config
(``reply_language: {teams: {<team>: <Language>}}``); the channel's team is resolved once per channel
through the Mattermost REST API using the gateway process's own connection settings and cached.
Fail-open: any error returns None (no injection).
"""
import json
import logging
import os
import urllib.request

logger = logging.getLogger("reply_language")
_TEAM_CACHE = {}

_TEXT = {
    "English": "[reply-language] Reply in English, whatever language the user's message or the sources are in. "
               "Translate quoted source material (for example Dutch websites) into English.",
    "Spanish": "[reply-language] Responde en español, sea cual sea el idioma del mensaje del usuario o de las "
               "fuentes. Traduce el material citado (por ejemplo páginas en neerlandés) al español.",
}


def _teams():
    import yaml
    from hermes_constants import get_hermes_home
    cfg = yaml.safe_load((get_hermes_home() / "config.yaml").read_text(encoding="utf-8")) or {}
    return (cfg.get("reply_language") or {}).get("teams") or {}


def _chat_id():
    from gateway.session_context import get_session_env
    return get_session_env("HERMES_SESSION_CHAT_ID")


def _get(path):
    base = os.environ["MATTERMOST_URL"].rstrip("/") + "/api/v4"
    req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + os.environ["MATTERMOST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _team_of_channel(chat_id):
    channel = _get("/channels/" + chat_id)
    return _get("/teams/" + channel["team_id"]).get("name", "")


def _on_pre_llm_call(**kwargs):
    try:
        if str(kwargs.get("platform") or "").lower() != "mattermost":
            return None
        chat = _chat_id()
        if not chat:
            return None
        if chat not in _TEAM_CACHE:
            _TEAM_CACHE[chat] = _team_of_channel(chat)
        language = _teams().get(_TEAM_CACHE[chat])
        text = _TEXT.get(str(language))
        return {"context": text} if text else None
    except Exception:
        logger.warning("reply-language: lookup failed; no language injected", exc_info=True)
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
