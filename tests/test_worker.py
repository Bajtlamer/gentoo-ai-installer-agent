import io
import shutil
import time
import unittest
from unittest.mock import patch

from gentoo_agent.core import approval, runs
from gentoo_agent.core.config import load_json
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

    def test_credentials_are_pending_while_the_plan_executes(self):
        seen = {}

        def execute(plan, run_id, records):
            seen["credentials"] = load_json(runs.credentials_path(run_id))
            return {"status": "installed_pending_reboot", "authentication_applied": True}

        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            with patch.object(worker, "execute_plan", side_effect=execute):
                worker.run(run_id, {"password_records": "root:x\n", "generated_credentials": {"root": "x"}})
            credentials = runs.take_credentials(run_id)
        self.assertEqual(seen["credentials"]["credentials"], {"root": "x"})
        self.assertIn("applied", seen["credentials"])
        self.assertIsNone(seen["credentials"]["applied"])
        self.assertIs(credentials["applied"], True)

    def test_final_applied_value_is_written_after_execution(self):
        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            outcome = {"status": "failed", "authentication_applied": False}
            with patch.object(worker, "execute_plan", return_value=outcome):
                worker.run(run_id, {"password_records": "root:x\n", "generated_credentials": {"root": "x"}})
            credentials = runs.take_credentials(run_id)
        self.assertIs(credentials["applied"], False)

    def test_credentials_stay_pending_when_execution_is_interrupted(self):
        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            with patch.object(worker, "execute_plan", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    worker.run(run_id, {"password_records": "root:x\n", "generated_credentials": {"root": "x"}})
            credentials = runs.take_credentials(run_id)
            lock_exists = runs.lock_path().exists()
        self.assertIsNone(credentials["applied"])
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
        self.assertIsNone(credentials)

    def test_unclaimed_run_writes_no_credentials(self):
        with temp_home():
            run_id = runs.new_run_id()
            with patch.object(worker, "execute_plan") as execute:
                result = worker.run(run_id, {"password_records": "root:x\n", "generated_credentials": {"root": "x"}})
            credentials_exist = runs.credentials_path(run_id).exists()
        execute.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(credentials_exist)

    def run_main_with_stdin(self, text):
        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            with patch.object(worker.sys, "stdin", io.StringIO(text)), \
                    patch.object(worker, "execute_plan") as execute:
                exit_code = worker.main([run_id])
            record = runs.read_run(run_id)
            lock_exists = runs.lock_path().exists()
        return exit_code, execute, record, lock_exists

    def test_invalid_start_payload_never_executes(self):
        for text in ["", '{"password_records": "root:x', "[]",
                     '{"password_records": null}',
                     '{"password_records": 5, "generated_credentials": {}}',
                     '{"password_records": null, "generated_credentials": {"root": 1}}',
                     '{"password_records": null, "generated_credentials": {}, "extra": 1}']:
            with self.subTest(stdin=text):
                exit_code, execute, record, lock_exists = self.run_main_with_stdin(text)
                execute.assert_not_called()
                self.assertEqual(exit_code, 1)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error"]["code"], "CONFIG_INVALID")
                self.assertFalse(lock_exists)

    def test_parse_payload_accepts_a_valid_handshake(self):
        payload = worker.parse_payload('{"password_records": "root:x\\n", "generated_credentials": {"root": "x"}}')
        self.assertEqual(payload, {"password_records": "root:x\n", "generated_credentials": {"root": "x"}})

    def test_missing_password_records_for_a_plan_that_sets_passwords_fails(self):
        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            with patch.object(worker, "execute_plan") as execute:
                result = worker.run(run_id, {"password_records": None, "generated_credentials": {}})
            record = runs.read_run(run_id)
            lock_exists = runs.lock_path().exists()
        execute.assert_not_called()
        self.assertEqual(result["error"]["code"], "CONFIG_INVALID")
        self.assertEqual(record["status"], "failed")
        self.assertFalse(lock_exists)

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH client not installed")
    def test_real_worker_process_reports_an_unreachable_target(self):
        with temp_home():
            plan = approved_plan(host="gentoo-agent-test.invalid")
            run_id = launcher.start_run(plan["plan_id"])
            deadline = time.monotonic() + 60
            record = runs.run_status(run_id)
            while ((record["status"] in runs.ACTIVE_STATUSES or runs.lock_path().exists())
                   and time.monotonic() < deadline):
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
