"""Generated remote commands must stay byte-identical across refactors.

Regenerate only on an intentional command change:
    GOLDEN_UPDATE=1 python -m unittest tests.test_golden_commands
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
TARGET = {
    "name": "gentoo",
    "host": "192.0.2.10",
    "port": 22,
    "user": "root",
    "identity_file": "~/.ssh/id_test",
}
PUBLIC_KEY = "ssh-ed25519 AAAAGOLDEN gentoo-agent-golden"


def render(install: dict) -> str:
    from gentoo_agent.core import phases

    with patch("gentoo_agent.core.phases.public_key_for_target", return_value=PUBLIC_KEY):
        parts = []
        for phase in phases.PHASES:
            parts.append(f"===== {phase} apply =====")
            parts.append(phases.command_for(phase, TARGET, install))
            parts.append(f"===== {phase} check =====")
            parts.append(phases.check_command_for(phase, TARGET, install))
        return "\n".join(parts) + "\n"


class GoldenCommandTests(unittest.TestCase):
    def test_generated_commands_match_golden_files(self):
        configs = sorted((GOLDEN / "configs").glob("*.json"))
        self.assertEqual(len(configs), 7)
        for config_path in configs:
            with self.subTest(config=config_path.stem):
                install = json.loads(config_path.read_text(encoding="utf-8"))
                rendered = render(install)
                golden_path = GOLDEN / f"{config_path.stem}.txt"
                if os.environ.get("GOLDEN_UPDATE") == "1":
                    golden_path.write_bytes(rendered.encode("utf-8"))
                self.assertEqual(golden_path.read_bytes().decode("utf-8"), rendered)

    def test_empty_dual_boot_entries_change_nothing(self):
        # Every configuration without Windows renders exactly as before dual boot existed.
        for config_path in sorted((GOLDEN / "configs").glob("*.json")):
            install = json.loads(config_path.read_text(encoding="utf-8"))
            if install.get("dual_boot_entries"):
                continue
            base = {key: value for key, value in install.items() if key not in ("dual_boot", "dual_boot_entries")}
            for extra in ({}, {"dual_boot": "auto", "dual_boot_entries": []}, {"dual_boot": "off", "dual_boot_entries": []}):
                with self.subTest(config=config_path.stem, extra=extra):
                    self.assertEqual(render({**base, **extra}), (GOLDEN / f"{config_path.stem}.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
