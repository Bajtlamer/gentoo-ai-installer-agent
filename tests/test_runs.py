import datetime as dt
import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import runs
from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.errors import CONFIG_INVALID, RUN_ALREADY_ACTIVE, RUN_NOT_FOUND, AgentError
from tests.helpers import temp_home


REPO_ROOT = Path(__file__).resolve().parents[1]


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
        with temp_home():
            first = runs.new_run_id()
            runs.acquire_lock(first, record(first, pid=os.getpid()))
            second = runs.new_run_id()
            with self.assertRaises(AgentError) as caught:
                runs.acquire_lock(second, record(second))
            self.assertEqual(caught.exception.code, RUN_ALREADY_ACTIVE)
            runs.release_lock(first)
            third = runs.new_run_id()
            runs.acquire_lock(third, record(third))

    def test_stale_lock_is_reclaimed(self):
        with temp_home():
            stale = runs.new_run_id()
            runs.acquire_lock(stale, record(stale, pid=dead_pid()))
            fresh = runs.new_run_id()
            runs.acquire_lock(fresh, record(fresh))
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
            runs.acquire_lock(run_id, record(run_id, pid=dead_pid()))
            status = runs.run_status(run_id)
            self.assertFalse(runs.lock_path().exists())
            stored = runs.read_run(run_id)
        self.assertEqual(status["status"], "interrupted")
        expected = {"code": "PHASE_FAILED", "message": "The worker process ended without recording a result.",
                    "details": {"run_id": run_id}}
        self.assertEqual(status["error"], expected)
        self.assertEqual(stored["error"], expected)
        self.assertEqual(stored["last_error"], expected["message"])


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


REPO_ROOT = Path(__file__).resolve().parents[1]

LOCK_SCRIPT = """
import pathlib, sys, time
from gentoo_agent.core import runs
from gentoo_agent.core.errors import RUN_ALREADY_ACTIVE, AgentError
start = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 30
while not start.exists() and time.monotonic() < deadline:
    time.sleep(0.001)
run_id = runs.new_run_id()
record = {"run_id": run_id, "status": "starting", "pid": None, "started_at": runs.now()}
try:
    runs.acquire_lock(run_id, record)
except AgentError as error:
    print("ACTIVE" if error.code == RUN_ALREADY_ACTIVE else error.code)
else:
    print("OK")
"""


