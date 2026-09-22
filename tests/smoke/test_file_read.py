#!/usr/bin/env python3
"""Tests for plugins/file-read: reading without writing, against the REAL hermes-agent.

The upstream `file` toolset grants read and write as one thing. `file_read` grants
open, list and search, and nothing else:

  * a profile granted `file_read` can open, list and search a file;
  * it is refused when it asks to write, create, move or delete one -- through the
    tool's own op, and through the upstream write tools, which it does not hold;
  * an op the tool cannot classify is refused (fail closed);
  * a refusal is an error result and the file is unchanged, never a warning;
  * a profile with no file grant reads nothing;
  * the shipped planner profile holds `file_read` and no tool that writes a file.

Exit 77 = hermes-agent not importable (the wrapper reports SKIP).
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "file-read"
PLANNER = REPO / "config" / "profiles" / "planner" / "config.yaml.example"
AGENT_SRC = Path(os.path.expanduser(
    os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent"))).resolve()

_HOME = Path(tempfile.mkdtemp(prefix="file-read-test-")).resolve()
_REAL = Path(os.path.expanduser("~/.hermes")).resolve()
if _HOME == _REAL or _REAL in _HOME.parents:
    sys.exit("refusing: scratch HERMES_HOME resolves inside the real ~/.hermes")
os.environ["HERMES_HOME"] = str(_HOME)

if not (AGENT_SRC / "toolsets.py").is_file():
    print(f"SKIP: hermes-agent source not found at {AGENT_SRC}")
    sys.exit(77)
sys.path.insert(0, str(AGENT_SRC))
try:
    import yaml
    import toolsets
    import model_tools
    from run_agent import AIAgent
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
except Exception as exc:
    print(f"SKIP: cannot import hermes-agent ({exc})")
    sys.exit(77)

_spec = importlib.util.spec_from_file_location("file_read_plugin", PLUGIN_DIR / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)
plugin.register(PluginContext(PluginManifest(name="file-read"), get_plugin_manager()))

# Every upstream tool that can change a file, directly or through a shell.
WRITERS = {"write_file", "patch", "terminal", "execute_code", "process",
           "skill_manage"}


def granted(toolset_names):
    names = set()
    for ts in toolset_names:
        names.update(toolsets.resolve_toolset(ts))
    return names


def agent_for(enabled):
    os.environ["HERMES_HOME"] = str(_HOME)
    return AIAgent(base_url="http://127.0.0.1:9", api_key="x", model="m",
                   enabled_toolsets=list(enabled), quiet_mode=True,
                   skip_context_files=True, skip_memory=True)


def call(name, args, enabled):
    """One tool call the way the agent loop makes it (agent/conversation_loop.py):
    a name outside the agent's valid_tool_names is repaired or refused before
    anything runs; a valid one goes through handle_function_call and its hooks."""
    agent = agent_for(enabled)
    if name not in agent.valid_tool_names:
        name = agent._repair_tool_call(name) or name
    if name not in agent.valid_tool_names:
        return {"error": f"Tool '{name}' does not exist."}
    return json.loads(model_tools.handle_function_call(
        name, args, task_id="file-read-test",
        enabled_tools=sorted(agent.valid_tool_names), enabled_toolsets=list(enabled)))


class FileRead(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=_HOME))
        self.file = self.dir / "survey.md"
        self.file.write_text("line one\nneedle two\n")

    def assertRefused(self, result):
        self.assertIn("error", result, result)
        self.assertFalse(result.get("success", False), result)

    def assertUnchanged(self):
        self.assertEqual(self.file.read_text(), "line one\nneedle two\n")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["survey.md"])

    # --- the grant ---------------------------------------------------------
    def test_toolset_is_the_one_tool(self):
        self.assertEqual(granted(["file_read"]), {"file_read"})

    def test_grant_holds_no_writer(self):
        self.assertFalse(granted(["file_read"]) & WRITERS)
        self.assertEqual(agent_for(["file_read"]).valid_tool_names, {"file_read"})

    # --- reading -----------------------------------------------------------
    def test_open(self):
        r = call("file_read", {"op": "open", "path": str(self.file)}, ["file_read"])
        self.assertNotIn("error", r, r)
        self.assertIn("needle two", json.dumps(r))

    def test_list(self):
        r = call("file_read", {"op": "list", "path": str(self.dir), "pattern": "*.md"},
                 ["file_read"])
        self.assertNotIn("error", r, r)
        self.assertIn("survey.md", json.dumps(r))

    def test_search(self):
        r = call("file_read", {"op": "search", "path": str(self.dir), "pattern": "needle"},
                 ["file_read"])
        self.assertNotIn("error", r, r)
        self.assertIn("needle two", json.dumps(r))

    # --- refusals through the tool's own op --------------------------------
    def test_mutating_ops_refused(self):
        for op in ("write", "create", "move", "rename", "delete", "remove", "patch",
                   "append", "chmod", "mkdir"):
            with self.subTest(op=op):
                r = call("file_read", {"op": op, "path": str(self.file),
                                       "content": "x", "destination": str(self.dir / "y")},
                         ["file_read"])
                self.assertRefused(r)
                self.assertUnchanged()

    def test_unclassified_op_refused(self):
        for op in ("", "OPEN", "open ", None, 3, "read_and_write", "sync"):
            with self.subTest(op=op):
                self.assertRefused(call("file_read", {"op": op, "path": str(self.file)},
                                        ["file_read"]))

    def test_missing_op_refused(self):
        self.assertRefused(call("file_read", {"path": str(self.file)}, ["file_read"]))

    def test_open_ignores_write_arguments(self):
        r = call("file_read", {"op": "open", "path": str(self.file), "content": "gone"},
                 ["file_read"])
        self.assertNotIn("error", r, r)
        self.assertUnchanged()

    # --- refusals through the upstream write tools -------------------------
    def test_upstream_writers_refused(self):
        attempts = [
            ("write_file", {"path": str(self.file), "content": "overwritten"}),
            ("write_file", {"path": str(self.dir / "new.md"), "content": "created"}),
            ("patch", {"mode": "replace", "path": str(self.file),
                       "old_string": "line one", "new_string": "changed"}),
            ("terminal", {"command": f"mv {self.file} {self.dir / 'moved.md'}"}),
            ("terminal", {"command": f"rm {self.file}"}),
        ]
        for name, args in attempts:
            with self.subTest(tool=name, args=args):
                self.assertRefused(call(name, args, ["file_read"]))
                self.assertUnchanged()

    # --- no grant, no reading ----------------------------------------------
    def test_no_grant_reads_nothing(self):
        self.assertFalse(granted(["kanban", "memory"]) & {"file_read", "read_file",
                                                          "search_files"})
        for name, args in (("file_read", {"op": "open", "path": str(self.file)}),
                           ("read_file", {"path": str(self.file)})):
            with self.subTest(tool=name):
                r = call(name, args, ["kanban", "memory"])
                self.assertRefused(r)
                self.assertNotIn("needle two", json.dumps(r))

    # --- what the grant covers: by what a file IS, not only its name ---------
    def _put(self, name, text, mode=0o644, data=None):
        p = self.dir / name
        if data is not None:
            p.write_bytes(data)
        else:
            p.write_text(text)
        p.chmod(mode)
        return p

    def _open(self, p):
        return call("file_read", {"op": "open", "path": str(p)}, ["file_read"])

    def test_planning_formats_open(self):
        for name in ("brief.md", "notes.txt", "NOTES", "LICENSE", "run.json", "c.yaml",
                     "c.yml", "p.toml", "rows.csv", "rows.tsv", "run.log"):
            with self.subTest(name=name):
                r = self._open(self._put(name, "needle here\n"))
                self.assertNotIn("error", r, r)
                self.assertIn("needle here", r["content"])

    def test_named_source_opens(self):
        for name in ("Product.php", "main.go", "lib.rs", "x.c", "view.phtml",
                     "page.twig"):
            with self.subTest(name=name):
                self.assertNotIn("error", self._open(self._put(name, "needle\n")))

    def test_scripts_refused_by_name(self):
        for name in ("deploy.sh", "a.bash", "a.zsh", "tool.py", "x.rb", "x.pl",
                     "app.js", "app.ts", "Makefile", ".bashrc", "DEPLOY.SH"):
            with self.subTest(name=name):
                r = self._open(self._put(name, "echo needle\n"))
                self.assertRefused(r)
                self.assertNotIn("needle", json.dumps(r))

    def test_executable_bit_refused_whatever_the_name(self):
        for name in ("notes.md", "NOTES", "data.json"):
            with self.subTest(name=name):
                self.assertRefused(self._open(self._put(name, "needle\n", mode=0o755)))

    def test_shebang_refused_whatever_the_name(self):
        for name in ("readme.md", "NOTES", "c.yaml"):
            with self.subTest(name=name):
                r = self._open(self._put(name, "#!/bin/sh\nrm -rf needle\n"))
                self.assertRefused(r)
                self.assertNotIn("needle", json.dumps(r))

    def test_unknown_and_binary_refused(self):
        self.assertRefused(self._open(self._put("blob.xyz", "needle\n")))
        self.assertRefused(self._open(self._put("image.md", "", data=b"\x89PNG\x00\x00")))
        self.assertRefused(self._open(self._put("dump.sql", "select 1;\n")))

    def test_symlink_named_md_to_script_refused(self):
        target = self._put("deploy.sh", "echo needle\n")
        link = self.dir / "deploy.md"
        link.symlink_to(target)
        self.assertRefused(self._open(link))

    def test_search_never_reads_a_script(self):
        self._put("deploy.sh", "needle in script\n")
        self._put("run.md", "#!/bin/sh\nneedle in disguise\n")
        r = call("file_read", {"op": "search", "path": str(self.dir), "pattern": "needle"},
                 ["file_read"])
        self.assertNotIn("error", r, r)
        text = json.dumps(r)
        self.assertIn("needle two", text)
        self.assertNotIn("in script", text)
        self.assertNotIn("in disguise", text)

    # --- the planner -------------------------------------------------------
    def test_planner_reads_and_cannot_write(self):
        cfg = yaml.safe_load(PLANNER.read_text())
        sets = cfg["platform_toolsets"]["cli"]
        self.assertIn("file_read", sets)
        self.assertNotIn("file", sets)
        held = granted(sets)
        self.assertIn("file_read", held)
        self.assertFalse(held & WRITERS, held & WRITERS)
        self.assertFalse(held & {"read_file", "search_files"})
        self.assertIn("file-read", cfg["plugins"]["enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
