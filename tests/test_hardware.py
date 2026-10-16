import json
import unittest

from gentoo_agent.core.hardware import summarize_inventory
from tests.helpers import (
    BASIC_DATA_TYPE,
    ESP_TYPE,
    identity_result,
    partition,
    partitions_result,
    secure_boot_result,
    windows_bios_disk,
    windows_disk,
)


class HardwareSummaryTests(unittest.TestCase):
    def test_summary(self):
        disks = [{"path": "/dev/sda", "type": "disk", "size": 100, "model": "QEMU", "serial": "", "wwn": None}]
        inventory = {"target": "gentoo", "collected_at": "2026-10-15T12:00:00+00:00", "results": [
            {"label": "boot_mode", "returncode": 0, "stdout": "uefi"},
            {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": disks})},
        ]}
        summary = summarize_inventory(inventory)
        self.assertEqual(summary["boot_mode"], "uefi")
        self.assertEqual(summary["collected_at"], "2026-10-15T12:00:00+00:00")
        self.assertEqual(summary["disks"][0]["strength"], "weak")
        self.assertEqual(len(summary["inventory_hash"]), 64)

    def test_boot_mode_needs_clean_evidence(self):
        cases = [
            ([], "unknown"),
            ([{"label": "boot_mode", "returncode": 255, "stdout": ""}], "unknown"),
            ([{"label": "boot_mode", "returncode": 255, "stdout": "uefi"}], "unknown"),
            ([{"label": "boot_mode", "error": "timed out"}], "unknown"),
            ([{"label": "boot_mode", "returncode": 0, "stdout": ""}], "unknown"),
            ([{"label": "boot_mode", "returncode": 0, "stdout": "something"}], "unknown"),
            ([{"label": "boot_mode", "returncode": 0, "stdout": None}], "unknown"),
            ([{"label": "boot_mode", "returncode": 0, "stdout": "uefi"}], "uefi"),
            ([{"label": "boot_mode", "returncode": 0, "stdout": "bios-or-unknown"}], "bios"),
        ]
        for results, expected in cases:
            with self.subTest(results=results):
                self.assertEqual(summarize_inventory({"results": results})["boot_mode"], expected)


    def test_architecture_comes_from_a_supported_machine_only(self):
        cases = [
            ([identity_result("aarch64")], "arm64"),
            ([identity_result("x86_64")], "amd64"),
            ([], "unknown"),
            ([identity_result("riscv64")], "unknown"),
            ([{**identity_result("aarch64"), "returncode": 255}], "unknown"),
        ]
        for results, expected in cases:
            with self.subTest(results=results):
                self.assertEqual(summarize_inventory({"results": results})["architecture"], expected)

    def test_existing_os_and_secure_boot_are_reported(self):
        disks = [{"path": path, "type": "disk", "size": 100, "model": "QEMU", "serial": "s", "wwn": None}
                 for path in ("/dev/nvme0n1", "/dev/sda", "/dev/sdb")]
        results = [
            {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": disks})},
            partitions_result("/dev/nvme0n1", windows_disk("/dev/sda"), windows_bios_disk("/dev/sdb")),
            secure_boot_result("0"),
        ]
        summary = summarize_inventory({"results": results})
        self.assertEqual(summary["secure_boot"], "off")
        self.assertEqual([disk["existing_os"] for disk in summary["disks"]], [None, "windows", "windows-bios"])

    def test_windows_data_is_reported_for_a_split_install(self):
        disks = [{"path": path, "type": "disk", "size": 100, "model": "QEMU", "serial": "s", "wwn": None}
                 for path in ("/dev/sda", "/dev/sdb", "/dev/sdc")]
        results = [
            {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": disks})},
            partitions_result(
                {"path": "/dev/sda", "type": "disk", "children": [partition("/dev/sda", 1, BASIC_DATA_TYPE, "ntfs")]},
                {"path": "/dev/sdb", "type": "disk", "children": [partition("/dev/sdb", 1, ESP_TYPE, "vfat", "1111-2222")]},
                "/dev/sdc"),
        ]
        summary = summarize_inventory({"results": results})
        self.assertEqual([disk["existing_os"] for disk in summary["disks"]], ["windows-data", "windows-data", None])

    def test_existing_os_is_unknown_without_a_usable_partition_list(self):
        disks = [{"path": "/dev/sda", "type": "disk", "size": 100, "model": "QEMU", "serial": "s", "wwn": None}]
        identity = {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": disks})}
        hostile = windows_disk("/dev/sda", esp_uuid="ABCD-1234\nDisk erase: no")
        bare = {"path": "/dev/sda", "type": "disk", "children": [partition("/dev/sda", 1, None)]}
        for extra in ([], [partitions_result(hostile)], [partitions_result(bare)]):
            with self.subTest(extra=extra):
                summary = summarize_inventory({"results": [identity, *extra]})
                self.assertEqual(summary["disks"][0]["existing_os"], "unknown")
                self.assertEqual(summary["secure_boot"], "unknown")


if __name__ == "__main__":
    unittest.main()
