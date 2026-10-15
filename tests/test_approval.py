import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import approval
from gentoo_agent.core.config import load_json


class ApprovalTests(unittest.TestCase):
    def test_phrase_names_target_and_disk(self):
        self.assertEqual(
            approval.approval_phrase({"name": "gentoo"}, {"disk": "/dev/sda"}),
            "APPROVE INSTALL gentoo ON /dev/sda",
        )

    def test_enable_execution_sets_all_three_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(approval, "CONFIG", Path(tmp)):
            approval.enable_execution({"name": "gentoo"}, {"disk": "/dev/sda"}, {"phases": []})
            target = load_json(Path(tmp) / "target.json")
            install = load_json(Path(tmp) / "install.json")
            plan = load_json(Path(tmp) / "plan.json")
        self.assertTrue(target["execution_enabled"])
        self.assertTrue(install["execution_enabled"])
        self.assertEqual(install["review_status"], "approved_for_execution")
        self.assertTrue(plan["execution_enabled"])
        self.assertEqual(plan["review_status"], "approved_for_execution")


if __name__ == "__main__":
    unittest.main()
