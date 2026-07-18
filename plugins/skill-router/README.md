# skill-router

Force a matching skill's must-follow guidance to a small local model, deterministically — by
injecting the skill's `router_directive` into the current turn via a `pre_llm_call` hook.

## Why

Hermes surfaces skills to the model **opt-in**: only `<available_skills>` (each skill's name + a
truncated description) goes into the system prompt; the full `SKILL.md` body loads **only** when the
model calls `skill_view(name)`. On a small local model that call reliably does **not** happen, even
on a perfect tag match — so the skill's guidance never reaches the model.

This plugin injects an instruction into the **current turn's user message** — the freshest,
most-followed slot, cache-safe (never touches the cached system-prompt prefix), ephemeral (never
persisted). The injected text is the matched skill's own `router_directive`, typically *"run THIS
one deterministic command and report it"* — which removes the model's freedom to improvise.

## Opt-in per skill

A skill participates **only** if its frontmatter declares `metadata.hermes.router_directive`:

```yaml
metadata:
  hermes:
    tags: [gitlab, mr-watcher, mr-stream, ...]
    router_directive: "Before anything else, run `bash /…/status.sh` and report its VERDICT verbatim. Do NOT free-hand the diagnosis."
    # optional: router_triggers: ["mr watcher", "merge request notifications"]
```

Match phrases come from `router_triggers` if given, else the skill **name** + its hyphenated/
multi-word **tags** (distinctive only — generic single words like `web`/`devops` never match, to
avoid false positives). Direct action/coding messages (`fix …`, `write …`) are never routed.

## Which skill directories are scanned

Set via the `SKILL_ROUTER_DIRS` env var — an `os.pathsep`-separated list of absolute skill
directories (`~` expanded). If unset, it defaults to `<HERMES_HOME>/skills` (with `HERMES_HOME`
itself defaulting to `~/.hermes`). Point it at whatever `skills.external_dirs` your profile uses,
e.g.:

```sh
export SKILL_ROUTER_DIRS="$HOME/Documents/Code/<your-project>/skills"
```

## Continuation matching (topicless follow-ups)

The classifier is keyword-only and stateless, so a continuation like *"test again? where are we
now?"* matches **nothing** on its own. The fix: when the **current** message matches no skill, the
hook scans the last ~6 messages of the **conversation history** Hermes hands it each turn
(`conversation_history` kwarg — a `list(messages)` with the current user message already appended).
If a recent message *did* match a skill **and** the current message reads like a follow-up — short
(≤4 words) or carrying a continuation cue (`again`, `test`, `check`, `status`, `still`, `now`, …) —
that skill's directive is re-injected, tagged `[skill-router/continuation]`.

This is **stateless across turns by construction** (each turn may run as a fresh process; the *only*
cross-turn memory is the reloaded `conversation_history`). The action-verb guard still applies — a
fresh, unrelated `fix …` / `write …` request mid-session is **never** hijacked into a continuation.

## Files

| File | Role |
|---|---|
| `plugin.yaml` | manifest — registers the `pre_llm_call` hook |
| `__init__.py` | thin wiring: `register(ctx)` + the hook callback |
| `lib/skill_match.py` | matcher + directive builder (single source of truth; self-test below) |

## Verify

```sh
python3 plugins/skill-router/lib/skill_match.py   # self-test; prints LIVE opt-in skills loaded
hermes -p <profile> plugins list | grep skill-router   # expect: enabled · user
```
