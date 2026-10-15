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


if __name__ == "__main__":
    unittest.main()
