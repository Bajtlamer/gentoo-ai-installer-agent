import json
import unittest
from pathlib import Path

from gentoo_agent.core.errors import (
    CONFIG_CONFLICT,
    CONFIG_INVALID,
    DISK_AMBIGUOUS,
    INVENTORY_MISSING,
    AgentError,
)
from gentoo_agent.core.resolve import resolve_config


REPO = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
          "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True}
DEFAULTS = json.loads((REPO / "config" / "install.default.json").read_text(encoding="utf-8"))


def inventory(boot: str = "uefi", disks: tuple[str, ...] = ("/dev/nvme0n1",)) -> dict:
    devices = [{"path": path, "type": "disk", "size": 512110190592, "model": "NVMe", "serial": f"S{index}", "wwn": None}
               for index, path in enumerate(disks)]
    return {"results": [
        {"label": "boot_mode", "returncode": 0, "stdout": boot if boot == "uefi" else "bios-or-unknown"},
        {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": devices})},
    ]}


def resolve(overrides=None, fallbacks=None, **kwargs):
    return resolve_config(TARGET, overrides or {}, fallbacks or {}, DEFAULTS, kwargs.get("inv", inventory()),
                          profile=kwargs.get("profile"))


class ResolveTests(unittest.TestCase):
    def test_spec_example(self):
        result = resolve(
            {"username": "Alex", "user_password": {"env": "ALEX_PW"}, "boot_mode": "uefi", "bootloader": "grub"},
            {"username": "gentoo", "hostname": "server", "timezone": "Europe/Prague"},
        )
        install, sources = result["install"], result["sources"]
        self.assertEqual(install["username"], "alex")
        self.assertIn("username Alex was lowercased to alex", result["warnings"])
        self.assertEqual(install["user_password_env"], "ALEX_PW")
        self.assertEqual(install["password_source"], "environment")
        self.assertEqual(install["hostname"], "server")
        self.assertEqual(sources["hostname"], "fallback")
        self.assertEqual(sources["username"], "override")
        self.assertEqual(sources["user_password_env"], "override")
        self.assertEqual(install["disk"], "/dev/nvme0n1")
        self.assertEqual(sources["disk"], "inferred")
        self.assertEqual(install["partition_table"], "gpt")
        self.assertEqual(install["partition_layout"][0]["device"], "/dev/nvme0n1p1")
        self.assertTrue(install["disk_must_be_erased"])
        self.assertNotIn("user_password", install)
        self.assertNotIn("user_password", sources)

    def test_precedence(self):
        result = resolve({"hostname": "a"}, {"hostname": "c"}, profile={"hostname": "b"})
        self.assertEqual(result["install"]["hostname"], "a")
        result = resolve({}, {"hostname": "c"}, profile={"hostname": "b"})
        self.assertEqual((result["install"]["hostname"], result["sources"]["hostname"]), ("b", "profile"))
        result = resolve({}, {})
        self.assertEqual(result["sources"]["hostname"], "default")

    def test_plain_text_password_is_rejected(self):
        for value in ("secret", {"env": "lowercase"}, {"value": "x"}, {"env": "OK", "extra": 1}):
            with self.subTest(value=value), self.assertRaises(AgentError) as caught:
                resolve({"user_password": value})
            self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_passwords_only_in_overrides_or_profile(self):
        with self.assertRaises(AgentError) as caught:
            resolve({}, {"root_password": {"env": "ROOT_PW"}})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"partition_layout": []})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(caught.exception.details["keys"], ["partition_layout"])

    def test_requested_boot_mode_must_match_the_live_system(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"boot_mode": "uefi"}, inv=inventory(boot="bios"))
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)
        self.assertEqual(caught.exception.details, {"requested": "uefi", "live": "bios"})

    def test_boot_mode_is_inferred_from_the_live_system(self):
        result = resolve({}, inv=inventory(boot="bios", disks=("/dev/sda",)))
        self.assertEqual(result["install"]["boot_mode"], "bios")
        self.assertEqual(result["sources"]["boot_mode"], "inferred")
        self.assertEqual(result["install"]["partition_table"], "mbr")

    def test_several_disks_need_a_choice(self):
        with self.assertRaises(AgentError) as caught:
            resolve({}, inv=inventory(disks=("/dev/sda", "/dev/sdb")))
        self.assertEqual(caught.exception.code, DISK_AMBIGUOUS)
        self.assertEqual(caught.exception.details["candidates"], ["/dev/sda", "/dev/sdb"])
        result = resolve({"disk": "/dev/sdb"}, inv=inventory(disks=("/dev/sda", "/dev/sdb")))
        self.assertEqual(result["install"]["disk"], "/dev/sdb")

    def test_missing_inventory(self):
        with self.assertRaises(AgentError) as caught:
            resolve_config(TARGET, {}, {}, DEFAULTS, None)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_unsupported_bootloader(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"bootloader": "systemd-boot"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_erase_flags_come_from_the_target(self):
        result = resolve_config({**TARGET, "disk_erase_authorized": False}, {}, {}, DEFAULTS, inventory())
        self.assertFalse(result["install"]["disk_erase_authorized"])


if __name__ == "__main__":
    unittest.main()
