import datetime as dt
import subprocess
import sys
import unittest

from gentoo_agent.core import runs
from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.errors import CONFIG_INVALID, RUN_ALREADY_ACTIVE, RUN_NOT_FOUND, AgentError
from tests.helpers import temp_home


def dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def record(run_id: str, **changes) -> dict:
    return {"run_id": run_id, "plan_id": "a" * 16, "status": "running", "pid": None,
            "started_at": runs.now(), **changes}


class RunRecordTests(unittest.TestCase):
    def test_run_ids_are_validated(self):
        run_id = runs.new_run_id()
        self.assertRegex(run_id, r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
        with self.assertRaises(AgentError) as caught:
            runs.run_dir("../x")
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_write_read_update_and_missing(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id))
            runs.update_run(run_id, status="failed")
            self.assertEqual(runs.read_run(run_id)["status"], "failed")
            with self.assertRaises(AgentError) as caught:
                runs.read_run(runs.new_run_id())
        self.assertEqual(caught.exception.code, RUN_NOT_FOUND)


class LockTests(unittest.TestCase):
    def test_lock_is_exclusive_while_the_holder_is_alive(self):
        import os

        with temp_home():
            first = runs.new_run_id()
            runs.write_run(first, record(first, pid=os.getpid()))
            runs.acquire_lock(first)
            with self.assertRaises(AgentError) as caught:
                runs.acquire_lock(runs.new_run_id())
            self.assertEqual(caught.exception.code, RUN_ALREADY_ACTIVE)
            runs.release_lock(first)
            runs.acquire_lock(runs.new_run_id())

    def test_stale_lock_is_reclaimed(self):
        with temp_home():
            stale = runs.new_run_id()
            runs.write_run(stale, record(stale, pid=dead_pid()))
            runs.acquire_lock(stale)
            fresh = runs.new_run_id()
            runs.acquire_lock(fresh)
            self.assertEqual(runs.lock_path().read_text(encoding="utf-8"), fresh)


class StatusTests(unittest.TestCase):
    def test_cancel_flag(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id))
            self.assertFalse(runs.cancel_requested(run_id))
            runs.request_cancel(run_id)
            self.assertTrue(runs.cancel_requested(run_id))

    def test_dead_worker_is_reported_as_interrupted(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id, pid=dead_pid()))
            runs.acquire_lock(run_id)
            status = runs.run_status(run_id)
            self.assertFalse(runs.lock_path().exists())
        self.assertEqual(status["status"], "interrupted")


class CredentialsAndLogTests(unittest.TestCase):
    def test_credentials_are_private_and_returned_once(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id, status="failed"))
            runs.write_credentials(run_id, {"credentials": {"root": "secret"}, "applied": True})
            path = runs.run_dir(run_id) / "credentials.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(runs.take_credentials(run_id)["credentials"], {"root": "secret"})
            self.assertIsNone(runs.take_credentials(run_id))

    def test_stale_credentials_are_purged(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id, status="failed"))
            runs.write_credentials(run_id, {"credentials": {"root": "secret"}, "applied": False})
            path = runs.run_dir(run_id) / "credentials.json"
            data = load_json(path)
            data["written_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).isoformat()
            save_json(path, data, mode=0o600)
            runs.purge_stale_credentials()
            self.assertFalse(path.exists())

    def test_log_is_read_in_pages(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id))
            for index in range(5):
                runs.append_log(run_id, {"index": index})
            page = runs.read_log(run_id, offset=1, limit=2)
            with self.assertRaises(AgentError):
                runs.read_log(run_id, offset=-1, limit=2)
        self.assertEqual([entry["index"] for entry in page["entries"]], [1, 2])
        self.assertEqual(page["next_offset"], 3)
        self.assertEqual(page["total"], 5)


if __name__ == "__main__":
    unittest.main()
