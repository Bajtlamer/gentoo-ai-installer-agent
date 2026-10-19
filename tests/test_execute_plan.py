import unittest
from unittest.mock import patch

from gentoo_agent.core import runner, runs
from gentoo_agent.core.errors import CONFIG_INVALID, TARGET_DRIFT, AgentError
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
STORAGE_PLAN = {
    **PLAN,
    "phases": [
        {"id": "preflight", "destructive": False, "apply": "apply-preflight", "check": "check-preflight"},
        {"id": "storage", "destructive": True, "apply": "apply-storage", "check": "check-storage"},
        {"id": "kernel", "destructive": True, "apply": "apply-kernel", "check": "check-kernel"},
    ],
}


IDENTITY = {"hostname": "gentoo", "root_uuid": "00000000-0000-4000-8000-000000000000", "hostkey": "SHA256:" + "A" * 43}
PROVISION_PLAN = {
    "plan_id": "b" * 16,
    "plan_hash": "b" * 64,
    "kind": "provision",
    "target": TARGET,
    "identity": IDENTITY,
    "phases": [
        {"id": "preflight", "destructive": False, "run": True, "apply": "apply-preflight", "check": "check-preflight"},
        {"id": "nvidia-drivers", "destructive": True, "run": True, "apply": "apply-drivers", "check": "check-drivers"},
        {"id": "gpu-online", "destructive": False, "run": False, "apply": "apply-online", "check": "check-online"},
    ],
}


class FakeShell:
    def __init__(self, returncodes=None, raise_on=None):
        self.returncodes = returncodes or {}
        self.raise_on = raise_on or {}
        self.calls = []
        self.keep_fds = []

    def __call__(self, target, label, command, timeout=3600, stream=False, input_data=None, on_output=None,
                 keep_fds=(), holder_python=None):
        self.calls.append((label, command, input_data))
        self.keep_fds.append((keep_fds, holder_python))
        if label in self.raise_on:
            raise self.raise_on[label]
        stdout = "".join(f"line {number}\n" for number in range(60))
        return {"label": label, "command": command, "returncode": self.returncodes.get(label, 0),
                "stdout": stdout, "stderr": "", "finished_at": "now"}


class StorageAlwaysAppliedTests(unittest.TestCase):
    """The storage phase erases the disk in every run: no check can decide it is already done.

    A disk with an old installation of the same layout passes the check. Skipping the phase reused
    the old filesystems and the old system instead of erasing the disk. This holds for a resumed
    run too.
    """

    def run_storage_plan(self, shell, resumed_from=None):
        run_id = runs.new_run_id()
        runs.write_run(run_id, {"run_id": run_id, "plan_id": STORAGE_PLAN["plan_id"], "status": "running",
                                "pid": None, "started_at": runs.now(), "resumed_from": resumed_from})
        with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift"):
            return runner.execute_plan(STORAGE_PLAN, run_id, RECORDS)

    def test_storage_is_applied_in_a_new_run_even_when_its_check_would_pass(self):
        for resumed_from in (None, "20261019T000000Z-abcdef"):
            with self.subTest(resumed_from=resumed_from), temp_home():
                shell = FakeShell()
                result = self.run_storage_plan(shell, resumed_from)
                labels = [call[0] for call in shell.calls]
                self.assertIn("storage", labels)
                self.assertNotIn("storage-check", labels)
                self.assertLess(labels.index("storage"), labels.index("storage-verify"))
                self.assertEqual(result["status"], "installed_pending_reboot")
                self.assertEqual(result["completed_phases"], ["preflight", "storage", "kernel"])

    def test_other_completed_phases_are_still_skipped(self):
        with temp_home():
            shell = FakeShell()
            self.run_storage_plan(shell)
        labels = [call[0] for call in shell.calls]
        self.assertIn("kernel-check", labels)
        self.assertNotIn("kernel", labels)
        self.assertNotIn("preflight", labels)

    def test_a_failed_storage_apply_stops_the_run(self):
        with temp_home():
            shell = FakeShell({"storage": 1})
            result = self.run_storage_plan(shell)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_phase"], "storage")
        self.assertNotIn("kernel-check", [call[0] for call in shell.calls])

    def test_the_verify_after_apply_still_runs(self):
        with temp_home():
            shell = FakeShell({"storage-verify": 1})
            result = self.run_storage_plan(shell)
        self.assertEqual(result["status"], "failed")
        self.assertIn("storage-verify", [call[0] for call in shell.calls])


