---
name: Bug report
about: Report a problem with the scaffold
title: '[bug] '
labels: bug
assignees: ''
---

## Describe the bug

A clear and concise description of what the bug is.

## Deployment path

<!-- IMPORTANT for triage -->

- [ ] Native (launchd + venv)
- [ ] Docker

## To reproduce

1.
2.
3.

## Expected behavior

What you expected to happen.

## Logs / output

```
<!-- REDACT secrets, tokens, and any path under your home directory -->
```

## Environment

- OS + version:
- Hermes agent version:
- LLM backend / endpoint (e.g. your backend base URL):
- Docker version (if applicable):

## Checklist

- [ ] I have set a read timeout of **≥120s** on requests to the LLM backend (first request to a cold backend may be slow).
- [ ] The LLM backend is reachable at the configured base URL (`bash scripts/llm/llm-smoke-test.sh` passes).
- [ ] No secrets, tokens, or personal paths are included in this report.
