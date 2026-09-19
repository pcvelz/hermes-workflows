# kanban-harness

Enforces the kanban board's transition matrix (coding → QA → human architect → done) for
agent processes. It uses a `pre_tool_call` hook plus a `kanban_handoff` worker tool.
Fail-closed for kanban mutations.

- Config: `harness.yaml` (git-ignored; template `harness.yaml.example`), or `KANBAN_HARNESS_FILE`.
- Self-test (decision logic, no agent needed): `python3 plugins/kanban-harness/__init__.py`
- End-to-end test: `bash tests/smoke/kanban-harness.sh`

Full description, the matrix, configuration and limits: [docs/kanban-harness.md](../../docs/kanban-harness.md).
