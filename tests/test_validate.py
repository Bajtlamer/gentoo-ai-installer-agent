import json
import unittest
from pathlib import Path

from gentoo_agent.core.errors import CONFIG_CONFLICT, CONFIG_INVALID, AgentError
from gentoo_agent.core.validate import validate_installation, validated_settings
from tests.helpers import identity_result, storage_result


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
STATIC = {"interface": "enp1s0", "address": "192.168.10.20/24", "gateway": "192.168.10.1", "dns": ["1.1.1.1"]}


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
    ("architecture missing", changed("uefi_gpt_user_sudo", architecture=DROP), "architecture"),
    ("architecture unknown", changed("uefi_gpt_user_sudo", architecture="x86"), "architecture"),
    ("init list", changed("uefi_gpt_user_sudo", init_system=["openrc"]), "init_system"),
    ("network unknown", changed("uefi_gpt_user_sudo", network_method="ppp"), "network_method"),
    ("network list", changed("uefi_gpt_user_sudo", network_method=["dhcp"]), "network_method"),
    ("static missing", changed("uefi_gpt_root_only_static", network_static=DROP), "network_static"),
    ("static string", changed("uefi_gpt_root_only_static", network_static="10.0.0.2/24"), "network_static"),
    ("static extra key", static(extra="x"), "network_static"),
    ("interface missing", static(interface=DROP), "network_static"),
    ("interface with space", static(interface="enp1s0 x"), "network_static"),
    ("interface injection", static(interface="eth0;reboot"), "network_static"),
    ("interface too long", static(interface="a" * 16), "network_static"),
    ("interface not a string", static(interface=1), "network_static"),
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
    ("address bare", static(address="192.168.10.20"), "network_static"),
    ("address ipv6", static(address="2001:db8::2/64", gateway="2001:db8::1"), "network_static"),
    ("address prefix 7", static(address="10.0.0.2/7", gateway="10.0.0.1"), "network_static"),
    ("address prefix 31", static(address="192.168.10.20/31", gateway="192.168.10.21"), "network_static"),
    ("address prefix 32", static(address="192.168.10.20/32", gateway="192.168.10.20"), "network_static"),
    ("gateway ipv6", static(gateway="2001:db8::1"), "network_static"),
    ("gateway outside subnet", static(gateway="192.168.11.1"), "network_static"),
    ("sudo without user", changed("uefi_gpt_root_only_static", configure_sudo=True), "configure_sudo"),
    ("sudo default without user", changed("uefi_gpt_root_only_static", configure_sudo=DROP), "configure_sudo"),
    ("swap bool", changed("uefi_gpt_user_sudo", swap_mb=True), "swap_mb"),
    ("swap string", changed("uefi_gpt_user_sudo", swap_mb="2048"), "swap_mb"),
    ("swap float", changed("uefi_gpt_user_sudo", swap_mb=2048.0), "swap_mb"),
    ("swap negative", changed("uefi_gpt_user_sudo", swap_mb=-1), "swap_mb"),
    ("swap huge", changed("uefi_gpt_user_sudo", swap_mb=131073), "swap_mb"),
    ("root btrfs", changed("uefi_gpt_user_sudo", root_filesystem="btrfs"), "root_filesystem"),
    ("systemd", changed("uefi_gpt_user_sudo", init_system="systemd"), "init_system"),
    ("size string", changed("uefi_gpt_user_sudo", expected_disk_size_gib="40"), "expected_disk_size_gib"),
    ("size bool", changed("uefi_gpt_user_sudo", expected_disk_size_gib=True), "expected_disk_size_gib"),
    ("size zero", changed("uefi_gpt_user_sudo", expected_disk_size_gib=0), "expected_disk_size_gib"),
    ("size negative", changed("uefi_gpt_user_sudo", expected_disk_size_gib=-1.5), "expected_disk_size_gib"),
    ("size nan", changed("uefi_gpt_user_sudo", expected_disk_size_gib=float("nan")), "expected_disk_size_gib"),
    ("size list", changed("uefi_gpt_user_sudo", expected_disk_size_gib=[40]), "expected_disk_size_gib"),
    ("model int", changed("uefi_gpt_user_sudo", expected_disk_model=5), "expected_disk_model"),
    ("model list", changed("uefi_gpt_user_sudo", expected_disk_model=["QEMU"]), "expected_disk_model"),
]