class ProvisionPlanTests(unittest.TestCase):
    def run_provision(self, shell, plan=PROVISION_PLAN, phase_ids=None, identity_error=None):
        run_id = runs.new_run_id()
        runs.write_run(run_id, {"run_id": run_id, "plan_id": plan["plan_id"], "status": "running",
                                "pid": None, "started_at": runs.now()})
        with patch.object(runner, "remote_shell", shell), \
                patch.object(runner, "check_identity", side_effect=identity_error) as identity, \
                patch.object(runner, "check_drift") as drift:
            result = runner.execute_plan(plan, run_id, None, phase_ids=phase_ids)
        return result, identity, drift

    def run_provision_recording(self, shell, identity_error=None):
        """Like run_provision, but returns the run id so the run record can be read."""
        run_id = runs.new_run_id()
        runs.write_run(run_id, {"run_id": run_id, "plan_id": PROVISION_PLAN["plan_id"], "status": "running",
                                "pid": None, "started_at": runs.now()})
        with patch.object(runner, "remote_shell", shell), \
                patch.object(runner, "check_identity", side_effect=identity_error), \
                patch.object(runner, "check_drift"):
            result = runner.execute_plan(PROVISION_PLAN, run_id, None)
        return result, run_id

    def test_the_run_record_counts_only_the_phases_that_run(self):
        with temp_home():
            _, run_id = self.run_provision_recording(FakeShell({"nvidia-drivers-check": 1}))
            self.assertEqual(runs.read_run(run_id)["phase_count"], 2)

    def test_drift_before_the_destructive_apply_stops_the_run(self):
        error = AgentError(TARGET_DRIFT, "The target does not match the approved plan.")
        with temp_home():
            shell = FakeShell({"nvidia-drivers-check": 1})
            result, _ = self.run_provision_recording(shell, identity_error=[None, error])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], TARGET_DRIFT)
        self.assertNotIn("nvidia-drivers", [call[0] for call in shell.calls])

    def test_the_host_identity_is_checked_instead_of_disks(self):
        with temp_home():
            shell = FakeShell({"nvidia-drivers-check": 1})
            result, identity, drift = self.run_provision(shell)
        self.assertEqual(result["status"], "installed_pending_reboot")
        self.assertEqual(identity.call_count, 2)  # before the run, and before the destructive apply
        identity.assert_called_with(TARGET, IDENTITY)
        self.assertEqual(drift.call_count, 0)
        self.assertEqual([call[0] for call in shell.calls],
                         ["preflight-check", "nvidia-drivers-check", "nvidia-drivers", "nvidia-drivers-verify"])

    def test_a_verify_only_phase_is_never_run(self):
        with temp_home():
            shell = FakeShell()
            result, _, _ = self.run_provision(shell)
        self.assertFalse(any(call[0].startswith("gpu-online") for call in shell.calls))
        self.assertEqual(result["completed_phases"], ["preflight", "nvidia-drivers"])

    def test_a_verify_only_phase_cannot_be_selected(self):
        with temp_home():
            with self.assertRaises(AgentError) as caught:
                self.run_provision(FakeShell(), phase_ids=["gpu-online"])
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_a_plan_without_an_identity_fails_before_any_command(self):
        plan = {key: value for key, value in PROVISION_PLAN.items() if key != "identity"}
        with temp_home():
            shell = FakeShell()
            result, identity, _ = self.run_provision(shell, plan=plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], TARGET_DRIFT)
        self.assertEqual(shell.calls, [])
        self.assertEqual(identity.call_count, 0)

    def test_a_different_host_stops_the_run_before_any_phase(self):
        error = AgentError(TARGET_DRIFT, "The target does not match the approved plan.")
        with temp_home():
            shell = FakeShell()
            result, _, _ = self.run_provision(shell, identity_error=error)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], TARGET_DRIFT)
        self.assertEqual(shell.calls, [])


class InstallPlanKeepsItsGuardTests(unittest.TestCase):
    def test_an_install_plan_checks_disks_and_runs_every_phase_without_a_run_key(self):
        self.assertNotIn("kind", PLAN)
        self.assertTrue(all("run" not in phase for phase in PLAN["phases"]))
        with temp_home():
            shell = FakeShell({"preflight-check": 1, "authentication-check": 1, "kernel-check": 1})
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "running",
                                    "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", shell), \
                    patch.object(runner, "check_identity") as identity, \
                    patch.object(runner, "check_drift") as drift:
                result = runner.execute_plan(PLAN, run_id, RECORDS)
        self.assertEqual(result["status"], "installed_pending_reboot")
        self.assertEqual(identity.call_count, 0)
        self.assertEqual(drift.call_count, 3)
        drift.assert_called_with(TARGET, PLAN["disks"])
        self.assertEqual(result["completed_phases"], ["preflight", "authentication", "kernel"])
        applied = [call[0] for call in shell.calls]
        for label in ("preflight", "authentication", "kernel"):
            self.assertIn(label, applied)


