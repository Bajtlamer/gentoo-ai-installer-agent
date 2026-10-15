import shutil
import time
import unittest
from unittest.mock import patch

from gentoo_agent.core import approval, runs
from gentoo_agent.jobs import launcher, worker
from tests.helpers import temp_home
from tests.test_launcher import approved_plan


class WorkerTests(unittest.TestCase):
    def new_run(self, plan):
        run_id = runs.new_run_id()
        runs.acquire_lock(run_id, {"run_id": run_id, "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"],
                                   "status": "starting", "pid": None, "started_at": runs.now()})
        return run_id

    def test_worker_runs_the_plan_writes_credentials_and_releases_the_lock(self):
        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            outcome = {"status": "failed", "authentication_applied": True}
            with patch.object(worker, "execute_plan", return_value=outcome) as execute:
                result = worker.run(run_id, {"password_records": "root:x\n", "generated_credentials": {"root": "x"}})
            credentials = runs.take_credentials(run_id)
            lock_exists = runs.lock_path().exists()
        self.assertEqual(result, outcome)
        self.assertEqual(execute.call_args.args[2], "root:x\n")
        self.assertEqual(credentials["credentials"], {"root": "x"})
        self.assertTrue(credentials["applied"])
        self.assertFalse(lock_exists)

    def test_worker_refuses_a_plan_whose_approval_is_gone(self):
        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            approval.approval_path(plan["plan_id"]).unlink()
            with patch.object(worker, "execute_plan") as execute:
                result = worker.run(run_id, {"password_records": None, "generated_credentials": {}})
            record = runs.read_run(run_id)
        execute.assert_not_called()
        self.assertEqual(result["error"]["code"], "APPROVAL_REQUIRED")
        self.assertEqual(record["status"], "failed")

    def test_worker_does_not_execute_a_run_that_is_no_longer_starting(self):
        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            runs.update_run(run_id, status="interrupted")
            with patch.object(worker, "execute_plan") as execute:
                result = worker.run(run_id, {"password_records": "root:x\n", "generated_credentials": {"root": "x"}})
            record = runs.read_run(run_id)
            credentials = runs.take_credentials(run_id)
        execute.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "CONFIG_INVALID")
        self.assertEqual(record["status"], "interrupted")
        self.assertFalse(credentials["applied"])

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH client not installed")
    def test_real_worker_process_reports_an_unreachable_target(self):
        with temp_home():
            plan = approved_plan(host="gentoo-agent-test.invalid")
            run_id = launcher.start_run(plan["plan_id"])
            deadline = time.monotonic() + 60
            record = runs.run_status(run_id)
            while record["status"] in runs.ACTIVE_STATUSES and time.monotonic() < deadline:
                time.sleep(0.2)
                record = runs.run_status(run_id)
            errors = (runs.run_dir(run_id) / "worker.err").read_text(encoding="utf-8", errors="replace")
            credentials = runs.take_credentials(run_id)
            lock_exists = runs.lock_path().exists()
        self.assertEqual(record["status"], "failed", errors)
        self.assertEqual(record["error"]["code"], "SSH_UNREACHABLE", errors)
        self.assertFalse(credentials["applied"])
        self.assertFalse(lock_exists)


if __name__ == "__main__":
    unittest.main()