class ConcurrentLockTests(unittest.TestCase):
    def test_only_one_of_many_processes_acquires_the_lock(self):
        with temp_home() as home:
            env = {**os.environ, "GENTOO_AGENT_HOME": str(home), "PYTHONPATH": str(REPO_ROOT)}
            for round_number in range(5):
                start = home / f"start-{round_number}"
                processes = [
                    subprocess.Popen([sys.executable, "-c", LOCK_SCRIPT, str(start)], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(12)
                ]
                time.sleep(0.5)
                start.touch()
                outputs = [process.communicate(timeout=60) for process in processes]
                results = [stdout.strip() for stdout, _ in outputs]
                errors = [stderr for _, stderr in outputs if stderr]
                self.assertEqual(results.count("OK"), 1, (round_number, results, errors[:1]))
                self.assertEqual(results.count("ACTIVE"), 11, results)
                runs.lock_path().unlink()

    def test_malformed_holder_record_fails_closed(self):
        for broken in ({"started_at": "not a date"}, {"started_at": None}, {"started_at": "2026-01-01T00:00:00"}):
            with self.subTest(broken=broken), temp_home():
                holder = runs.new_run_id()
                holder_record = record(holder, status="starting")
                holder_record.update(broken)
                runs.acquire_lock(holder, holder_record)
                other = runs.new_run_id()
                with self.assertRaises(AgentError) as caught:
                    runs.acquire_lock(other, record(other))
                self.assertEqual(caught.exception.code, RUN_ALREADY_ACTIVE)

    def test_missing_started_at_fails_closed(self):
        with temp_home():
            holder = runs.new_run_id()
            holder_record = record(holder, status="starting")
            del holder_record["started_at"]
            runs.acquire_lock(holder, holder_record)
            other = runs.new_run_id()
            with self.assertRaises(AgentError) as caught:
                runs.acquire_lock(other, record(other))
            self.assertEqual(caught.exception.code, RUN_ALREADY_ACTIVE)

    def test_lock_that_is_a_directory_never_raises_a_raw_error(self):
        with temp_home():
            runs.lock_path().mkdir(parents=True)
            run_id = runs.new_run_id()
            with self.assertRaises(AgentError) as caught:
                runs.acquire_lock(run_id, record(run_id))
            self.assertEqual(caught.exception.code, RUN_ALREADY_ACTIVE)
            stamp = time.time() - 120
            os.utime(runs.lock_path(), (stamp, stamp))
            with self.assertRaises(AgentError) as caught:
                runs.acquire_lock(run_id, record(run_id))
            self.assertEqual(caught.exception.code, CONFIG_INVALID)
            self.assertIn("not a regular file", caught.exception.message)
            self.assertTrue(runs.lock_path().is_dir())

    def test_acquire_needs_fcntl(self):
        with temp_home(), patch.object(runs, "fcntl", None):
            run_id = runs.new_run_id()
            with self.assertRaises(AgentError) as caught:
                runs.acquire_lock(run_id, record(run_id))
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


class StatusConcurrencyTests(unittest.TestCase):
    def test_run_status_never_overwrites_a_terminal_status(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id, status="failed", pid=dead_pid()))
            self.assertEqual(runs.run_status(run_id)["status"], "failed")
            self.assertEqual(runs.read_run(run_id)["status"], "failed")

    def test_run_status_rereads_the_record_under_the_mutex(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.acquire_lock(run_id, record(run_id, pid=dead_pid()))
            result = {}
            with runs._mutex():
                reader = threading.Thread(target=lambda: result.update(runs.run_status(run_id)))
                reader.start()
                time.sleep(0.3)
                final = runs.read_run(run_id)
                final["status"] = "failed"
                save_json(runs.run_dir(run_id) / "run.json", final)
            reader.join(10)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(runs.read_run(run_id)["status"], "failed")

    def test_unreaped_zombie_child_is_not_alive(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            time.sleep(0.2)
            deadline = time.monotonic() + 5
            while runs.pid_alive(child.pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(runs.pid_alive(child.pid))
        finally:
            child.wait()

    def test_claim_run_moves_starting_to_running(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.acquire_lock(run_id, record(run_id, status="starting"))
            claimed = runs.claim_run(run_id, 4242)
            self.assertEqual((claimed["status"], claimed["pid"]), ("running", 4242))
            self.assertEqual(runs.read_run(run_id)["status"], "running")
            with self.assertRaises(AgentError) as caught:
                runs.claim_run(run_id, 4242)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_claim_run_refuses_an_interrupted_run(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.acquire_lock(run_id, record(run_id, status="starting"))
            runs.update_run(run_id, status="interrupted")
            with self.assertRaises(AgentError) as caught:
                runs.claim_run(run_id, 4242)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertIn("no longer startable", caught.exception.message)


class CredentialsConcurrencyTests(unittest.TestCase):
    def test_take_credentials_is_one_time_under_concurrency(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id, status="failed"))
            for _ in range(20):
                runs.write_credentials(run_id, {"credentials": {"root": "secret"}})
                barrier = threading.Barrier(4)
                results, errors = [], []

                def take():
                    barrier.wait()
                    try:
                        results.append(runs.take_credentials(run_id))
                    except Exception as error:  # noqa: BLE001
                        errors.append(error)

                threads = [threading.Thread(target=take) for _ in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(10)
                self.assertEqual(errors, [])
                self.assertEqual(len([item for item in results if item is not None]), 1, results)
            leftovers = [path.name for path in runs.run_dir(run_id).iterdir() if "credentials" in path.name]
            self.assertEqual(leftovers, [])

    def test_credentials_that_are_not_an_object_are_rejected(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id, status="failed"))
            runs.credentials_path(run_id).write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(AgentError) as caught:
                runs.take_credentials(run_id)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)
            self.assertFalse(runs.credentials_path(run_id).exists())

    def test_malformed_credentials_do_not_break_purge(self):
        with temp_home():
            old, fresh, naive, other = (runs.new_run_id() for _ in range(4))
            for run_id in (old, fresh, naive, other):
                runs.write_run(run_id, record(run_id, status="failed"))
            old_path = runs.credentials_path(old)
            old_path.write_text("{not json", encoding="utf-8")
            stamp = time.time() - 25 * 3600
            os.utime(old_path, (stamp, stamp))
            fresh_path = runs.credentials_path(fresh)
            fresh_path.write_text("{not json", encoding="utf-8")
            naive_path = runs.credentials_path(naive)
            save_json(naive_path, {"written_at": "2020-01-01T00:00:00"})
            runs.purge_stale_credentials()
            self.assertFalse(old_path.exists())
            self.assertTrue(fresh_path.exists())
            self.assertTrue(naive_path.exists())
            self.assertEqual(runs.run_status(other)["status"], "failed")


class LogRobustnessTests(unittest.TestCase):
    def test_non_integer_paging_is_rejected(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id))
            runs.append_log(run_id, {"index": 0})
            for offset, limit in (("1", 2), (None, 2), (0, "1"), (0, None), (True, 2), (0, True)):
                with self.subTest(offset=offset, limit=limit):
                    with self.assertRaises(AgentError) as caught:
                        runs.read_log(run_id, offset=offset, limit=limit)
                    self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_partial_and_corrupt_lines(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id))
            runs.append_log(run_id, {"index": 0})
            with (runs.run_dir(run_id) / "log.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("{broken\n")
                handle.write('{"index": 2}\n')
                handle.write('{"index": 3, "par')
            page = runs.read_log(run_id, offset=0, limit=10)
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["entries"][0], {"index": 0})
        self.assertEqual(page["entries"][1], {"corrupt": True, "line": 1})
        self.assertEqual(page["entries"][2], {"index": 2})
        self.assertEqual(page["next_offset"], 3)

    def test_long_output_keeps_its_tail(self):
        # A full install log is ~12 MB; MCP sends each page twice, and Claude Code
        # disconnects a server that writes more than 16 MB in one message.
        stdout = "x" * (runs.MAX_LOG_OUTPUT + 10) + "the error is here"
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id))
            runs.append_log(run_id, {"phase": "kernel", "stdout": stdout, "stderr": "short"})
            entry = runs.read_log(run_id, offset=0, limit=1)["entries"][0]
            self.assertEqual(entry["stdout"], stdout[-runs.MAX_LOG_OUTPUT:])
            self.assertTrue(entry["stdout"].endswith("the error is here"))
            self.assertEqual(entry["stdout_truncated"], len(stdout))
            self.assertEqual(entry["stderr"], "short")
            self.assertNotIn("stderr_truncated", entry)
            self.assertEqual(len(load_json(runs.run_dir(run_id) / "log.jsonl")["stdout"]), len(stdout))

    def test_page_stops_at_the_size_budget(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id))
            count = runs.MAX_LOG_PAGE_CHARS // runs.MAX_LOG_OUTPUT + 5
            for index in range(count):
                runs.append_log(run_id, {"index": index, "stdout": "y" * (runs.MAX_LOG_OUTPUT * 2)})
            first = runs.read_log(run_id, offset=0, limit=runs.MAX_LOG_PAGE)
            self.assertLess(len(json.dumps(first)), runs.MAX_LOG_PAGE_CHARS + runs.MAX_LOG_OUTPUT * 2)
            self.assertLess(first["next_offset"], count)
            self.assertEqual(first["total"], count)
            second = runs.read_log(run_id, offset=first["next_offset"], limit=runs.MAX_LOG_PAGE)
            self.assertEqual(second["entries"][0]["index"], first["next_offset"])
            self.assertEqual(second["next_offset"], count)


