"""Generated remote commands must stay byte-identical across refactors.

Regenerate only on an intentional command change:
    GOLDEN_UPDATE=1 python -m unittest tests.test_golden_commands
"""

import json
import os
import sys
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
    sys.path.insert(0, str(ROOT / "tools"))
    import install_phases

    with patch("install_phases.target_config", return_value=TARGET), patch(
        "install_phases.public_key_for_target", return_value=PUBLIC_KEY
    ):
        parts = []
        for phase in install_phases.PHASES:
            parts.append(f"===== {phase} apply =====")
            parts.append(install_phases.command_for(phase, TARGET, install))
            parts.append(f"===== {phase} check =====")
            parts.append(install_phases.check_command_for(phase, TARGET, install))
        return "\n".join(parts) + "\n"


class GoldenCommandTests(unittest.TestCase):
    def test_generated_commands_match_golden_files(self):
        configs = sorted((GOLDEN / "configs").glob("*.json"))
        self.assertEqual(len(configs), 4)
        for config_path in configs:
            with self.subTest(config=config_path.stem):
                install = json.loads(config_path.read_text(encoding="utf-8"))
                rendered = render(install)
                golden_path = GOLDEN / f"{config_path.stem}.txt"
                if os.environ.get("GOLDEN_UPDATE") == "1":
                    golden_path.write_text(rendered, encoding="utf-8", newline="\n")
                self.assertEqual(golden_path.read_text(encoding="utf-8"), rendered)


if __name__ == "__main__":
    unittest.main()
