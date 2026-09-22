# @user-gated
"""A wait is visible on the card, and a wait is never a failure.

Contract: docs/harness-contract.md, "A wait is visible" (T45). Only the user
changes this file, in their own session. No skip, no xfail.

Runs the GENERATED agent patches (scripts/harness-patches/build.py, built from
the installed checkout's own upstream) against the real kanban_db in a scratch
HOME, one subprocess per case so module state never leaks between cases.
The endpoint below is a string the tests parse; nothing ever connects to it.

T45a  a timer tick with nothing received leaves the card's heartbeat alone and
      extends the claim; a chunk refreshes the heartbeat
T45b  a waiting tick writes a note naming endpoint, model, attempt, duration
T45c  one wait is one note, updated in place -- never one per tick
T45d  the first bytes close the note; a later wait is a new note
T45e  a silent request ends at the silence bound (config, default 300s, local
      endpoints included), the reason is on the card, and the card's failure
      count, status and runs are untouched
T45h  the generator pins chat_completion_helpers.py and is deterministic
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENT_SRC = Path(os.path.expanduser(
    os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent"))).resolve()
PATCH_BUILD = REPO / "scripts" / "harness-patches" / "build.py"
_SCRATCH = Path(tempfile.mkdtemp(prefix="visible-wait-")).resolve()
_REAL = Path(os.path.expanduser("~/.hermes")).resolve()
if _REAL in _SCRATCH.parents or _SCRATCH == _REAL:
    sys.exit("refusing: scratch resolves inside the real ~/.hermes")

HELPERS = "agent/chat_completion_helpers.py"


def _load_build():
    spec = importlib.util.spec_from_file_location("visible_wait_build", PATCH_BUILD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _upstream(dest, rels):
    for rel in rels:
        out = subprocess.run(["git", "-C", str(AGENT_SRC), "show", f"HEAD:{rel}"],
                             capture_output=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(f"cannot read upstream {rel}: {out.stderr}")
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(out.stdout)
    return dest


_TREE = {}


def _patched_tree():
    """The installed agent with every generated patch swapped in, as links."""
    if "tree" in _TREE:
        return _TREE["tree"]
    mod = _load_build()
    built = mod.build(_upstream(_SCRATCH / "upstream", mod.PINNED), _SCRATCH / "built")
    tree = _SCRATCH / "agent"
    tree.mkdir()
    swapped = set(mod.PINNED)
    dirs = {rel.split("/", 1)[0] for rel in swapped}
    for entry in AGENT_SRC.iterdir():
        if entry.name in dirs:
            (tree / entry.name).mkdir()
            for f in entry.iterdir():
                rel = f"{entry.name}/{f.name}"
                (tree / rel).symlink_to(built / rel if rel in swapped else f)
        else:
            (tree / entry.name).symlink_to(entry)
    _TREE["tree"], _TREE["built"] = tree, built
    return tree


#: A running card owned by this worker, heartbeat an hour old so any refresh
#: shows, claim about to expire so any extension shows.
_PRELUDE = r'''
import json, os, sys, time
from hermes_cli import kanban_db as kb
conn = kb.connect()
tid = kb.create_task(conn, title="widget A", assignee="coder")
T0 = int(time.time())
with conn:
    conn.execute("UPDATE tasks SET status='running', claim_lock='lock-1', "
                 "claim_expires=?, last_heartbeat_at=?, worker_pid=? WHERE id=?",
                 (T0 + 5, T0 - 3600, os.getpid(), tid))
os.environ.update(HERMES_KANBAN_TASK=tid, HERMES_KANBAN_CLAIM_LOCK="lock-1")
from tools import kanban_tools as kt
def card():
    c = kb.connect()
    r = c.execute("SELECT status, last_heartbeat_at, claim_expires, "
                  "consecutive_failures, title FROM tasks WHERE id=?", (tid,)).fetchone()
    notes = [x[0] for x in c.execute(
        "SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (tid,))]
    runs = c.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=? AND outcome "
                     "IS NOT NULL", (tid,)).fetchone()[0]
    c.close()
    return {"status": r[0], "hb": r[1], "claim": r[2], "failures": r[3],
            "title": r[4], "notes": notes, "ended_runs": runs, "T0": T0}
EP, MODEL = "127.0.0.1:4242", "qwen3.6-35b"
'''


def _run(body, **env):
    home = _SCRATCH / f"h-{time.time_ns()}"
    (home / ".hermes" / "logs").mkdir(parents=True)
    full = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "KANBAN_"))}
    full.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"),
                PYTHONPATH=str(_patched_tree()))
    full.update({k: str(v) for k, v in env.items()})
    out = subprocess.run([sys.executable, "-c", _PRELUDE + body], capture_output=True,
                         text=True, timeout=180, env=full, cwd=str(home))
    if out.returncode != 0:
        raise AssertionError(f"subprocess failed:\n{out.stderr[-3000:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestVisibleWait(unittest.TestCase):

    def test_T45a_a_tick_without_bytes_extends_the_claim_not_the_heartbeat(self):
        r = _run(r'''
kt.visible_wait_begin(EP, MODEL, 30)
kt._auto_heartbeat_last_attempt = 0.0
kt.heartbeat_current_worker_from_env()   # what _touch_activity bridges to
kt.visible_wait_tick(45)
print(json.dumps(card()))
''')
        self.assertEqual(r["hb"], r["T0"] - 3600, "a wait refreshed the card's heartbeat")
        self.assertGreater(r["claim"], r["T0"] + 60, "a wait did not extend the claim")

    def test_T45a_a_chunk_refreshes_the_heartbeat(self):
        r = _run(r'''
kt.visible_wait_begin(EP, MODEL, 30)
kt.visible_wait_tick(45)
kt.visible_wait_progress()
kt.heartbeat_current_worker_from_env()
print(json.dumps(card()))
''')
        self.assertGreaterEqual(r["hb"], r["T0"], "bytes arrived and the heartbeat stayed old")

    def test_T45b_the_note_names_endpoint_model_attempt_and_duration(self):
        r = _run(r'''
for _ in range(9):
    kt.visible_wait_begin(EP, MODEL, 30)
kt.visible_wait_tick(14 * 60)
print(json.dumps(card()))
''')
        self.assertEqual(len(r["notes"]), 1, r["notes"])
        note = r["notes"][0]
        for part in ("127.0.0.1:4242", "qwen3.6-35b", "attempt 9 of 30", "14m"):
            self.assertIn(part, note)
        first = note.splitlines()[0]
        self.assertFalse(first.startswith("["), "the note must open with a sentence, "
                         "not a marker only a machine can read")
        self.assertIn("not a failure", first)
        self.assertEqual(r["title"], "widget A", "the title changed")

    def test_T45b_a_short_wait_writes_nothing(self):
        r = _run(r'''
kt.visible_wait_begin(EP, MODEL, 30)
kt.visible_wait_tick(5)
kt.visible_wait_progress()
print(json.dumps(card()))
''')
        self.assertEqual(r["notes"], [])

    def test_T45c_one_wait_is_one_note_updated_in_place(self):
        r = _run(r'''
kt.visible_wait_begin(EP, MODEL, 30)
for i in range(1, 33):
    if i % 4 == 0:
        kt.visible_wait_begin(EP, MODEL, 30)   # the retry loop's next attempt
    kt.visible_wait_tick(30 * i)
print(json.dumps(card()))
''')
        self.assertEqual(len(r["notes"]), 1, f"{len(r['notes'])} notes for one wait")
        self.assertIn("16m", r["notes"][0])
        self.assertIn("attempt 9 of 30", r["notes"][0])

    def test_T45d_bytes_close_the_note_and_a_new_wait_is_a_new_note(self):
        r = _run(r'''
kt.visible_wait_begin(EP, MODEL, 30)
kt.visible_wait_tick(120)
kt.visible_wait_progress()
kt.visible_wait_begin(EP, MODEL, 30)
kt.visible_wait_tick(60)
print(json.dumps(card()))
''')
        self.assertEqual(len(r["notes"]), 2, r["notes"])
        self.assertIn("visible-wait closed", r["notes"][0])
        self.assertIn("answered", r["notes"][0])
        self.assertIn("visible-wait open", r["notes"][1])

    def test_T45e_silence_past_the_bound_is_recorded_and_costs_nothing(self):
        r = _run(r'''
kt.visible_wait_begin(EP, MODEL, 30)
kt.visible_wait_tick(301)
kt.visible_wait_silence_ended(301, 300)
kt.visible_wait_begin(EP, MODEL, 30)
print(json.dumps(card()))
''')
        self.assertEqual(len(r["notes"]), 1, r["notes"])
        self.assertIn("ended one request after 5m", r["notes"][0])
        self.assertIn("stream_silence_limit_s=300", r["notes"][0])
        self.assertEqual((r["status"], r["failures"], r["ended_runs"]), ("running", 0, 0),
                         "a wait changed the card's status, failure count or runs")

    def test_T45e_the_bound_comes_from_config_and_is_never_unbounded(self):
        r = _run(r'''
from agent import chat_completion_helpers as h
out = {"default": h._visible_wait_silence_limit(),
       "local_upstream_inf": h._visible_wait_stale_bound(float("inf")),
       "remote_lower_kept": h._visible_wait_stale_bound(180.0)}
os.environ["HERMES_STREAM_SILENCE_LIMIT_S"] = "120"
out["env"] = h._visible_wait_silence_limit()
for bad in ("0", "-5", "inf", "nan", "soon"):
    os.environ["HERMES_STREAM_SILENCE_LIMIT_S"] = bad
    out["bad:" + bad] = h._visible_wait_silence_limit()
del os.environ["HERMES_STREAM_SILENCE_LIMIT_S"]
open(os.path.join(os.environ["HERMES_HOME"], "config.yaml"), "w").write(
    "agent:\n  stream_silence_limit_s: 600\n")
out["config"] = h._visible_wait_silence_limit()
print(json.dumps(out))
''')
        self.assertEqual(r["default"], 300.0)
        self.assertEqual(r["local_upstream_inf"], 300.0, "a local endpoint is still unbounded")
        self.assertEqual(r["remote_lower_kept"], 180.0)
        self.assertEqual(r["env"], 120.0)
        self.assertEqual(r["config"], 600.0)
        for bad in ("0", "-5", "inf", "nan", "soon"):
            self.assertEqual(r["bad:" + bad], 300.0, f"{bad!r} unbounded the silence")

    def test_T45_the_wait_loop_is_wired_to_the_card(self):
        """Every site upstream's streaming loop waits or receives at: the request
        start opens the wait, the 30s tick records it, the stale kill ends it,
        a chunk closes it."""
        _patched_tree()
        text = (_TREE["built"] / HELPERS).read_text()
        for call in ('_visible_wait("begin"', '_visible_wait("tick"',
                     '_visible_wait("silence_ended"', '_visible_wait("progress"',
                     "_visible_wait_stale_bound("):
            self.assertIn(call, text)
        self.assertNotIn('_stream_stale_timeout = float("inf")', text,
                         "a local endpoint's request is still held forever")

    def test_T45h_the_generator_pins_the_helpers_and_is_deterministic(self):
        mod = _load_build()
        self.assertIn(HELPERS, mod.PINNED)
        up = _upstream(_SCRATCH / "up-drift", mod.PINNED)
        a, b = mod.build(up, _SCRATCH / "a"), mod.build(up, _SCRATCH / "b")
        self.assertEqual((a / HELPERS).read_bytes(), (b / HELPERS).read_bytes())
        path = up / HELPERS
        path.write_bytes(path.read_bytes() + b"\n# newer upstream\n")
        with self.assertRaises(mod.UpstreamMismatch) as caught:
            mod.build(up, _SCRATCH / "never")
        self.assertIn(hashlib.sha256(path.read_bytes()).hexdigest(), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
