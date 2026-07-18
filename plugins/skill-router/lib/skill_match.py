"""
skill_match.py — match a user message to an opt-in skill and build the
just-in-time instruction the ``skill-router`` plugin injects for it.

Why this exists
---------------
Hermes surfaces skills to the model *opt-in*: only ``<available_skills>`` (each
skill's name + a truncated description) goes into the system prompt; the full
SKILL.md body loads ONLY when the model itself calls ``skill_view(name)``. On a
small local model that call reliably does NOT happen, even on a perfect tag match,
so the skill's must-follow guidance never reaches the model.

Fix: a ``pre_llm_call`` hook that injects an instruction into the CURRENT turn's
user message (freshest, most-followed slot, cache-safe). Here the injected text is
the matched skill's own ``router_directive`` — typically "run THIS one deterministic
command and report it", which removes the model's freedom to improvise the check.

Opt-in by design
----------------
A skill participates ONLY if its frontmatter declares
``metadata.hermes.router_directive``. Matching phrases come from
``metadata.hermes.router_triggers`` if given, else the skill's name + its
hyphenated/multi-word tags (distinctive ones only — generic single words like
"web"/"devops" never match, to avoid false positives). Skills with no
``router_directive`` are ignored here.

Skill directories scanned
--------------------------
Set via the ``SKILL_ROUTER_DIRS`` env var (``os.pathsep``-separated absolute
paths, ``~`` expanded). If unset, defaults to ``<HERMES_HOME>/skills`` (with
``HERMES_HOME`` itself defaulting to ``~/.hermes``). Point this at whatever
``skills.external_dirs`` your profile uses.

Run ``python3 skill_match.py`` for a self-test.
"""

import os
import re

try:
    import yaml
except Exception:  # pragma: no cover - yaml absent ⇒ plugin no-ops safely
    yaml = None


def _default_skill_dirs():
    """Skill dirs to scan: ``SKILL_ROUTER_DIRS`` (os.pathsep-separated) if set,
    else ``<HERMES_HOME>/skills`` (HERMES_HOME defaults to ~/.hermes)."""
    env = os.environ.get("SKILL_ROUTER_DIRS", "").strip()
    if env:
        return [os.path.expanduser(p) for p in env.split(os.pathsep) if p.strip()]
    home = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser("~/.hermes")
    return [os.path.join(home, "skills")]


# External skill dirs scanned (see _default_skill_dirs for how this is resolved).
SKILL_DIRS = _default_skill_dirs()

# A direct do-this/coding request — never route.
_ACTION_PREFIXES = (
    "fix", "write", "refactor", "debug", "implement", "add", "remove", "delete",
    "create", "run", "execute", "build", "compile", "deploy", "commit",
    "rename", "move", "edit", "patch", "install", "configure", "set up", "make",
)

# Continuation cues — phrases/words that signal the CURRENT message is a
# topicless follow-up on whatever skill the conversation was already about
# (rather than a fresh, unrelated request). Used only by the history fallback:
# when the current message matches no skill directly but a recent message DID,
# one of these cues lets us re-inject that skill's directive. Lowercased,
# matched as substrings (with word-ish boundaries via the cleaned text).
_CONTINUATION_CUES = (
    "again", "test", "retest", "re test", "check", "recheck", "re check",
    "rerun", "re run", "run it", "run again", "where are we", "where we are",
    "where we at", "where are we at", "status", "still", "anything else",
    "any thing else", "anything more", "is it fixed", "is it working",
    "is this working", "did it work", "any change", "any changes", "now",
    "currently", "at the moment", "look again", "see anything", "have a look",
    "take a look",
)

# How many recent messages to scan for a prior skill match.
_HISTORY_SCAN = 6


def _msg_text(msg):
    """Extract plain text from a history message (content may be str or a
    multimodal list of parts). Never raises."""
    try:
        content = msg.get("content")
    except Exception:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text") or p.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(p, str):
                parts.append(p)
        return " ".join(parts)
    return ""


def _looks_like_continuation(text):
    """True if the message reads as a topicless follow-up (short, or carries a
    continuation cue) rather than a fresh request."""
    t = text.strip().lower()
    if not t:
        return False
    low = " " + re.sub(r"[^a-z0-9 ]+", " ", t) + " "
    low = re.sub(r"\s+", " ", low)
    for cue in _CONTINUATION_CUES:
        if (" " + cue + " ") in low:
            return True
    # Very short messages with no fresh-request content also count.
    if len(t.split()) <= 4:
        return True
    return False


