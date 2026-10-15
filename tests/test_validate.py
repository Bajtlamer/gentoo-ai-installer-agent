import json
import unittest
from pathlib import Path

from gentoo_agent.core.errors import CONFIG_CONFLICT, CONFIG_INVALID, AgentError
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


DROP = object()
STATIC = {"address": "192.168.10.20/24", "gateway": "192.168.10.1", "dns": ["1.1.1.1"]}


def changed(name: str, **changes) -> dict:
    install = fixture(name)
    for key, value in changes.items():
        if value is DROP:
            install.pop(key, None)
        else:
            install[key] = value
    return install


def static(**changes) -> dict:
    network = dict(STATIC)
    for key, value in changes.items():
        if value is DROP:
            network.pop(key)
        else:
            network[key] = value
    return changed("uefi_gpt_root_only_static", network_static=network)


INVALID = [
    ("hostname int", changed("uefi_gpt_user_sudo", hostname=5), "hostname"),
    ("hostname list", changed("uefi_gpt_user_sudo", hostname=["server"]), "hostname"),
    ("hostname missing", changed("uefi_gpt_user_sudo", hostname=DROP), "hostname"),
    ("timezone int", changed("uefi_gpt_user_sudo", timezone=5), "timezone"),
    ("timezone missing", changed("uefi_gpt_user_sudo", timezone=DROP), "timezone"),
    ("timezone dotdot", changed("uefi_gpt_user_sudo", timezone="../../etc/shadow"), "timezone"),
    ("timezone inner dotdot", changed("uefi_gpt_user_sudo", timezone="Europe/../../etc/shadow"), "timezone"),
    ("timezone only dotdot", changed("uefi_gpt_user_sudo", timezone=".."), "timezone"),
    ("timezone absolute", changed("uefi_gpt_user_sudo", timezone="/etc/shadow"), "timezone"),
    ("timezone space", changed("uefi_gpt_user_sudo", timezone="UTC x"), "timezone"),
    ("init unknown", changed("uefi_gpt_user_sudo", init_system="runit"), "init_system"),
    ("init missing", changed("uefi_gpt_user_sudo", init_system=DROP), "init_system"),
    ("init list", changed("uefi_gpt_user_sudo", init_system=["openrc"]), "init_system"),
    ("network unknown", changed("uefi_gpt_user_sudo", network_method="ppp"), "network_method"),
    ("network list", changed("uefi_gpt_user_sudo", network_method=["dhcp"]), "network_method"),
    ("static missing", changed("uefi_gpt_root_only_static", network_static=DROP), "network_static"),
    ("static string", changed("uefi_gpt_root_only_static", network_static="10.0.0.2/24"), "network_static"),
    ("static extra key", static(interface="eth0"), "network_static"),
    ("static no dns", static(dns=DROP), "network_static"),
    ("address injection", static(address="10.0.0.2/24 x; touch /PWNED"), "network_static"),
    ("address quote", static(address="10.0.0.2/24'"), "network_static"),
    ("address text", static(address="router"), "network_static"),
    ("address int", static(address=5), "network_static"),
    ("address scope id", static(address="fe80::2%eth0;id/64"), "network_static"),
    ("gateway prefix", static(gateway="10.0.0.1/24"), "network_static"),
    ("gateway bool", static(gateway=True), "network_static"),
    ("gateway scope id", static(gateway="fe80::1%a;touch /PWNED"), "network_static"),
    ("dns string", static(dns="1.1.1.1"), "network_static"),
    ("dns empty", static(dns=[]), "network_static"),
    ("dns four", static(dns=["1.1.1.1", "1.0.0.1", "9.9.9.9", "8.8.8.8"]), "network_static"),
    ("dns int", static(dns=[16843009]), "network_static"),
    ("dns injection", static(dns=["1.1.1.1; touch /PWNED"]), "network_static"),
    ("sudo without user", changed("uefi_gpt_root_only_static", configure_sudo=True), "configure_sudo"),
    ("sudo default without user", changed("uefi_gpt_root_only_static", configure_sudo=DROP), "configure_sudo"),
    ("swap bool", changed("uefi_gpt_user_sudo", swap_mb=True), "swap_mb"),
    ("swap string", changed("uefi_gpt_user_sudo", swap_mb="2048"), "swap_mb"),
    ("swap float", changed("uefi_gpt_user_sudo", swap_mb=2048.0), "swap_mb"),
    ("swap negative", changed("uefi_gpt_user_sudo", swap_mb=-1), "swap_mb"),
    ("swap huge", changed("uefi_gpt_user_sudo", swap_mb=131073), "swap_mb"),
    ("root btrfs", changed("uefi_gpt_user_sudo", root_filesystem="btrfs"), "root_filesystem"),
]

VALID = [
    ("swap zero", changed("uefi_gpt_user_sudo", swap_mb=0)),
    ("swap max", changed("uefi_gpt_user_sudo", swap_mb=131072)),
    ("systemd", changed("uefi_gpt_user_sudo", init_system="systemd")),
    ("nested timezone", changed("uefi_gpt_user_sudo", timezone="America/Argentina/Buenos_Aires")),
    ("etc timezone", changed("uefi_gpt_user_sudo", timezone="Etc/GMT+5")),
    ("static ipv4", static()),
    ("static three dns", static(dns=["1.1.1.1", "9.9.9.9", "2606:4700:4700::1111"])),
    ("static ipv6", static(address="2001:db8::2/64", gateway="2001:db8::1", dns=["2001:db8::53"])),
]


class SettingValidationTests(unittest.TestCase):
    def test_invalid_settings_are_rejected(self):
        for name, install, field in INVALID:
            with self.subTest(name):
                with self.assertRaises(AgentError) as caught:
                    validated_settings(TARGET, install)
                self.assertEqual(caught.exception.code, CONFIG_INVALID)
                self.assertEqual(caught.exception.details.get("field"), field)

    def test_valid_settings_are_accepted(self):
        for name, install in VALID:
            with self.subTest(name):
                validated_settings(TARGET, install)


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
