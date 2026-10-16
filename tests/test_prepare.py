import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gentoo_agent import prepare
from gentoo_agent.core.config import load_json, save_json
from tests.helpers import temp_home


class PrepareInventoryTests(unittest.TestCase):
    def test_uses_core_inventory_and_warns_on_nonzero_results(self):
        inventory = {"results": [
            {"label": "identity", "returncode": 0},
            {"label": "storage", "returncode": 2},
            {"label": "errors", "error": "timed out"},
        ]}

        def fake_collect(target, on_progress=None):
            on_progress("identity")
            return inventory, Path("/tmp/inventory.json")

        output = io.StringIO()
        with patch.object(prepare, "collect_inventory", fake_collect), redirect_stdout(output):
            result, path = prepare.collect_inventory_interactive({"name": "gentoo"})
        self.assertIs(result, inventory)
        self.assertEqual(path, Path("/tmp/inventory.json"))
        self.assertIn("  Read-only check: identity", output.getvalue())
        self.assertIn("    warning: storage returned 2", output.getvalue())
        self.assertNotIn("identity returned", output.getvalue())

    def test_missing_ssh_client_is_reported(self):
        def fake_collect(target, on_progress=None):
            raise FileNotFoundError("ssh")

        with patch.object(prepare, "collect_inventory", fake_collect):
            with self.assertRaises(SystemExit) as caught:
                prepare.collect_inventory_interactive({"name": "gentoo"})
        self.assertEqual(str(caught.exception), "OpenSSH client was not found. Install ssh and retry.")

    def test_unreachable_target_is_reported_without_a_traceback(self):
        from gentoo_agent.core.errors import SSH_UNREACHABLE, AgentError

        def fake_collect(target, on_progress=None):
            raise AgentError(SSH_UNREACHABLE, "Could not reach the target over SSH; check the address, key, and host key.",
                             {"host": "gentoo.invalid"})

        with patch.object(prepare, "collect_inventory", fake_collect):
            with self.assertRaises(SystemExit) as caught:
                prepare.collect_inventory_interactive({"name": "gentoo"})
        self.assertEqual(str(caught.exception), "SSH_UNREACHABLE: Could not reach the target over SSH; "
                                                "check the address, key, and host key.")


REPO = Path(__file__).resolve().parents[1]


class EraseAuthorizationTests(unittest.TestCase):
    def build(self, erase_answer: str) -> tuple[dict, dict]:
        target = {"name": "gentoo", "host": "192.0.2.10", "user": "root", "identity_file": "~/.ssh/id_test"}

        def fake_ask(prompt, default=None):
            if prompt.startswith("Type ERASE"):
                return erase_answer
            return default if default is not None else ""

        with temp_home() as home:
            save_json(home / "config" / "install.default.json", load_json(REPO / "config" / "install.default.json"))
            with patch.object(prepare, "choose_disk", return_value={"path": "/dev/sda", "model": "QEMU HARDDISK", "size": None}), \
                    patch.object(prepare, "ask", fake_ask), \
                    patch.object(prepare, "ask_choice", lambda prompt, choices, default: default), \
                    patch.object(prepare, "ask_int", lambda prompt, default: default), \
                    redirect_stdout(io.StringIO()):
                install = prepare.build_install_config(target, {"results": []})
        return target, install

    def test_typed_erase_confirmation_authorizes_both_files(self):
        target, install = self.build("ERASE /dev/sda")
        self.assertIs(target["target_is_disposable"], True)
        self.assertIs(target["disk_erase_authorized"], True)
        for field in ("disk_must_be_erased", "target_is_disposable", "disk_erase_authorized"):
            self.assertIs(install[field], True, field)

    def test_declined_confirmation_authorizes_nothing(self):
        target, install = self.build("CANCEL")
        self.assertNotIn("target_is_disposable", target)
        self.assertIs(install["disk_must_be_erased"], False)
        self.assertIs(install["disk_erase_authorized"], False)


if __name__ == "__main__":
    unittest.main()
