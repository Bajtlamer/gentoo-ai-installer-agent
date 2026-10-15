import json
import unittest
from unittest.mock import patch

from gentoo_agent.core import disks
from gentoo_agent.core.errors import CONFIG_INVALID, DISK_AMBIGUOUS, INVENTORY_MISSING, SSH_UNREACHABLE, TARGET_DRIFT, AgentError
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
        self.assertEqual(READ_ONLY_COMMANDS["disk_identity"],
                         "lsblk -J -b -d -e7 -o PATH,TYPE,SIZE,MODEL,SERIAL,WWN,RM,TRAN,RO")

    def test_parse_ignores_eligibility_fields(self):
        extended = json.dumps({"blockdevices": [
            {"path": "/dev/sda", "type": "disk", "size": 100, "model": "M", "serial": "S1", "wwn": None,
             "rm": True, "tran": "usb", "ro": False},
        ]})
        self.assertEqual(disks.parse_disk_identities(extended), [
            {"path": "/dev/sda", "size_bytes": 100, "model": "M", "serial": "S1", "wwn": ""},
        ])

    def test_disk_candidates_excludes_removable_usb_readonly_and_mounted(self):
        identity = json.dumps({"blockdevices": [
            {"path": "/dev/sda", "type": "disk", "size": 100, "model": "M", "serial": "S1", "wwn": None},
            {"path": "/dev/sdb", "type": "disk", "size": 100, "model": "M", "serial": "S2", "wwn": None,
             "rm": "1", "tran": "sata", "ro": "0"},
            {"path": "/dev/sdc", "type": "disk", "size": 100, "model": "M", "serial": "S3", "wwn": None,
             "rm": False, "tran": "usb", "ro": False},
            {"path": "/dev/sdd", "type": "disk", "size": 100, "model": "M", "serial": "S4", "wwn": None, "ro": 1},
            {"path": "/dev/sde", "type": "disk", "size": 100, "model": "M", "serial": "S5", "wwn": None,
             "rm": 0, "tran": "nvme", "ro": "0"},
            {"path": "/dev/sr0", "type": "rom", "size": 100, "model": "DVD", "serial": "S6", "wwn": None},
        ]})
        storage = json.dumps({"blockdevices": [
            {"name": "sda", "path": "/dev/sda", "type": "disk", "mountpoints": [None]},
            {"name": "sde", "path": "/dev/sde", "type": "disk", "mountpoints": [None], "children": [
                {"name": "sde1", "path": "/dev/sde1", "type": "part", "mountpoints": [None]},
                {"name": "sde2", "path": "/dev/sde2", "type": "part", "mountpoints": ["/run/media/live"]},
            ]},
        ]})
        inventory = {"results": [
            {"label": "disk_identity", "returncode": 0, "stdout": identity},
            {"label": "storage_json", "returncode": 0, "stdout": storage},
        ]}
        eligible, excluded = disks.disk_candidates(inventory)
        self.assertEqual(eligible, [{"path": "/dev/sda", "size_bytes": 100, "model": "M", "serial": "S1", "wwn": ""}])
        reasons = {item["path"]: item["reason"] for item in excluded}
        self.assertEqual(sorted(reasons), ["/dev/sdb", "/dev/sdc", "/dev/sdd", "/dev/sde"])
        self.assertIn("removable", reasons["/dev/sdb"])
        self.assertIn("usb", reasons["/dev/sdc"])
        self.assertIn("read-only", reasons["/dev/sdd"])
        self.assertIn("/run/media/live", reasons["/dev/sde"])

    def test_disk_candidates_without_storage_json_treats_disks_as_unmounted(self):
        eligible, excluded = disks.disk_candidates(inventory_with(LSBLK))
        self.assertEqual([disk["path"] for disk in eligible], ["/dev/sda", "/dev/vda"])
        self.assertEqual(excluded, [])

    def test_disk_candidates_rejects_unreadable_mount_state(self):
        for item in ({"label": "storage_json", "returncode": 1, "stdout": ""},
                     {"label": "storage_json", "returncode": 0, "stdout": "not json"},
                     {"label": "storage_json", "returncode": 0, "stdout": "[]"}):
            with self.subTest(item=item):
                inventory = inventory_with(LSBLK)
                inventory["results"].append(item)
                with self.assertRaises(AgentError) as caught:
                    disks.disk_candidates(inventory)
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)

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

    def test_ssh_launch_failure_is_reported_as_unreachable(self):
        with patch.object(disks, "run_ssh_readonly", side_effect=FileNotFoundError("ssh")):
            with self.assertRaises(AgentError) as caught:
                disks.read_live_identity(TARGET, "/dev/sda")
        self.assertEqual(caught.exception.code, SSH_UNREACHABLE)

    def test_weak_twins_are_ambiguous(self):
        twins = json.dumps({"blockdevices": [
            {"path": "/dev/vda", "type": "disk", "size": 100, "model": "VIRT", "serial": None, "wwn": None},
            {"path": "/dev/vdb", "type": "disk", "size": 100, "model": "VIRT", "serial": None, "wwn": None},
        ]})
        with self.assertRaises(AgentError) as caught:
            disks.disk_from_inventory(inventory_with(twins), "/dev/vda")
        self.assertEqual(caught.exception.code, DISK_AMBIGUOUS)
        self.assertEqual(caught.exception.details["twins"], ["/dev/vdb"])

    def test_weak_disk_with_different_sized_neighbour_is_accepted(self):
        mixed = json.dumps({"blockdevices": [
            {"path": "/dev/vda", "type": "disk", "size": 100, "model": "VIRT", "serial": None, "wwn": None},
            {"path": "/dev/vdb", "type": "disk", "size": 200, "model": "VIRT", "serial": None, "wwn": None},
        ]})
        self.assertEqual(disks.disk_from_inventory(inventory_with(mixed), "/dev/vda")["path"], "/dev/vda")

    def test_strong_disk_with_same_size_neighbour_is_accepted(self):
        strong = json.dumps({"blockdevices": [
            {"path": "/dev/sda", "type": "disk", "size": 100, "model": "M", "serial": "S1", "wwn": None},
            {"path": "/dev/sdb", "type": "disk", "size": 100, "model": "M", "serial": "S2", "wwn": None},
        ]})
        self.assertEqual(disks.disk_from_inventory(inventory_with(strong), "/dev/sda")["serial"], "S1")


if __name__ == "__main__":
    unittest.main()
