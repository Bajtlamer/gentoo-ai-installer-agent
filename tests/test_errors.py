import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError


class AgentErrorTests(unittest.TestCase):
    def test_error_carries_code_message_and_details(self):
        error = AgentError(CONFIG_INVALID, "disk path is unsafe", {"disk": "/dev/x;reboot"})
        self.assertEqual(error.code, "CONFIG_INVALID")
        self.assertEqual(str(error), "disk path is unsafe")
        self.assertEqual(
            error.to_dict(),
            {"code": "CONFIG_INVALID", "message": "disk path is unsafe", "details": {"disk": "/dev/x;reboot"}},
        )

    def test_details_default_to_empty_dict(self):
        self.assertEqual(AgentError(CONFIG_INVALID, "x").details, {})

    def test_error_survives_pickle_round_trip(self):
        import pickle

        error = AgentError(CONFIG_INVALID, "disk path is unsafe", {"disk": "/dev/x"})
        restored = pickle.loads(pickle.dumps(error))
        self.assertIsInstance(restored, AgentError)
        self.assertEqual(restored.to_dict(), error.to_dict())
        self.assertEqual(str(restored), "disk path is unsafe")

    def test_cli_reports_agent_error_with_code_and_exit_1(self):
        from gentoo_agent import cli

        def failing(_):
            raise AgentError(CONFIG_INVALID, "broken config")

        stderr = io.StringIO()
        with patch.object(cli, "cmd_status", failing), redirect_stderr(stderr):
            code = cli.main(["status"])
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "error [CONFIG_INVALID]: broken config\n")
