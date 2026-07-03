# Hermes Learnings (Meta)

The workflow's **reflections about itself** — lessons learned, postmortems, recurring failure modes and their fixes, prompt and skill insights, and 'what went wrong and what we changed'.

This is the institutional memory of the autonomous system's own evolution. It is where the workflow gets smarter over time, rather than relearning the same lessons from scratch.

---

## What belongs here

- **Incident postmortems** structured as: timeline → root cause → fix → prevention
- **"Lesson" notes** distilled from repeated mistakes — when the same failure mode appears twice, write it down once here
- **Prompt and skill insights** — observations about what instruction patterns work, what causes the agent to misfire, what phrasings produce reliable behavior
- **Config and tuning notes** — "we changed X setting because Y was happening; here's the before/after"
- **Notes on what to promote** — when a learning hardens into a rule the agent must follow every turn, note it here before distilling it into MEMORY.md

---

## Relationship to MEMORY.md

This directory and MEMORY.md are in a deliberate relationship:

1. A failure or insight surfaces — write a full note here with context, timeline, and explanation.
2. If the learning distills into a **rule the agent must obey on most turns**, write **one concise line** into the relevant profile's `MEMORY.md` and link back here for the full story.
3. Respect the ~2200-char MEMORY.md cap — every line added there must displace something less important.

Meta/hermes-learnings/ holds the full story. MEMORY.md holds only the actionable conclusion.

---

## Suggested conventions

- **Filename:** `YYYY-MM-DD-short-title.md` — the date makes the evolution of the workflow's self-knowledge visible
- **Tag with subsystem** — e.g., `#hooks`, `#dispatch`, `#memory`, `#cost` in the note body for filtering
- **Keep one learning per file** — easier to recall, easier to link from MEMORY.md

---

This is where the workflow gets smarter over time — write it down or relearn it.
