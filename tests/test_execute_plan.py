import unittest
from unittest.mock import patch

from gentoo_agent.core import runner, runs
from gentoo_agent.core.errors import TARGET_DRIFT, AgentError
from tests.helpers import temp_home


TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root"}
PLAN = {
    "plan_id": "a" * 16,
    "plan_hash": "a" * 64,
    "target": TARGET,
    "disks": [{"path": "/dev/sda", "size_bytes": 1, "model": "m", "serial": "s", "wwn": ""}],
    "phases": [
        {"id": "preflight", "destructive": False, "apply": "apply-preflight", "check": "check-preflight"},
        {"id": "authentication", "destructive": True, "apply": "apply-auth", "check": "check-auth"},
        {"id": "kernel", "destructive": True, "apply": "apply-kernel", "check": "check-kernel"},
    ],
}
RECORDS = "root:pw-root\ngentoo:pw-user\n"


class FakeShell:
    def __init__(self, returncodes=None, raise_on=None):
        self.returncodes = returncodes or {}
        self.raise_on = raise_on or {}
        self.calls = []

    def __call__(self, target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
        self.calls.append((label, command, input_data))
        if label in self.raise_on:
            raise self.raise_on[label]
        stdout = "".join(f"line {number}\n" for number in range(60))
        return {"label": label, "command": command, "returncode": self.returncodes.get(label, 0),
                "stdout": stdout, "stderr": "", "finished_at": "now"}


class ExecutePlanTests(unittest.TestCase):
    def run_plan(self, shell, drift=None):
        run_id = runs.new_run_id()
        runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "starting",
                                "pid": None, "started_at": runs.now()})
        with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift", side_effect=drift):
            result = runner.execute_plan(PLAN, run_id, RECORDS)
        return run_id, result, runs.read_run(run_id)

    def test_completed_checks_skip_apply(self):
        with temp_home():
            shell = FakeShell()
            _, result, record = self.run_plan(shell)
        self.assertEqual(result["status"], "installed_pending_reboot")
        self.assertEqual([call[0] for call in shell.calls], ["preflight-check", "authentication-check", "kernel-check"])
        self.assertEqual(record["status"], "installed_pending_reboot")
        self.assertEqual(record["completed_phases"], ["preflight", "authentication", "kernel"])

    def test_failed_apply_stops_with_output_tail(self):
        with temp_home():
            shell = FakeShell({"kernel-check": 1, "kernel": 4})
            _, result, record = self.run_plan(shell)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_phase"], "kernel")
        self.assertEqual(result["error"]["code"], "PHASE_FAILED")
        self.assertEqual(len(result["output_tail"].splitlines()), 40)
        self.assertTrue(result["output_tail"].endswith("line 59"))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["last_error"], "kernel apply returned 4")

    def test_failed_verify_stops(self):
        with temp_home():
            _, result, record = self.run_plan(FakeShell({"kernel-check": 1, "kernel-verify": 2}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(record["last_error"], "kernel verification returned 2")

    def test_drift_stops_before_any_phase(self):
        drift = AgentError(TARGET_DRIFT, "Disk /dev/sda does not match the approved plan.")
        with temp_home():
            shell = FakeShell()
            _, result, record = self.run_plan(shell, drift=drift)
        self.assertEqual(shell.calls, [])
        self.assertEqual(result["error"]["code"], TARGET_DRIFT)
        self.assertEqual(record["status"], "failed")

    def test_cancel_stops_at_the_next_phase_boundary(self):
        with temp_home():
            shell = FakeShell({"preflight-check": 1})
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "status": "starting", "pid": None, "started_at": runs.now()})
            original = shell.__call__

            def cancel_after_preflight_verify(*args, **kwargs):
                result = original(*args, **kwargs)
                if args[1] == "preflight-verify":
                    runs.request_cancel(run_id)
                return result

            with patch.object(runner, "remote_shell", cancel_after_preflight_verify), patch.object(runner, "check_drift"):
                result = runner.execute_plan(PLAN, run_id, RECORDS)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["completed_phases"], ["preflight"])

    def test_unexpected_exception_is_recorded(self):
        with temp_home():
            _, result, record = self.run_plan(FakeShell(raise_on={"authentication-check": OSError("ssh vanished")}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_phase"], "authentication")
        self.assertIn("ssh vanished", record["last_error"])

    def test_interrupt_is_recorded_and_reraised(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "status": "starting", "pid": None, "started_at": runs.now()})
            shell = FakeShell(raise_on={"kernel-check": KeyboardInterrupt()})
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift"):
                with self.assertRaises(KeyboardInterrupt):
                    runner.execute_plan(PLAN, run_id, RECORDS)
            self.assertEqual(runs.read_run(run_id)["status"], "interrupted")

    def test_passwords_reach_only_the_authentication_apply_and_never_the_log(self):
        with temp_home():
            shell = FakeShell({"authentication-check": 1})
            run_id, result, _ = self.run_plan(shell)
            log_text = (runs.run_dir(run_id) / "log.jsonl").read_text(encoding="utf-8")
        inputs = {label: data for label, _, data in shell.calls}
        self.assertEqual(inputs["authentication"], RECORDS)
        self.assertTrue(all(data is None for label, data in inputs.items() if label != "authentication"))
        self.assertTrue(result["authentication_applied"])
        self.assertNotIn("pw-root", log_text)
        self.assertNotIn("pw-user", log_text)


if __name__ == "__main__":
    unittest.main()