def _parse_frontmatter(path):
    try:
        txt = open(path, encoding="utf-8").read()
    except Exception:
        return None
    if not txt.startswith("---") or yaml is None:
        return None
    parts = txt.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return None


def _phrases_for(name, tags, triggers):
    """Distinctive match phrases for a skill (lowercased, hyphens→spaces)."""
    out = set()
    out.add(name.replace("-", " ").lower())
    if triggers:
        for t in triggers:
            out.add(str(t).replace("-", " ").lower())
    else:
        for t in (tags or []):
            t = str(t)
            if "-" in t or " " in t:  # only multi-word/hyphenated tags are distinctive
                out.add(t.replace("-", " ").lower())
    return [p for p in out if len(p) >= 4]


def load_skills(dirs=None):
    """Return [{name, phrases, directive}] for skills that OPT IN (have router_directive)."""
    dirs = SKILL_DIRS if dirs is None else dirs
    skills = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for entry in sorted(os.listdir(d)):
            sk = os.path.join(d, entry, "SKILL.md")
            if not os.path.isfile(sk):
                continue
            fm = _parse_frontmatter(sk)
            if not fm:
                continue
            meta = (fm.get("metadata") or {}).get("hermes") or {}
            directive = meta.get("router_directive")
            if not directive:
                continue  # opt-in only
            name = fm.get("name") or entry
            phrases = _phrases_for(name, meta.get("tags"), meta.get("router_triggers"))
            if phrases:
                skills.append({"name": name, "phrases": phrases, "directive": directive})
    return skills


# Loaded once at import (restart picks up skill changes — fine for a steering hook).
_SKILLS = load_skills()


def _looks_like_action(text):
    t = text.strip().lower()
    for p in _ACTION_PREFIXES:
        if t == p or t.startswith(p + " "):
            return True
    return False


def match(text, skills=None):
    """Return the matched opt-in skill dict, or None."""
    if not text or not text.strip():
        return None
    if _looks_like_action(text):
        return None
    skills = _SKILLS if skills is None else skills
    low = " " + re.sub(r"[^a-z0-9 ]+", " ", text.lower()) + " "
    low = re.sub(r"\s+", " ", low)
    for sk in skills:
        for p in sk["phrases"]:
            if (" " + p + " ") in low:
                return sk
    return None


def match_with_history(text, history=None, skills=None):
    """Match the current message to a skill, falling back to conversation history.

    Returns ``(skill_or_None, via)`` where ``via`` is ``"direct"`` (the current
    message itself matched), ``"continuation"`` (the current message matched
    nothing but reads as a topicless follow-up on a skill a RECENT message was
    about), or ``None`` (no match).

    The fresh-process-per-turn constraint means we cannot keep sticky state in a
    module dict — so the *only* memory we use is ``history``, the conversation
    Hermes reloads and hands the hook each turn (``conversation_history``).

    The action-verb guard still applies: a fresh ``fix …`` / ``write …`` request
    is never hijacked into a continuation, even mid-skill-session.
    """
    skills = _SKILLS if skills is None else skills

    direct = match(text, skills)
    if direct:
        return direct, "direct"

    # Fallback only for genuine follow-ups: not a fresh action request, and it
    # has to *read* like a continuation (short, or carries a continuation cue).
    if not text or not text.strip():
        return None, None
    if _looks_like_action(text):
        return None, None
    if not _looks_like_continuation(text):
        return None, None
    if not history:
        return None, None

    # Scan the most recent messages (newest first) for the last skill any of
    # them was about. The current turn's user message is the last element of
    # `messages` (Hermes appends it before invoking the hook), so skip a
    # trailing message whose text equals the current one to avoid re-matching
    # it (it already failed `match` above, but be explicit).
    cur = text.strip()
    recent = [m for m in history if isinstance(m, dict)][-_HISTORY_SCAN:]
    for msg in reversed(recent):
        mt = _msg_text(msg)
        if not mt or mt.strip() == cur:
            continue
        sk = match(mt, skills)
        if sk:
            return sk, "continuation"
    return None, None


def build_steer(sk, via="direct"):
    """The instruction injected (ephemerally) into the current turn's user message.

    ``via="continuation"`` annotates that the directive was re-applied because
    the turn is a topicless follow-up on an earlier skill, not a direct match —
    so a log scan can tell the two paths apart.
    """
    tag = "skill-router" if via != "continuation" else "skill-router/continuation"
    return f"[{tag}] A skill (`{sk['name']}`) covers this request. {sk['directive']}"