VALID = [
    ("swap zero", changed("uefi_gpt_user_sudo", swap_mb=0)),
    ("swap max", changed("uefi_gpt_user_sudo", swap_mb=131072)),
    ("nested timezone", changed("uefi_gpt_user_sudo", timezone="America/Argentina/Buenos_Aires")),
    ("etc timezone", changed("uefi_gpt_user_sudo", timezone="Etc/GMT+5")),
    ("static ipv4", static()),
    ("static interface with dot", static(interface="eth0.10")),
    ("static three dns", static(dns=["1.1.1.1", "9.9.9.9", "2606:4700:4700::1111"])),
    ("static ipv6 dns", static(dns=["2001:db8::53"])),
    ("static prefix 8", static(address="10.1.2.3/8", gateway="10.200.0.1")),
    ("static prefix 30", static(address="192.168.10.21/30", gateway="192.168.10.22")),
    ("size int", changed("uefi_gpt_user_sudo", expected_disk_size_gib=40)),
    ("size float", changed("uefi_gpt_user_sudo", expected_disk_size_gib=1863.017)),
    ("size none", changed("uefi_gpt_user_sudo", expected_disk_size_gib=None)),
    ("model none", changed("uefi_gpt_user_sudo", expected_disk_model=None)),
    ("model text", changed("uefi_gpt_user_sudo", expected_disk_model="QEMU HARDDISK")),
]

# Names a model could pick to exfiltrate an unrelated variable as a password.
REJECTED_ENV_NAMES = (
    "PATH", "HOME", "GENTOO_AGENT_HOME", "SSH_AUTH_SOCK", "LD_PRELOAD", "PYTHONPATH", "GITHUB_TOKEN",
    "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_KEY", "SHLVL", "HOSTNAME", "USERNAME", "LC_ALL",
    "TZ", "DISPLAY", "DATABASE_URL", "GOOGLE_APPLICATION_CREDENTIALS", "PASSWORD", "PW", "_PW", "MY_PW_X",
    "SSH_PASSWORD", "GIT_PASSWORD", "AWS_PASSWORD", "MY_SECRET_PASSWORD", "OPENAI_PW", "GITHUB_TOKEN_PASSWORD",
    "PYTHON_PW", "LD_PW", "ANTHROPIC_PASSWORD", "DB_PRIVATE_PW",
)
VALID_ENV_NAMES = ("ALEX_PW", "GENTOO_ROOT_PASSWORD", "GENTOO_USER_PASSWORD", "ROOT_PW", "TEST_GENTOO_USER_PASSWORD")


WINDOWS_ENTRY = {"os": "windows", "disk": "/dev/sda", "esp_uuid": "ABCD-1234",
                 "loader": "/EFI/Microsoft/Boot/bootmgfw.efi"}


def entry(**changes) -> list[dict]:
    item = dict(WINDOWS_ENTRY)
    for key, value in changes.items():
        if value is DROP:
            item.pop(key)
        else:
            item[key] = value
    return [item]


INVALID_DUAL_BOOT = [
    ("dual_boot unknown", changed("uefi_gpt_user_sudo", dual_boot="on"), "dual_boot"),
    ("dual_boot bool", changed("uefi_gpt_user_sudo", dual_boot=True), "dual_boot"),
    ("entries not a list", changed("uefi_gpt_user_sudo", dual_boot_entries=WINDOWS_ENTRY), "dual_boot_entries"),
    ("entry not an object", changed("uefi_gpt_user_sudo", dual_boot_entries=["/dev/sda"]), "dual_boot_entries"),
    ("entry extra key", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(title="x")), "dual_boot_entries"),
    ("entry missing loader", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(loader=DROP)), "dual_boot_entries"),
    ("os linux", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(os="linux")), "dual_boot_entries"),
    ("os windows-bios", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(os="windows-bios")), "dual_boot_entries"),
    ("uuid injection", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(esp_uuid="ABCD-1234'; reboot; '")),
     "dual_boot_entries"),
    ("uuid lowercase", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(esp_uuid="abcd-1234")), "dual_boot_entries"),
    ("uuid newline", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(esp_uuid="ABCD-1234\n")), "dual_boot_entries"),
    ("uuid int", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(esp_uuid=1234)), "dual_boot_entries"),
    ("disk injection", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(disk="/dev/sda'; reboot")),
     "dual_boot_entries"),
    ("disk outside dev", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(disk="/tmp/sda")), "dual_boot_entries"),
    ("disk is the install disk", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(disk="/dev/nvme0n1")),
     "dual_boot_entries"),
    ("duplicate disk", changed("uefi_gpt_user_sudo", dual_boot_entries=entry() + entry()), "dual_boot_entries"),
    ("loader elsewhere", changed("uefi_gpt_user_sudo", dual_boot_entries=entry(loader="/EFI/Boot/bootx64.efi")),
     "dual_boot_entries"),
    ("loader injection", changed("uefi_gpt_user_sudo",
                                 dual_boot_entries=entry(loader="/EFI/Microsoft/Boot/bootmgfw.efi\n}")),
     "dual_boot_entries"),
]


