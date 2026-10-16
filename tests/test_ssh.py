import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

from gentoo_agent.core import ssh
from tests.helpers import temp_home


TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root"}


class RemoteShellKeepFdsTests(unittest.TestCase):
    def test_stream_passes_kept_descriptors_to_the_ssh_process(self):
        with temp_home(), patch.object(subprocess, "Popen", side_effect=RuntimeError("stop")) as popen:
            with self.assertRaises(RuntimeError):
                ssh.remote_shell(TARGET, "label", "true", stream=True, keep_fds=(7,), holder_python=sys.executable)
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (7,))

    def test_non_stream_passes_kept_descriptors_to_the_holder_process(self):
        with temp_home(), patch.object(subprocess, "Popen", side_effect=RuntimeError("stop")) as popen:
            with self.assertRaises(RuntimeError):
                ssh.remote_shell(TARGET, "label", "true", keep_fds=(7,), holder_python=sys.executable)
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (7,))

    def test_kept_descriptors_go_to_an_isolated_holder_in_its_own_session(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                with temp_home(), patch.object(subprocess, "Popen", side_effect=RuntimeError("stop")) as popen:
                    with self.assertRaises(RuntimeError):
                        ssh.remote_shell(TARGET, "label", "true", stream=stream, keep_fds=(7,), holder_python=sys.executable)
                argv = popen.call_args.args[0]
                self.assertEqual(argv[:3], [sys.executable, "-I", "-c"])
                self.assertEqual(argv[3], ssh.HOLDER)
                self.assertEqual(argv[4:], ssh.ssh_base(TARGET) + ["--", "sh -c true"])
                self.assertIs(popen.call_args.kwargs["start_new_session"], True)

    def test_kept_descriptors_require_an_interpreter_for_the_holder(self):
        with temp_home(), patch.object(subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                ssh.remote_shell(TARGET, "label", "true", stream=True, keep_fds=(7,))
        popen.assert_not_called()

    def test_without_kept_descriptors_ssh_runs_directly(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with temp_home(), patch.object(subprocess, "run", return_value=completed) as run:
            ssh.remote_shell(TARGET, "label", "true")
        self.assertEqual(run.call_args.args[0], ssh.ssh_base(TARGET) + ["--", "sh -c true"])
        self.assertNotIn("start_new_session", run.call_args.kwargs)

    def test_no_descriptors_are_kept_by_default(self):
        with temp_home(), patch.object(subprocess, "Popen", side_effect=RuntimeError("stop")) as popen:
            with self.assertRaises(RuntimeError):
                ssh.remote_shell(TARGET, "label", "true", stream=True)
        self.assertEqual(popen.call_args.kwargs.get("pass_fds", ()), ())



FAKE_SSH = ["sh", "-c", "cat; echo done; exit 7", "fake-ssh"]


@unittest.skipUnless(os.name == "posix", "process groups are POSIX only")
class HolderProcessTests(unittest.TestCase):
    def setUp(self):
        self.lock = open(os.devnull, "rb")
        self.addCleanup(self.lock.close)

    def test_returncode_stdin_and_output_pass_through_the_holder(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                seen = []
                with temp_home(), patch.object(ssh, "ssh_base", lambda target: list(FAKE_SSH)):
                    result = ssh.remote_shell(TARGET, "label", "true", stream=stream, input_data="hello\n",
                                              on_output=seen.append, keep_fds=(self.lock.fileno(),), holder_python=sys.executable)
                self.assertEqual(result["returncode"], 7)
                self.assertEqual(result["stdout"], "hello\ndone\n")
                if stream:
                    self.assertEqual(seen, ["hello\n", "done\n"])

    def test_holder_reports_an_ssh_that_cannot_start_and_exits_255(self):
        missing = ["/nonexistent/gentoo-agent-test/ssh"]
        for stream in (True, False):
            with self.subTest(stream=stream):
                with temp_home(), patch.object(ssh, "ssh_base", lambda target: list(missing)):
                    result = ssh.remote_shell(TARGET, "label", "true", stream=stream,
                                              keep_fds=(self.lock.fileno(),), holder_python=sys.executable)
                self.assertEqual(result["returncode"], 255)
                text = result["stdout"] if stream else result["stderr"]
                lines = text.splitlines()
                self.assertEqual(len(lines), 1, text)
                self.assertTrue(lines[0].startswith("gentoo-agent holder: cannot run ssh: "), text)
                self.assertIn("/nonexistent/gentoo-agent-test/ssh", lines[0])

    def test_signal_death_of_ssh_passes_through_the_holder(self):
        with temp_home(), patch.object(ssh, "ssh_base", lambda target: ["sh", "-c", "kill -TERM $$", "x"]):
            result = ssh.remote_shell(TARGET, "label", "true", stream=True, keep_fds=(self.lock.fileno(),), holder_python=sys.executable)
        self.assertEqual(result["returncode"], -15)

    @unittest.skipUnless(shutil.which("ssh") and shutil.which("pgrep"), "needs an OpenSSH client and pgrep")
    def test_timeout_kills_the_whole_group_and_leaves_no_ssh_behind(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                marker = f"gentoo-agent-holder-test-{uuid.uuid4().hex}"
                command = ["ssh", "-F", "/dev/null", "-o", f"ProxyCommand=sh -c 'sleep 30; echo {marker}'",
                           "-o", "BatchMode=yes", "nohost.invalid"]

                def leftovers() -> str:
                    return subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.strip()

                with temp_home(), patch.object(ssh, "ssh_base", lambda target: command):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        ssh.remote_shell(TARGET, "label", "true", timeout=1, stream=stream,
                                         keep_fds=(self.lock.fileno(),), holder_python=sys.executable)
                deadline = time.monotonic() + 3
                while leftovers() and time.monotonic() < deadline:
                    time.sleep(0.1)
                found = leftovers()
                if found:
                    subprocess.run(["pkill", "-KILL", "-f", marker], check=False)
                self.assertEqual(found, "")

    @unittest.skipUnless(shutil.which("ssh") and shutil.which("pgrep"), "needs an OpenSSH client and pgrep")
    def test_an_exception_while_ssh_runs_kills_the_whole_group(self):
        # ssh -v prints debug lines at once while the ProxyCommand still sleeps; the callback raises on the first.
        marker = f"gentoo-agent-holder-test-{uuid.uuid4().hex}"
        command = ["ssh", "-v", "-F", "/dev/null", "-o", f"ProxyCommand=sh -c 'sleep 30; echo {marker}'",
                   "-o", "BatchMode=yes", "nohost.invalid"]

        def leftovers() -> str:
            return subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.strip()

        def explode(line):
            raise RuntimeError("callback failed")

        with temp_home(), patch.object(ssh, "ssh_base", lambda target: command):
            with self.assertRaises(RuntimeError):
                ssh.remote_shell(TARGET, "label", "true", stream=True, on_output=explode,
                                 keep_fds=(self.lock.fileno(),), holder_python=sys.executable)
        deadline = time.monotonic() + 3
        while leftovers() and time.monotonic() < deadline:
            time.sleep(0.1)
        found = leftovers()
        if found:
            subprocess.run(["pkill", "-KILL", "-f", marker], check=False)
        self.assertEqual(found, "")

    def test_an_exception_while_ssh_runs_kills_ssh_without_a_holder(self):
        # Without a holder there is no process group; ssh itself is killed.
        started = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            started.append(process)
            return process

        def explode(line):
            raise RuntimeError("callback failed")

        with temp_home(), patch.object(ssh, "ssh_base", lambda target: ["sh", "-c", "echo start; exec sleep 30", "x"]), \
                patch.object(subprocess, "Popen", side_effect=recording_popen):
            with self.assertRaises(RuntimeError):
                ssh.remote_shell(TARGET, "label", "true", stream=True, on_output=explode)
        for process in started:
            self.addCleanup(lambda process=process: process.poll() is None and process.kill())
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll())


if __name__ == "__main__":
    unittest.main()