class WorkerLockTests(unittest.TestCase):
    def start(self, pid: int) -> str:
        run_id = runs.new_run_id()
        runs.acquire_lock(run_id, {"run_id": run_id, "plan_id": "a" * 16, "status": "running", "pid": pid,
                                   "started_at": runs.now()})
        return run_id

    def test_held_worker_lock_keeps_the_run_active_even_if_the_pid_is_gone(self):
        with temp_home():
            run_id = self.start(dead_pid())
            handle = runs.hold_worker_lock(run_id)
            try:
                self.assertEqual(runs.run_status(run_id)["status"], "running")
                with self.assertRaises(AgentError) as caught:
                    runs.acquire_lock(runs.new_run_id(), {"status": "starting", "pid": None, "started_at": runs.now()})
                self.assertEqual(caught.exception.code, "RUN_ALREADY_ACTIVE")
            finally:
                handle.close()

    def test_released_worker_lock_ends_the_run_even_if_the_pid_is_alive(self):
        with temp_home():
            run_id = self.start(os.getpid())
            runs.hold_worker_lock(run_id).close()
            self.assertEqual(runs.run_status(run_id)["status"], "interrupted")

    def test_a_second_worker_cannot_hold_the_same_run(self):
        with temp_home():
            run_id = self.start(os.getpid())
            handle = runs.hold_worker_lock(run_id)
            try:
                with self.assertRaises(AgentError) as caught:
                    runs.hold_worker_lock(run_id)
                self.assertEqual(caught.exception.code, "RUN_ALREADY_ACTIVE")
            finally:
                handle.close()

    def test_held_worker_lock_keeps_a_run_without_pid_active_after_the_grace(self):
        with temp_home():
            run_id = runs.new_run_id()
            long_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
            runs.acquire_lock(run_id, {"run_id": run_id, "plan_id": "a" * 16, "status": "starting", "pid": None,
                                       "started_at": long_ago})
            handle = runs.hold_worker_lock(run_id)
            try:
                status = runs.run_status(run_id)["status"]
            finally:
                handle.close()
        self.assertEqual(status, "starting")

    def test_unusable_flock_falls_back_to_the_pid_probe(self):
        real_flock = runs.fcntl.flock

        def failing_probe(fd, operation):
            if operation & runs.fcntl.LOCK_NB:
                raise OSError(errno.ENOLCK, "No locks available")
            return real_flock(fd, operation)

        with temp_home():
            alive = self.start(os.getpid())
            dead = self.start_without_lock(dead_pid())
            for run_id in (alive, dead):
                runs.hold_worker_lock(run_id).close()
            with patch.object(runs.fcntl, "flock", side_effect=failing_probe):
                held = runs.worker_lock_held(alive)
                alive_status = runs.run_status(alive)["status"]
                dead_status = runs.run_status(dead)["status"]
        self.assertIsNone(held)
        self.assertEqual(alive_status, "running")
        self.assertEqual(dead_status, "interrupted")

    def start_without_lock(self, pid: int) -> str:
        run_id = runs.new_run_id()
        runs.write_run(run_id, {"run_id": run_id, "plan_id": "a" * 16, "status": "running", "pid": pid,
                                "started_at": runs.now()})
        return run_id

    def test_without_a_lock_file_the_pid_probe_decides(self):
        with temp_home():
            run_id = self.start(dead_pid())
            self.assertIsNone(runs.worker_lock_held(run_id))
            self.assertFalse(runs.worker_lock_path(run_id).exists())
            self.assertEqual(runs.run_status(run_id)["status"], "interrupted")

    def test_taking_the_worker_lock_waits_for_the_mutex(self):
        # Probes run under the mutex; taking the lock under it too means a probe never sees
        # a lock file that exists but is not yet locked, and never makes the worker's flock fail.
        with temp_home():
            run_id = self.start(os.getpid())
            handles = []
            with runs._mutex():
                thread = threading.Thread(target=lambda: handles.append(runs.hold_worker_lock(run_id)))
                thread.start()
                thread.join(0.3)
                waiting = thread.is_alive()
                file_before = runs.worker_lock_path(run_id).exists()
            thread.join(5)
            try:
                self.assertTrue(waiting)
                self.assertFalse(file_before)
                self.assertEqual(len(handles), 1)
                self.assertIs(runs.worker_lock_held(run_id), True)
            finally:
                for handle in handles:
                    handle.close()

    @unittest.skipUnless(os.name == "posix" and shutil.which("ssh"), "needs POSIX and an OpenSSH client")
    def test_lock_outlives_a_killed_worker_while_its_real_ssh_runs(self):
        # The child takes the worker lock, runs a real ssh through remote_shell the way the
        # worker does, and SIGKILLs itself while ssh runs. ssh closes inherited descriptors
        # at startup, so only the holder process keeps the lock. ProxyCommand=sleep 3
        # stands in for the connection: no host is contacted.
        child_code = textwrap.dedent("""
            import os, signal, sys, threading, time
            from unittest.mock import patch
            from gentoo_agent.core import runs, ssh
            handle = runs.hold_worker_lock(sys.argv[1])
            command = ["ssh", "-F", "/dev/null", "-o", "ProxyCommand=sleep 3", "-o", "BatchMode=yes",
                       "nohost.invalid"]
            with patch.object(ssh, "ssh_base", lambda target: command):
                threading.Thread(target=ssh.remote_shell, daemon=True, args=({}, "label", "true"),
                                 kwargs={"stream": True, "keep_fds": (handle.fileno(),),
                                         "holder_python": sys.executable}).start()
                time.sleep(0.8)
                os.kill(os.getpid(), signal.SIGKILL)
        """)
        with temp_home() as home:
            run_id = runs.new_run_id()
            env = {**os.environ, "GENTOO_AGENT_HOME": str(home), "PYTHONPATH": str(REPO_ROOT)}
            started = time.monotonic()
            child = subprocess.Popen([sys.executable, "-c", child_code, run_id], env=env, cwd=str(REPO_ROOT))
            self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)
            held_after_kill = runs.worker_lock_held(run_id)
            released = False
            while time.monotonic() - started < 8:
                if runs.worker_lock_held(run_id) is False:
                    released = True
                    break
                time.sleep(0.1)
            elapsed = time.monotonic() - started
        self.assertIs(held_after_kill, True)
        self.assertTrue(released)
        self.assertGreater(elapsed, 2.5)


if __name__ == "__main__":
    unittest.main()