class DualBootValidationTests(unittest.TestCase):
    def test_hostile_dual_boot_settings_are_refused(self):
        for name, install, field in INVALID_DUAL_BOOT:
            with self.subTest(name):
                with self.assertRaises(AgentError) as caught:
                    validated_settings(TARGET, install)
                self.assertEqual(caught.exception.code, CONFIG_INVALID)
                self.assertEqual(caught.exception.details.get("field"), field)

    def test_entries_need_auto_and_uefi(self):
        for install in (changed("uefi_gpt_user_sudo", dual_boot="off", dual_boot_entries=entry()),
                        changed("bios_mbr_user_sudo", dual_boot_entries=entry(disk="/dev/sdb"))):
            with self.subTest(boot=install["boot_mode"]), self.assertRaises(AgentError) as caught:
                validated_settings(TARGET, install)
            self.assertEqual(caught.exception.code, CONFIG_CONFLICT)

    def test_valid_dual_boot_settings_are_accepted(self):
        for install in (changed("uefi_gpt_user_sudo", dual_boot="auto", dual_boot_entries=entry()),
                        changed("uefi_gpt_user_sudo", dual_boot="off", dual_boot_entries=[]),
                        changed("bios_mbr_user_sudo", dual_boot="auto", dual_boot_entries=[]),
                        changed("uefi_gpt_user_sudo", dual_boot=DROP, dual_boot_entries=DROP)):
            with self.subTest(install=install.get("dual_boot_entries")):
                validated_settings(TARGET, install)


class SettingValidationTests(unittest.TestCase):
    def test_invalid_settings_are_rejected(self):
        for name, install, field in INVALID:
            with self.subTest(name):
                with self.assertRaises(AgentError) as caught:
                    validated_settings(TARGET, install)
                self.assertEqual(caught.exception.code, CONFIG_INVALID)
                self.assertEqual(caught.exception.details.get("field"), field)

    def test_unsafe_password_env_names_are_rejected(self):
        for field in ("root_password_env", "user_password_env"):
            for name in REJECTED_ENV_NAMES:
                with self.subTest(field=field, name=name):
                    with self.assertRaises(AgentError) as caught:
                        validated_settings(TARGET, changed("uefi_gpt_user_sudo", **{field: name}))
                    self.assertEqual(caught.exception.code, CONFIG_INVALID)
                    self.assertEqual(caught.exception.details.get("field"), field)
                    self.assertIn("must end in _PASSWORD or _PW", caught.exception.message)
            for name in VALID_ENV_NAMES:
                with self.subTest(field=field, name=name):
                    validated_settings(TARGET, changed("uefi_gpt_user_sudo", **{field: name}))

    def test_static_address_message(self):
        with self.assertRaises(AgentError) as caught:
            validated_settings(TARGET, static(address="192.168.10.20"))
        self.assertEqual(caught.exception.message, "Static addresses must be IPv4 in CIDR form, for example 192.168.10.20/24")

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


class ArchitectureGateTests(unittest.TestCase):
    def test_arm64_needs_uefi(self):
        install = changed("bios_mbr_user_sudo", architecture="arm64")
        with self.assertRaises(AgentError) as caught:
            validated_settings(TARGET, install)
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)
        self.assertEqual(caught.exception.details, {"fields": ["architecture", "boot_mode"]})

    def test_review_reports_an_architecture_mismatch(self):
        install = fixture("uefi_gpt_user_sudo")
        inventory = {"results": [identity_result("aarch64"), {"label": "boot_mode", "returncode": 0, "stdout": "uefi"},
                                 storage_result("/dev/sda")]}
        review = validate_installation(TARGET, install, {}, inventory)
        self.assertIn("architecture amd64 does not match the live system (arm64)", review["errors"])


if __name__ == "__main__":
    unittest.main()
