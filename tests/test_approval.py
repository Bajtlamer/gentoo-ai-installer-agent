import datetime as dt
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import approval, install_plan, phases
from gentoo_agent.core.config import save_json
from gentoo_agent.core.errors import (
    APPROVAL_EXPIRED,
    APPROVAL_REQUIRED,
    CONFIG_INVALID,
    PLAN_HASH_MISMATCH,
    AgentError,
)
from tests.helpers import temp_home


REPO = Path(__file__).resolve().parents[1]
TARGET = {
    "name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
    "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True,
}
START = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)


def make_plan(serial: str = "QM00001") -> dict:
    install = json.loads((REPO / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))
    install.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True})
    project = json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8"))
    stdout = json.dumps({"blockdevices": [
        {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK", "serial": serial, "wwn": None},
    ]})
    inventory = {"results": [{"label": "disk_identity", "returncode": 0, "stdout": stdout}]}
    with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAA test"):
        return install_plan.save_install_plan(install_plan.create_install_plan(TARGET, install, project, inventory))


class ApprovalTests(unittest.TestCase):
    def test_summary_is_built_from_the_plan(self):
        with temp_home():
            plan = make_plan()
            summary = approval.approval_summary(plan)
            weak = approval.approval_summary(make_plan(serial=""))
        self.assertIn(plan["plan_hash"], summary)
        self.assertIn("serial=QM00001", summary)
        self.assertIn("root@192.0.2.10:22", summary)
        self.assertIn("Destructive phases: storage, filesystems, base-system", summary)
        self.assertIn("all data on the disk will be destroyed", summary)
        self.assertNotIn("WARNING", summary)
        self.assertIn("WARNING: this disk has no serial number or WWN", weak)

    def test_unapproved_plan_is_refused(self):
        with temp_home():
            plan = make_plan()
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)
        self.assertIn(f"gentoo-agent approve {plan['plan_id']}", caught.exception.message)

    def test_recorded_approval_allows_the_plan_until_expiry(self):
        with temp_home():
            plan = make_plan()
            record = approval.record_approval(plan, "cli", at=START)
            self.assertEqual(record["expires_at"], "2026-10-15T13:00:00+00:00")
            loaded = approval.require_approved_plan(plan["plan_id"], at=START + dt.timedelta(minutes=59))
            self.assertEqual(loaded["plan_hash"], plan["plan_hash"])
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START + dt.timedelta(minutes=60))
        self.assertEqual(caught.exception.code, APPROVAL_EXPIRED)

    def test_approval_for_another_hash_is_rejected(self):
        with temp_home():
            plan = make_plan()
            record = approval.record_approval(plan, "cli", at=START)
            save_json(approval.approval_path(plan["plan_id"]), {**record, "plan_hash": "0" * 64})
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_unknown_channel_is_invalid(self):
        with temp_home():
            plan = make_plan()
            with self.assertRaises(AgentError) as caught:
                approval.record_approval(plan, "model", at=START)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_phrase_names_the_plan(self):
        self.assertEqual(approval.approval_phrase({"plan_id": "abc"}), "APPROVE abc")

    def test_status_reports_both_states(self):
        with temp_home():
            plan = make_plan()
            self.assertEqual(approval.approval_status(plan["plan_id"], at=START)["code"], APPROVAL_REQUIRED)
            approval.record_approval(plan, "elicitation", at=START)
            status = approval.approval_status(plan["plan_id"], at=START)
        self.assertEqual(status, {"approved": True, "channel": "elicitation", "expires_at": "2026-10-15T13:00:00+00:00"})


if __name__ == "__main__":
    unittest.main()
