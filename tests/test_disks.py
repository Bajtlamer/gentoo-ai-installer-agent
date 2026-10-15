import json
import unittest
from unittest.mock import patch

from gentoo_agent.core import disks
from gentoo_agent.core.errors import CONFIG_INVALID, INVENTORY_MISSING, SSH_UNREACHABLE, TARGET_DRIFT, AgentError
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS


LSBLK = json.dumps({"blockdevices": [
    {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK  ", "serial": "QM00001", "wwn": None},
    {"path": "/dev/sr0", "type": "rom", "size": 1073741312, "model": "QEMU DVD-ROM", "serial": "QM00003", "wwn": None},
    {"path": "/dev/vda", "type": "disk", "size": "21474836480", "model": None, "serial": None, "wwn": None},
]})
TARGET = {"name": "gentoo", "host": "192.0.2.10", "user": "root"}


def inventory_with(stdout: str, returncode: int = 0) -> dict:
    return {"results": [{"label": "disk_identity", "returncode": returncode, "stdout": stdout}]}


class DiskIdentityTests(unittest.TestCase):
    def test_inventory_has_a_disk_identity_command(self):
        self.assertIn("SERIAL", READ_ONLY_COMMANDS["disk_identity"])
        self.assertIn("WWN", READ_ONLY_COMMANDS["disk_identity"])

    def test_parse_keeps_disks_only_and_normalizes(self):
        parsed = disks.parse_disk_identities(LSBLK)
        self.assertEqual(parsed, [
            {"path": "/dev/sda", "size_bytes": 42949672960, "model": "QEMU HARDDISK", "serial": "QM00001", "wwn": ""},
            {"path": "/dev/vda", "size_bytes": 21474836480, "model": "", "serial": "", "wwn": ""},
        ])
        self.assertEqual(disks.identity_strength(parsed[0]), "strong")
        self.assertEqual(disks.identity_strength(parsed[1]), "weak")

    def test_inventory_without_identity_is_missing(self):
        with self.assertRaises(AgentError) as caught:
            disks.disk_from_inventory({"results": []}, "/dev/sda")
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_unknown_disk_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            disks.disk_from_inventory(inventory_with(LSBLK), "/dev/sdz")
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_check_drift_accepts_the_same_disk_and_rejects_a_different_one(self):
        expected = disks.disk_from_inventory(inventory_with(LSBLK), "/dev/sda")
        same = {"label": "disk_identity", "returncode": 0, "stdout": LSBLK, "stderr": ""}
        with patch.object(disks, "run_ssh_readonly", return_value=same):
            disks.check_drift(TARGET, [expected])
        swapped = LSBLK.replace("QM00001", "QM99999")
        with patch.object(disks, "run_ssh_readonly", return_value={**same, "stdout": swapped}):
            with self.assertRaises(AgentError) as caught:
                disks.check_drift(TARGET, [expected])
        self.assertEqual(caught.exception.code, TARGET_DRIFT)
        self.assertEqual(caught.exception.details["actual"]["serial"], "QM99999")

    def test_unreachable_target_is_reported(self):
        failed = {"label": "disk_identity", "returncode": 255, "stdout": "", "stderr": "ssh: connect to host"}
        with patch.object(disks, "run_ssh_readonly", return_value=failed):
            with self.assertRaises(AgentError) as caught:
                disks.read_live_identity(TARGET, "/dev/sda")
        self.assertEqual(caught.exception.code, SSH_UNREACHABLE)


if __name__ == "__main__":
    unittest.main()
