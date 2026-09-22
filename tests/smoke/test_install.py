#!/usr/bin/env python3
"""Tests for scripts/install.py: one command from a clean checkout to a running loop.

The installer is driven against a scratch HERMES_HOME, a scratch LaunchAgents
directory and a fake `launchctl` that keeps its loaded jobs in a state dir, so
nothing here touches the real machine. What it proves:

  * one run registers the gateway (the motor) and the escalator, and names each;
  * optional jobs (backup, bridge) are skipped by name, with the reason;
  * a second run changes nothing and still reports every job as verified;
  * a loaded job whose live environment differs from the plist on disk is
    reloaded (bootout + bootstrap), not trusted -- the plist edit alone never
    reaches a registered job;
  * a job that will not load is a failure with a non-zero exit, never a silent
    success;
  * a gateway profile without `dispatch_in_gateway: true` is a failure: the
    gateway would run with no motor;
  * a missing hermes-agent is a failure that says where it looked;
  * --check writes nothing and loads nothing.
"""
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "scripts" / "install.py"

FAKE_LAUNCHCTL = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, plistlib, re, sys
    state = os.environ["FAKE_LAUNCHCTL_STATE"]
    path = os.path.join(state, "jobs.json")
    jobs = json.load(open(path)) if os.path.exists(path) else {}
    log = open(os.path.join(state, "calls.log"), "a")
    log.write(" ".join(sys.argv[1:]) + "\n")
    cmd = sys.argv[1]
    def save():
        json.dump(jobs, open(path, "w"))
    if cmd == "bootstrap":
        text = re.sub(r"<!--.*?-->", "", open(sys.argv[3]).read(), flags=re.S)
        pl = plistlib.loads(text.encode())
        if pl["Label"] in os.environ.get("FAKE_LAUNCHCTL_REFUSE", "").split(","):
            sys.stderr.write("Bootstrap failed: 5: Input/output error\n"); sys.exit(5)
        if pl["Label"] in jobs:
            sys.stderr.write("Bootstrap failed: 17: File exists\n"); sys.exit(17)
        jobs[pl["Label"]] = pl.get("EnvironmentVariables", {})
        save()
    elif cmd == "bootout":
        label = sys.argv[2].rsplit("/", 1)[1]
        if label not in jobs:
            sys.exit(3)
        del jobs[label]; save()
    elif cmd == "print":
        label = sys.argv[2].rsplit("/", 1)[1]
        if label not in jobs:
            sys.stderr.write("Could not find service\n"); sys.exit(113)
        print("gui/501/%s = {" % label)
        if label in os.environ.get("FAKE_LAUNCHCTL_DEAD", "").split(","):
            print("\tstate = spawn scheduled\n\tlast exit code = 1")
        elif label in os.environ.get("FAKE_LAUNCHCTL_EXITED0", "").split(","):
            print("\tstate = not running\n\tlast exit code = 0")
        else:
            print("\tstate = running\n\tlast exit code = (never exited)")
        print("\tdefault environment = {\n\t\tPATH => /usr/bin\n\t}")
        print("\tenvironment = {")
        print("\t\tOSLogRateLimit => 64")
        for k, v in jobs[label].items():
            print("\t\t%s => %s" % (k, v))
        print("\t}")
        print("}")
    else:
        sys.exit(64)