# --------------------------------------------------------------------------- #
# Self-test: ``python3 skill_match.py`` — uses a synthetic skill set so it does
# not depend on the live SKILL.md files, then also reports what the LIVE set matches.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    FAKE = [
        {"name": "gitlab-mr-watcher",
         "phrases": _phrases_for("gitlab-mr-watcher",
                                 ["gitlab", "mr-watcher", "mr-stream", "merge-request", "glab", "watcher"],
                                 None),
         "directive": "Run `status.sh` and report its VERDICT."},
    ]

    # ----- direct (stateless) classification — existing regression set ----- #
    SAMPLES = [
        ("Is the GitLab MR watcher actually working right now?", "gitlab-mr-watcher"),
        ("did the mr-stream watcher post anything?", "gitlab-mr-watcher"),
        ("why are there no merge request notifications?", "gitlab-mr-watcher"),
        ("fix the bug in mr_poll.py", None),
        ("write a python script that sorts a list", None),
        ("what's the weather like?", None),
        ("", None),
    ]
    ok = 0
    for text, expected in SAMPLES:
        got = match(text, FAKE)
        gotname = got["name"] if got else None
        flag = "ok " if gotname == expected else "FAIL"
        if gotname == expected:
            ok += 1
        print(f"  [{flag}] {gotname!s:>18}  (want {expected!s:>18})  {text[:48]!r}")
    print(f"\n{ok}/{len(SAMPLES)} direct classifications correct")

    # ----- continuation (history-aware) classification ----- #
    # A prior message about the MR watcher establishes the topic; a topicless
    # follow-up must re-inject the directive via the history fallback.
    HIST_MR = [
        {"role": "user", "content": "Is the GitLab MR watcher actually working right now?"},
        {"role": "assistant", "content": "I ran status.sh — verdict HEALTHY."},
    ]
    HIST_NONE = [
        {"role": "user", "content": "what's the weather like in Amsterdam?"},
        {"role": "assistant", "content": "It's mild and cloudy."},
    ]
    FAILING = ("Is there anything else you can see? Can you test again? I believe there "
               "has been some changes if it would be good to check where we are at now?")
    # (current_message, history, expected_name, expected_via)
    CONT = [
        # The EXACT failing prompt, as a continuation of an MR-watcher session.
        (FAILING, HIST_MR, "gitlab-mr-watcher", "continuation"),
        # Short follow-ups in an MR session → continuation re-inject.
        ("test again?", HIST_MR, "gitlab-mr-watcher", "continuation"),
        ("where are we now?", HIST_MR, "gitlab-mr-watcher", "continuation"),
        ("anything else?", HIST_MR, "gitlab-mr-watcher", "continuation"),
        # Direct match still wins and reports via="direct".
        ("did the mr-stream watcher post anything?", HIST_MR, "gitlab-mr-watcher", "direct"),
        # CONTROL: topicless follow-up with NO relevant history → stay None.
        (FAILING, HIST_NONE, None, None),
        ("test again?", HIST_NONE, None, None),
        # CONTROL: a fresh, unrelated coding request mid-MR-session must NOT be
        # hijacked into the MR-watcher continuation.
        ("write a python script that sorts a list", HIST_MR, None, None),
        ("fix the bug in mr_poll.py", HIST_MR, None, None),
        # CONTROL: a non-continuation, non-action fresh question mid-MR-session
        # (no cue, long) must NOT hijack either.
        ("Tell me about the history of the Roman empire in detail please thanks",
         HIST_MR, None, None),
    ]
    ok2 = 0
    for text, hist, exp_name, exp_via in CONT:
        sk, via = match_with_history(text, hist, FAKE)
        gotname = sk["name"] if sk else None
        good = (gotname == exp_name and via == exp_via)
        flag = "ok " if good else "FAIL"
        if good:
            ok2 += 1
        print(f"  [{flag}] {gotname!s:>18}/{via!s:<12} "
              f"(want {exp_name!s:>18}/{exp_via!s:<12})  {text[:42]!r}")
    print(f"\n{ok2}/{len(CONT)} continuation classifications correct")

    print(f"\nLIVE opt-in skills loaded: {[s['name'] for s in _SKILLS]}")
    for s in _SKILLS:
        print(f"  {s['name']}: phrases={s['phrases']}")
    raise SystemExit(0 if (ok == len(SAMPLES) and ok2 == len(CONT)) else 1)
