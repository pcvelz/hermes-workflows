# Browsing playbook (for agents driving a real website)

Short rules for an agent that browses a third-party site through the browser tools.
Paste the "Rules" block into a channel soul or a subagent prompt.

## Rules

1. **Walk it like a person.** Start at the site's homepage. Take a snapshot, then find the next element by its visible text or role (link "Inloggen", button "Zoeken"). Do not open a URL you guessed. Deep links are fine only after you have seen them on the site.
2. **Hybrid: helpers where they exist, a person's clicks where they don't.** If you were given a helper command for a step (login, running a search), use it. Otherwise click with the click tool and type with the type tool. Don't improvise your own scripts or request loops against the site: blind mechanical traffic is how an IP gets blocked.
3. **Look properly once instead of reloading.** On each page, read the full snapshot of the part you need (header, form) before deciding. Match elements by the text you see. Never reload a page just to look again.
4. **Check after every action.** Take a snapshot. Did the page change the way you expected? If not, do not repeat the action. Go to "When it goes wrong".
5. **One thing at a time.** One tab and one action at a time, with at least 5 seconds between page loads.
6. **Stay inside the budget** you were given: page loads per task, and login attempts (at most one per 30 minutes). When the budget runs out, stop and report.
7. **Snapshots, not screenshots.** Read pages through the accessibility snapshot. No full-page screenshots, no map views.

## When it goes wrong

| You see | Do |
|---|---|
| Error page, "403", "interne fout", "access denied", captcha, "too many requests" | Stop at once. Do not retry and do not try a variant. Report. |
| The element you need is not in the snapshot | Wait 3 s, take one more snapshot, and scroll once. Still missing? Stop and report the menu items and buttons you DO see. |
| A lookup matches 2+ elements (e.g. a login and a register form both have "Password") | On the same page, narrow it to the form or section that holds the other fields you need, and re-check. Find every element before acting. Stop only if it is still ambiguous. |
| An element exists but is "outside the viewport" or hidden | It sits in a closed menu, dropdown or modal. Click the button that opens it (often the header button with the same label), then check again. |
| A click did nothing | Do not click again. Take a snapshot, then report what is on the page. |
| A login did not work | Do not log in again. Report the visible message near the form. |
| Several look-alike entries ("Login" vs "Login hotels/partners/business") | Take the one for ordinary customers. Never submit on a partner or business login. If unclear, stop and report the options. |
| A submit button has a different label than expected ("Aanmelden" instead of "Login") | Do not click. Check that you are on the right page or form first; when in doubt, stop and report. |
| You are unsure what the next step is | Stop and report. Guessing on someone else's server is worse than asking. |

**Writing a script against the site?** Run it first on a local stub page (`page.set_content(...)`) that mimics the elements you expect. A script bug must never cost a page load on the real site.

**Report** = the last URL, the visible error or message text, and your last 3 actions (tool and target). Never include passwords or other secrets.

## For the builder: harnessing a weaker model

The rules above are for the agent that browses. This section is for whoever sets that agent up: usually a stronger model or a person, with a cheaper or local model doing the browsing.

- **You own its failures.** When it gets stuck, read its tool calls and ask it exactly what it tried. Don't just reword the prompt and hope.
- **Walk the path yourself first.** Go through the site once, like a person, with a small fixed budget of page loads. Write down the exact labels, the surprises (look-alike buttons, hidden duplicates, autocomplete that swaps your text, search state that never reaches the URL), and the network calls behind each step.
- **Turn the surprises into failure steps.** Add short "you see X → do Y" lines to its playbook. Spell out the common sense it lacks.
- **Two strikes on the same step → script it.** Record the requests the UI makes for that step (form posts, XHR, cookies) and reverse engineer them. Then give the agent a small helper command: rate-limited, allow-listed, one compact output. Test the helper on a local stub page before it touches the real site.
- **Go hybrid.** Scripts do the fiddly, deterministic parts (login, setting up a search, paging). The model does the reading, judging and replying.
- **Never let the model do arithmetic or formatting it can get wrong.** The helper returns the final values ready to paste (price per night with surcharges, a meal label, a total). The prompt says "copy these fields", not "calculate".
- **Rules the model must apply are countable at a glance.** "More than 5 days → push back" works. "More than 45 combinations of arrival × length × region" does not.
- **Look for earlier work first.** Old scripts and notes on the same site often hold the form fields and quirks already. Read them before you map anything live.
- **Every live page you learn from becomes a test fixture.** Save it, trim it, and test the parser against it. Stubs you wrote yourself only prove the code matches your guess. Also exercise the cold path live at least once (no stored session, a different month, accented names, unpadded dates).
- **Test a hypothesis against its degenerate case.** "The region's hotels come first" can be true with a block of one. Check what the data actually shows before discarding an idea.
- **Enforce limits mechanically.** Pacing, daily caps and forbidden URLs go in a hook (see `browser_guard` in `claude-code-bridge.md`), not only in prose. Never let it brute force a third-party server.
- **Test the real chat path.** Your test wait ends on the first reply and reads it. A wait that runs until a marker you made up hides failures that are already in chat.