''').lstrip()


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "hermes"
        self.agents = self.tmp / "LaunchAgents"
        self.state = self.tmp / "launchd-state"
        self.state.mkdir()
        py = self.home / "hermes-agent" / "venv" / "bin"
        py.mkdir(parents=True)
        for name in ("python", "python3"):
            (py / name).write_text("#!/bin/sh\n")
            (py / name).chmod(0o755)
        prof = self.home / "profiles" / "orchestrator"
        prof.mkdir(parents=True)
        (prof / "config.yaml").write_text("kanban:\n  dispatch_in_gateway: true\n")
        (self.home / "resilience.yaml").write_text("{}\n")
        self.launchctl = self.tmp / "launchctl"
        self.launchctl.write_text(FAKE_LAUNCHCTL)
        self.launchctl.chmod(self.launchctl.stat().st_mode | stat.S_IEXEC)

    def run_install(self, *args, env_extra=None):
        env = dict(os.environ)
        env.update({
            "HERMES_LAUNCHCTL": str(self.launchctl),
            "FAKE_LAUNCHCTL_STATE": str(self.state),
            "HERMES_INSTALL_SETTLE": "0",
        })
        env.update(env_extra or {})
        cmd = [sys.executable, str(INSTALL), "--hermes-home", str(self.home),
               "--agents-dir", str(self.agents), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def jobs(self):
        p = self.state / "jobs.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def calls(self):
        p = self.state / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    # -- the one command ---------------------------------------------------
    def test_one_run_registers_gateway_and_escalator_and_names_them(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("com.hermes-workflows.gateway", self.jobs())
        self.assertIn("com.hermes-workflows.escalator", self.jobs())
        self.assertRegex(r.stdout, r"registered\s+com\.hermes-workflows\.gateway")
        self.assertRegex(r.stdout, r"registered\s+com\.hermes-workflows\.escalator")
        for label in ("gateway", "escalator"):
            f = self.agents / f"com.hermes-workflows.{label}.plist"
            pl = plistlib.loads(f.read_bytes())
            self.assertNotIn("__", json.dumps(pl), f"placeholder left in {f}")

    def test_escalator_payload_is_copied_under_hermes_home(self):
        self.run_install()
        self.assertTrue((self.home / "launchd-bin" / "escalator.py").is_file())

    def test_optional_jobs_are_skipped_by_name_with_a_reason(self):
        r = self.run_install()
        self.assertRegex(r.stdout, r"skipped\s+com\.hermes-workflows\.backup\s+.*--with backup")
        self.assertRegex(r.stdout, r"skipped\s+com\.hermes-workflows\.bridge\s+.*--with bridge")
        self.assertNotIn("com.hermes-workflows.backup", self.jobs())

    # -- re-runnable -------------------------------------------------------
    def test_second_run_changes_nothing_and_still_reports(self):
        self.run_install()
        before = [(p.name, p.read_bytes()) for p in sorted(self.agents.iterdir())]
        n_calls = len(self.calls())
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = [(p.name, p.read_bytes()) for p in sorted(self.agents.iterdir())]
        self.assertEqual(before, after)
        new = self.calls()[n_calls:]
        self.assertFalse([c for c in new if not c.startswith("print")], new)
        self.assertRegex(r.stdout, r"unchanged\s+com\.hermes-workflows\.gateway\s+.*verified")
        self.assertRegex(r.stdout, r"unchanged\s+com\.hermes-workflows\.escalator\s+.*verified")

    # -- verifies rather than assumes ---------------------------------------
    def test_loaded_job_with_stale_environment_is_reloaded(self):
        self.run_install()
        jobs = self.jobs()
        del jobs["com.hermes-workflows.escalator"]["HERMES_WORKFLOWS_REPO"]
        (self.state / "jobs.json").write_text(json.dumps(jobs))
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("HERMES_WORKFLOWS_REPO",
                      self.jobs()["com.hermes-workflows.escalator"])
        self.assertRegex(r.stdout, r"reloaded\s+com\.hermes-workflows\.escalator\s+.*HERMES_WORKFLOWS_REPO")
        self.assertIn("bootout gui/%d/com.hermes-workflows.escalator" % os.getuid(), self.calls())

    def test_changed_plist_is_rewritten_and_reloaded(self):
        self.run_install()
        f = self.agents / "com.hermes-workflows.gateway.plist"
        f.write_text(f.read_text().replace("--replace", "--old"))
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--replace", f.read_text())
        self.assertRegex(r.stdout, r"updated\s+com\.hermes-workflows\.gateway")

    def test_job_that_will_not_load_fails_loudly(self):
        r = self.run_install(env_extra={"FAKE_LAUNCHCTL_REFUSE": "com.hermes-workflows.gateway"})
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAILED\s+com\.hermes-workflows\.gateway\s+.*not loaded")

    def test_gateway_without_dispatch_in_gateway_is_a_failure(self):
        (self.home / "profiles" / "orchestrator" / "config.yaml").write_text("kanban: {}\n")
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAILED\s+com\.hermes-workflows\.gateway\s+.*dispatch_in_gateway")

    def test_missing_hermes_agent_is_a_failure_that_says_where(self):
        for name in ("python", "python3"):
            (self.home / "hermes-agent" / "venv" / "bin" / name).unlink()
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(str(self.home / "hermes-agent" / "venv" / "bin"), r.stdout)
        self.assertEqual(self.jobs(), {})

    def test_check_writes_nothing_and_loads_nothing(self):
        r = self.run_install("--check")
        self.assertNotEqual(r.returncode, 0)  # nothing is loaded yet: honest
        self.assertFalse(self.agents.exists() and any(self.agents.iterdir()))
        self.assertEqual(self.jobs(), {})
        self.assertRegex(r.stdout, r"missing\s+com\.hermes-workflows\.gateway")

    def test_with_bridge_without_project_dir_fails_with_reason(self):
        r = self.run_install("--with", "bridge")
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAILED\s+com\.hermes-workflows\.bridge\s+.*--project-dir")

    def test_a_loaded_upstream_gateway_job_blocks_a_second_gateway(self):
        # `hermes gateway install` registers ai.hermes.gateway. Two KeepAlive
        # gateways each started with --replace would kill each other forever.
        self.agents.mkdir()
        (self.agents / "ai.hermes.gateway.plist").write_text("x")
        (self.state / "jobs.json").write_text(json.dumps({"ai.hermes.gateway": {}}))
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAILED\s+com\.hermes-workflows\.gateway\s+.*ai\.hermes\.gateway")
        self.assertNotIn("com.hermes-workflows.gateway", self.jobs())

    def test_an_unloaded_upstream_gateway_plist_is_named_but_not_blocking(self):
        self.agents.mkdir()
        (self.agents / "ai.hermes.gateway.plist").write_text("x")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ai.hermes.gateway", r.stdout)

    # -- loaded is not alive (found live: a gateway on a missing profile
    #    crash-looped while the installer reported a working loop) -----------
    def test_a_job_that_exits_1_after_bootstrap_is_failed_with_its_stderr(self):
        logs = self.home / "logs"
        logs.mkdir()
        (logs / "gateway.error.log").write_text(
            "Error: Profile 'orchestrator' does not exist. Create it with: hermes profile create orchestrator\n")
        r = self.run_install(env_extra={"FAKE_LAUNCHCTL_DEAD": "com.hermes-workflows.gateway"})
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAILED\s+com\.hermes-workflows\.gateway\s+.*last exit code 1")
        self.assertIn("Profile 'orchestrator' does not exist", r.stdout)
        self.assertNotIn("loop installed and verified", r.stdout)

    def test_an_unchanged_job_that_is_dead_is_still_failed(self):
        self.run_install()
        r = self.run_install(env_extra={"FAKE_LAUNCHCTL_DEAD": "com.hermes-workflows.escalator"})
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAILED\s+com\.hermes-workflows\.escalator\s+.*last exit code 1")

    def test_an_interval_job_that_exited_0_between_passes_is_fine(self):
        r = self.run_install(env_extra={"FAKE_LAUNCHCTL_EXITED0": "com.hermes-workflows.escalator"})
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_keepalive_gateway_that_is_not_running_is_failed(self):
        r = self.run_install(env_extra={"FAKE_LAUNCHCTL_EXITED0": "com.hermes-workflows.gateway"})
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAILED\s+com\.hermes-workflows\.gateway\s+.*not running")

    # -- which profile owns the motor ---------------------------------------
    def test_a_named_profile_that_does_not_exist_is_refused_with_path_and_choices(self):
        # The root config.yaml setting the key must not stand in for the profile.
        (self.home / "config.yaml").write_text("kanban:\n  dispatch_in_gateway: true\n")
        r = self.run_install("--profile", "coding")
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAILED\s+com\.hermes-workflows\.gateway\s+.*'coding' does not exist")
        self.assertIn(str(self.home / "profiles" / "coding"), r.stdout)
        self.assertIn("orchestrator", r.stdout)
        self.assertNotIn("com.hermes-workflows.gateway", self.jobs())

    def test_without_profile_the_one_motor_profile_is_chosen_and_named(self):
        (self.home / "profiles" / "coder").mkdir()
        (self.home / "profiles" / "coder" / "config.yaml").write_text("kanban: {}\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("profile orchestrator", r.stdout)

    def test_without_profile_two_motor_profiles_are_refused_and_listed(self):
        (self.home / "profiles" / "coding").mkdir()
        (self.home / "profiles" / "coding" / "config.yaml").write_text(
            "kanban:\n  dispatch_in_gateway: true\n")
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAILED\s+com\.hermes-workflows\.gateway\s+.*coding, orchestrator.*--profile")
        self.assertNotIn("com.hermes-workflows.gateway", self.jobs())


if __name__ == "__main__":
    unittest.main(verbosity=2)