class ExecutePlanTests(unittest.TestCase):
    def test_kept_descriptors_reach_every_remote_program(self):
        with temp_home():
            shell = FakeShell({"kernel-check": 1})
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "running",
                                    "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift"):
                result = runner.execute_plan(PLAN, run_id, RECORDS, keep_fds=(9,), holder_python="/py")
        self.assertEqual(result["status"], "installed_pending_reboot")
        self.assertEqual(shell.keep_fds, [((9,), "/py")] * 5)

    def run_plan(self, shell, drift=None):
        run_id = runs.new_run_id()
        runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "running",
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
            runs.write_run(run_id, {"run_id": run_id, "status": "running", "pid": None, "started_at": runs.now()})
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
            runs.write_run(run_id, {"run_id": run_id, "status": "running", "pid": None, "started_at": runs.now()})
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

    def test_unclaimed_run_is_refused_without_side_effects(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "status": "starting", "pid": None, "started_at": runs.now()})
            before = runs.read_run(run_id)
            shell = FakeShell()
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift"):
                with self.assertRaises(AgentError) as caught:
                    runner.execute_plan(PLAN, run_id, RECORDS)
            after = runs.read_run(run_id)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(shell.calls, [])
        self.assertEqual(before, after)

    def test_plan_without_disks_fails_before_any_command(self):
        plan = dict(PLAN, disks=[])
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "status": "running", "pid": None, "started_at": runs.now()})
            shell = FakeShell()
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift"):
                result = runner.execute_plan(plan, run_id, RECORDS)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], TARGET_DRIFT)
        self.assertEqual(shell.calls, [])

    def test_final_write_failure_is_not_recorded_as_failed(self):
        real_update = runs.update_run
        seen = []

        def flaky(run_id, **changes):
            if changes.get("status") == "installed_pending_reboot" and not seen:
                seen.append(True)
                raise OSError("disk full")
            return real_update(run_id, **changes)

        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "status": "running", "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", FakeShell()), patch.object(runner, "check_drift"), \
                    patch.object(runner, "update_run", flaky):
                with self.assertRaises(OSError):
                    runner.execute_plan(PLAN, run_id, RECORDS)
            self.assertEqual(runs.read_run(run_id)["status"], "running")

    def test_phase_subset_runs_only_selected_phases_in_plan_order(self):
        with temp_home():
            shell = FakeShell({"kernel-check": 1, "preflight-check": 1})
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "running",
                                    "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift"):
                result = runner.execute_plan(PLAN, run_id, RECORDS, phase_ids=["kernel", "preflight"])
        self.assertEqual(result["completed_phases"], ["preflight", "kernel"])
        self.assertNotIn("authentication-check", [call[0] for call in shell.calls])

    def test_unknown_phase_id_is_rejected_before_any_command(self):
        with temp_home():
            shell = FakeShell()
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "status": "running", "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift"):
                with self.assertRaises(AgentError) as caught:
                    runner.execute_plan(PLAN, run_id, RECORDS, phase_ids=["reboot"])
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(shell.calls, [])

    def test_drift_is_rechecked_before_each_destructive_apply(self):
        with temp_home():
            shell = FakeShell({"preflight-check": 1, "authentication-check": 1, "kernel-check": 1})
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "running",
                                    "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift") as drift:
                result = runner.execute_plan(PLAN, run_id, RECORDS)
        self.assertEqual(result["status"], "installed_pending_reboot")
        # once at the start, then before authentication and kernel (preflight is not destructive)
        self.assertEqual(drift.call_count, 3)

    def test_skipped_phases_need_no_drift_check(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "running",
                                    "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", FakeShell()), patch.object(runner, "check_drift") as drift:
                runner.execute_plan(PLAN, run_id, RECORDS)
        self.assertEqual(drift.call_count, 1)

    def test_drift_before_a_later_phase_stops_without_applying_it(self):
        drift = [None, None, AgentError(TARGET_DRIFT, "Disk /dev/sda does not match the approved plan.")]
        with temp_home():
            shell = FakeShell({"authentication-check": 1, "kernel-check": 1})
            _, result, record = self.run_plan(shell, drift=drift)
        labels = [call[0] for call in shell.calls]
        self.assertIn("authentication", labels)
        self.assertNotIn("kernel", labels)
        self.assertEqual(result["error"]["code"], TARGET_DRIFT)
        self.assertEqual(record["completed_phases"], ["preflight", "authentication"])


if __name__ == "__main__":
    unittest.main()
