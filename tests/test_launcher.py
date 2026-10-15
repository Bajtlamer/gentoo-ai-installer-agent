import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import approval, install_plan, phases, runs
from gentoo_agent.core.errors import APPROVAL_REQUIRED, RUN_ALREADY_ACTIVE, AgentError
from gentoo_agent.jobs import launcher
from tests.helpers import temp_home


REPO = Path(__file__).resolve().parents[1]


def approved_plan(host: str = "192.0.2.10") -> dict:
    target = {"name": "gentoo", "host": host, "port": 22, "user": "root",
              "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True}
    install = json.loads((REPO / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))
    install.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True,
                    "password_source": "generate"})
    project = json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8"))
    stdout = json.dumps({"blockdevices": [
        {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK", "serial": "QM00001", "wwn": None},
    ]})
    inventory = {"results": [{"label": "disk_identity", "returncode": 0, "stdout": stdout}]}
    with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAA test"):
        plan = install_plan.save_install_plan(install_plan.create_install_plan(target, install, project, inventory))
    approval.record_approval(plan, "cli")
    return plan


class FakeProcess:
    pid = 424242

    def __init__(self):
        self.stdin = self
        self.written = b""
        self.closed = False

    def write(self, data):
        self.written += data

    def close(self):
        self.closed = True


class LauncherTests(unittest.TestCase):
    def test_unapproved_plan_never_spawns_a_worker(self):
        with temp_home():
            plan = approved_plan()
            approval.approval_path(plan["plan_id"]).unlink()
            with patch.object(launcher.subprocess, "Popen") as popen:
                with self.assertRaises(AgentError) as caught:
                    launcher.start_run(plan["plan_id"])
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)
        popen.assert_not_called()

    def test_start_resolves_passwords_first_and_hands_them_over_stdin(self):
        process = FakeProcess()
        with temp_home() as home, patch.object(launcher.subprocess, "Popen", return_value=process) as popen:
            plan = approved_plan()
            run_id = launcher.start_run(plan["plan_id"])
            record = runs.read_run(run_id)
            command = popen.call_args.args[0]
            kwargs = popen.call_args.kwargs
        self.assertEqual(command[1:], ["-P", "-m", "gentoo_agent.jobs.worker", run_id])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["GENTOO_AGENT_HOME"], str(home))
        self.assertTrue(kwargs["env"]["PYTHONPATH"].startswith(str(REPO)))
        payload = json.loads(process.written)
        self.assertEqual(set(payload["generated_credentials"]), {"root", "gentoo"})
        self.assertTrue(payload["password_records"].startswith("root:"))
        self.assertTrue(process.closed)
        self.assertEqual(record["status"], "starting")
        self.assertEqual(record["pid"], 424242)
        self.assertEqual(record["plan_hash"], plan["plan_hash"])
        self.assertNotIn("password", json.dumps(record))

    def test_password_variables_are_not_passed_to_the_worker(self):
        secrets_env = {"GENTOO_ROOT_PASSWORD": "root-secret", "GENTOO_USER_PASSWORD": "user-secret"}
        with temp_home(), patch.dict(os.environ, secrets_env), \
                patch.object(launcher.subprocess, "Popen", return_value=FakeProcess()) as popen:
            plan = approved_plan()
            launcher.start_run(plan["plan_id"])
            environment = popen.call_args.kwargs["env"]
        # Compare names only, so a failure never prints the whole environment.
        self.assertEqual(set(secrets_env) & set(environment), set())

    def test_a_failed_handover_kills_the_worker_and_fails_the_run(self):
        class BrokenProcess(FakeProcess):
            killed = False

            def write(self, data):
                raise BrokenPipeError("worker went away")

            def kill(self):
                BrokenProcess.killed = True

        with temp_home(), patch.object(launcher.subprocess, "Popen", return_value=BrokenProcess()), \
                patch.object(launcher, "new_run_id", return_value="20261015T000000Z-abcdef"):
            plan = approved_plan()
            with self.assertRaises(BrokenPipeError):
                launcher.start_run(plan["plan_id"])
            record = runs.read_run("20261015T000000Z-abcdef")
            lock_exists = runs.lock_path().exists()
        self.assertTrue(BrokenProcess.killed)
        self.assertEqual(record["status"], "failed")
        self.assertFalse(lock_exists)

    def test_second_start_while_active_is_refused(self):
        with temp_home(), patch.object(launcher.subprocess, "Popen", return_value=FakeProcess()):
            plan = approved_plan()
            first = launcher.start_run(plan["plan_id"])
            runs.update_run(first, pid=os.getpid())
            with self.assertRaises(AgentError) as caught:
                launcher.start_run(plan["plan_id"])
        self.assertEqual(caught.exception.code, RUN_ALREADY_ACTIVE)

    def test_resume_starts_a_new_run_for_the_same_plan(self):
        with temp_home(), patch.object(launcher.subprocess, "Popen", return_value=FakeProcess()):
            plan = approved_plan()
            first = launcher.start_run(plan["plan_id"])
            runs.update_run(first, status="failed")
            runs.release_lock(first)
            second = launcher.resume_run(first)
            record = runs.read_run(second)
        self.assertNotEqual(first, second)
        self.assertEqual(record["resumed_from"], first)
        self.assertEqual(record["plan_id"], plan["plan_id"])


if __name__ == "__main__":
    unittest.main()
