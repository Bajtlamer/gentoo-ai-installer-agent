import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gentoo_agent import init_config
from gentoo_agent.core.config import load_json, save_json
from tests.helpers import temp_home


REPO = Path(__file__).resolve().parents[1]


def run(*arguments: str):
    """Run init_config.main() against a temporary home; returns (install, target) or raises SystemExit."""
    with temp_home() as home:
        for name in ("target.default.json", "install.default.json"):
            save_json(home / "config" / name, load_json(REPO / "config" / name))
        argv = ["init_config", "--host", "192.0.2.10", "--disk", "/dev/nvme0n1", "--allow-disk-erase", *arguments]
        with patch("sys.argv", argv), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            init_config.main()
        return load_json(home / "config" / "install.json"), load_json(home / "config" / "target.json")


class InitConfigArchitectureTests(unittest.TestCase):
    def test_architecture_defaults_to_amd64(self):
        install, target = run()
        self.assertEqual(install["architecture"], "amd64")
        self.assertEqual(install["profile"], "gentoo-amd64-openrc")
        self.assertEqual(target["architecture"], "amd64")

    def test_arm64_is_written_to_the_install_config(self):
        install, target = run("--architecture", "arm64")
        self.assertEqual(install["architecture"], "arm64")
        self.assertEqual(install["profile"], "gentoo-arm64-openrc")
        self.assertEqual(target["handbook_url"], "https://wiki.gentoo.org/wiki/Handbook:ARM64")

    def test_arm64_needs_uefi(self):
        with self.assertRaises(SystemExit) as caught:
            run("--architecture", "arm64", "--boot-mode", "bios")
        self.assertIn("UEFI", str(caught.exception))

    def test_unknown_architecture_is_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            run("--architecture", "x86")
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
