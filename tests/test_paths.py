import unittest

from gentoo_agent.core import paths
from tests.helpers import temp_home


class PathsTests(unittest.TestCase):
    def test_root_follows_the_environment_at_call_time(self):
        with temp_home() as first:
            self.assertEqual(paths.root(), first)
            with temp_home() as second:
                self.assertEqual(paths.root(), second)

    def test_directories_live_under_root(self):
        with temp_home() as home:
            self.assertEqual(paths.config_dir(), home / "config")
            self.assertEqual(paths.state_dir(), home / "state")
            self.assertEqual(paths.inventory_dir(), home / "inventory")
            self.assertEqual(paths.log_dir(), home / "logs")
            self.assertEqual(paths.approval_dir(), home / "approvals")
            self.assertEqual(paths.plans_dir(), home / "plans")
            self.assertEqual(paths.runs_dir(), home / "runs")


if __name__ == "__main__":
    unittest.main()
