import unittest

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.layout import build_layout, partition_device


class LayoutTests(unittest.TestCase):
    def test_partition_device_names(self):
        self.assertEqual(partition_device("/dev/sda", 2), "/dev/sda2")
        self.assertEqual(partition_device("/dev/nvme0n1", 1), "/dev/nvme0n1p1")

    def test_uefi_layout(self):
        table, layout = build_layout("/dev/nvme0n1", "uefi", 4096)
        self.assertEqual(table, "gpt")
        self.assertEqual(layout, [
            {"device": "/dev/nvme0n1p1", "size_mib": 512, "type": "efi-system", "filesystem": "vfat", "mount": "/boot"},
            {"device": "/dev/nvme0n1p2", "size_mib": 4096, "type": "linux-swap", "mount": None},
            {"device": "/dev/nvme0n1p3", "size": "remaining", "type": "linux-root", "filesystem": "ext4", "mount": "/"},
        ])

    def test_bios_layout(self):
        table, layout = build_layout("/dev/sda", "bios", 2048)
        self.assertEqual(table, "mbr")
        self.assertEqual([item["device"] for item in layout], ["/dev/sda1", "/dev/sda2"])
        self.assertEqual(layout[0]["type"], "linux-swap")

    def test_invalid_inputs(self):
        for args in (("/dev/sda", "efi", 1024), ("/dev/sda", "bios", -1), ("/dev/sda", "bios", True)):
            with self.subTest(args=args), self.assertRaises(AgentError) as caught:
                build_layout(*args)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)


if __name__ == "__main__":
    unittest.main()
