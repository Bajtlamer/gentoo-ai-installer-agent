import json
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import runner
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.phases import PHASES


ROOT = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root", "identity_file": "~/.ssh/id_test"}


def fixture() -> dict:
    return json.loads((ROOT / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))


class SelectPhasesTests(unittest.TestCase):
    def test_default_is_every_phase(self):
        self.assertEqual(runner.select_phases(None), PHASES)

    def test_start_phase_slices_the_list(self):
        self.assertEqual(runner.select_phases("kernel"), ["kernel", "bootloader", "validation"])

    def test_unknown_start_phase_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            runner.select_phases("reboot")
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


class DryRunTests(unittest.TestCase):
    def test_render_lists_header_and_each_phase(self):
        with patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA test"):
            text = runner.render_dry_run(TARGET, fixture(), ["preflight", "storage"])
        self.assertTrue(text.startswith("Target: root@192.0.2.10:22\nDisk:   /dev/sda (bios/mbr)\n"))
        self.assertIn("\n\n===== preflight =====\n", text)
        self.assertIn("\n\n===== storage =====\n", text)
        self.assertNotIn("===== filesystems =====", text)


if __name__ == "__main__":
    unittest.main()
