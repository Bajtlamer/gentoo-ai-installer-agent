import json
import unittest
from pathlib import Path

from gentoo_agent.core.errors import CONFIG_CONFLICT, AgentError
from gentoo_agent.core.validate import validate_installation, validated_settings


ROOT = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "user": "root"}


def fixture(name: str) -> dict:
    data = json.loads((ROOT / "tests" / "golden" / "configs" / f"{name}.json").read_text(encoding="utf-8"))
    data.update({
        "profile": "gentoo-amd64-openrc",
        "swap_mb": 2048,
        "bootloader": "grub",
    })
    return data


def inventory_with(storage: str, boot_mode: str) -> dict:
    return {"results": [
        {"label": "storage", "stdout": storage},
        {"label": "boot_mode", "stdout": boot_mode},
    ]}


class ValidatedSettingsTests(unittest.TestCase):
    def test_valid_configuration_returns_disk_and_layout(self):
        disk, layout = validated_settings(TARGET, fixture("uefi_gpt_user_sudo"))
        self.assertEqual(disk, "/dev/nvme0n1")
        self.assertEqual(len(layout), 3)

    def test_uefi_with_mbr_is_a_conflict(self):
        install = fixture("uefi_gpt_user_sudo")
        install["partition_table"] = "mbr"
        with self.assertRaises(AgentError) as caught:
            validated_settings(TARGET, install)
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)


class ValidateInstallationTests(unittest.TestCase):
    def test_without_inventory_is_valid_with_warning(self):
        result = validate_installation(TARGET, fixture("bios_mbr_user_sudo"), {}, None)
        self.assertTrue(result["valid_for_execution"])
        self.assertIn("no inventory exists; run inspect before review", result["warnings"])

    def test_disk_missing_from_inventory_is_an_error(self):
        inventory = inventory_with("sdb 40G disk", "bios-or-unknown")
        result = validate_installation(TARGET, fixture("bios_mbr_user_sudo"), {}, inventory)
        self.assertFalse(result["valid_for_execution"])
        self.assertIn("configured disk /dev/sda was not found in the latest inventory", result["errors"])

    def test_uefi_plan_on_bios_live_boot_is_an_error(self):
        inventory = inventory_with("/dev/nvme0n1 512G disk", "bios-or-unknown")
        result = validate_installation(TARGET, fixture("uefi_gpt_user_sudo"), {}, inventory)
        self.assertTrue(any("conflicts with the latest live boot mode" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
