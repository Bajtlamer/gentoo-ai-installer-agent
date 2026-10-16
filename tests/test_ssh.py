import subprocess
import unittest
from unittest.mock import patch

from gentoo_agent.core import ssh
from tests.helpers import temp_home


TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root"}


class RemoteShellKeepFdsTests(unittest.TestCase):
    def test_stream_passes_kept_descriptors_to_the_ssh_process(self):
        with temp_home(), patch.object(subprocess, "Popen", side_effect=RuntimeError("stop")) as popen:
            with self.assertRaises(RuntimeError):
                ssh.remote_shell(TARGET, "label", "true", stream=True, keep_fds=(7,))
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (7,))

    def test_non_stream_passes_kept_descriptors_to_the_ssh_process(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with temp_home(), patch.object(subprocess, "run", return_value=completed) as run:
            ssh.remote_shell(TARGET, "label", "true", keep_fds=(7,))
        self.assertEqual(run.call_args.kwargs["pass_fds"], (7,))

    def test_no_descriptors_are_kept_by_default(self):
        with temp_home(), patch.object(subprocess, "Popen", side_effect=RuntimeError("stop")) as popen:
            with self.assertRaises(RuntimeError):
                ssh.remote_shell(TARGET, "label", "true", stream=True)
        self.assertEqual(popen.call_args.kwargs.get("pass_fds", ()), ())


if __name__ == "__main__":
    unittest.main()
