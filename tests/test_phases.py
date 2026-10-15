import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import config, phases
from gentoo_agent.core.errors import CONFIG_CONFLICT, CONFIG_INVALID, AgentError


ROOT = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "user": "root", "identity_file": "~/.ssh/id_test"}


def fixture(name: str) -> dict:
    return json.loads((ROOT / "tests" / "golden" / "configs" / f"{name}.json").read_text(encoding="utf-8"))


class PhaseGeneratorTests(unittest.TestCase):
    def test_phases_module_does_not_load_target_config_itself(self):
        self.assertFalse(hasattr(phases, "target_config"))

    def test_storage_command_uses_the_given_target(self):
        command = phases.storage_command(TARGET, fixture("bios_mbr_user_sudo"))
        self.assertIn("sfdisk", command)
        self.assertIn("disk=/dev/sda", command)

    def test_uefi_with_mbr_is_a_conflict(self):
        install = fixture("uefi_gpt_user_sudo")
        install["partition_table"] = "mbr"
        with self.assertRaises(AgentError) as caught:
            phases.validated_settings(TARGET, install)
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)

    def test_unsafe_disk_path_is_invalid(self):
        install = fixture("bios_mbr_user_sudo")
        install["disk"] = "/dev/sda;reboot"
        with self.assertRaises(AgentError) as caught:
            phases.validated_settings(TARGET, install)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_missing_identity_file_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            phases.public_key_for_target({"user": "root"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_unknown_phase_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            phases.command_for("reboot", TARGET, fixture("bios_mbr_user_sudo"))
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


class ConfigLoadingTests(unittest.TestCase):
    def test_missing_target_config_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "CONFIG", Path(tmp)):
            with self.assertRaises(AgentError) as caught:
                config.target_config()
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


if __name__ == "__main__":
    unittest.main()
