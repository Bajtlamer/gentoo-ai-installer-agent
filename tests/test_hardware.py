import json
import unittest

from gentoo_agent.core.hardware import summarize_inventory


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


if __name__ == "__main__":
    unittest.main()
