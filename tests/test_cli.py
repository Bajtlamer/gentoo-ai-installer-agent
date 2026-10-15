import io
import unittest
from contextlib import redirect_stderr

from gentoo_agent import cli


class InstallCommandTests(unittest.TestCase):
    def test_install_execute_points_to_the_plan_workflow(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.main(["install", "--execute"])
        self.assertEqual(code, 1)
        self.assertIn("error [APPROVAL_REQUIRED]: install --execute was replaced", stderr.getvalue())
        self.assertIn("gentoo-agent plan create", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
