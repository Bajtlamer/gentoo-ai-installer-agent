import subprocess
import unittest
from unittest.mock import patch

from gentoo_agent.core import inventory, paths, state
from gentoo_agent.core.errors import SSH_UNREACHABLE, AgentError
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS
from tests.helpers import temp_home


TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root"}


class StateTests(unittest.TestCase):
    def test_missing_state_has_defaults(self):
        with temp_home():
            self.assertEqual(state.load_state(), {"schema_version": 1, "completed_phases": []})

    def test_save_state_stamps_updated_at(self):
        with temp_home():
            state.save_state({"status": "running"})
            loaded = state.load_state()
        self.assertEqual(loaded["status"], "running")
        self.assertIn("updated_at", loaded)


class InventoryTests(unittest.TestCase):
    def fake_ssh(self, target, label, command):
        if label == "errors":
            raise subprocess.TimeoutExpired(command, 45)
        return {"label": label, "command": command, "returncode": 0, "stdout": f"{label}-out", "stderr": ""}

    def test_collect_inventory_runs_every_allowlisted_command_and_saves(self):
        seen = []
        with temp_home(), patch.object(inventory, "run_ssh_readonly", self.fake_ssh):
            data, path = inventory.collect_inventory(TARGET, on_progress=seen.append)
            self.assertTrue(path.exists())
        self.assertEqual(seen, list(READ_ONLY_COMMANDS))
        self.assertEqual(len(data["results"]), len(READ_ONLY_COMMANDS))
        self.assertTrue(data["read_only"])
        self.assertEqual(data["target_address"], "root@192.0.2.10:22")
        errors = next(item for item in data["results"] if item["label"] == "errors")
        self.assertIn("timed out", errors["error"])

    def test_unreachable_target_raises_and_saves_nothing(self):
        def refused(target, label, command):
            return {"label": label, "command": command, "returncode": 255, "stdout": "",
                    "stderr": "ssh: connect to host gentoo.invalid port 22: Connection refused"}

        def timed_out(target, label, command):
            raise subprocess.TimeoutExpired(command, 45)

        def mixed(target, label, command):
            if label == "identity":
                return timed_out(target, label, command)
            return refused(target, label, command)

        for fake in (refused, timed_out, mixed):
            with self.subTest(fake=fake.__name__), temp_home(), patch.object(inventory, "run_ssh_readonly", fake):
                with self.assertRaises(AgentError) as caught:
                    inventory.collect_inventory({**TARGET, "host": "gentoo.invalid"})
                saved = list(paths.inventory_dir().glob("inventory-*.json"))
                self.assertIsNone(inventory.latest_inventory())
            self.assertEqual(caught.exception.code, SSH_UNREACHABLE)
            self.assertEqual(caught.exception.details, {"host": "gentoo.invalid"})
            self.assertEqual(caught.exception.message,
                             "Could not reach the target over SSH; check the address, key, and host key.")
            self.assertEqual(saved, [])

    def test_one_reachable_command_still_saves(self):
        def partly(target, label, command):
            code = 0 if label == "boot_mode" else 255
            return {"label": label, "command": command, "returncode": code, "stdout": "uefi", "stderr": ""}

        with temp_home(), patch.object(inventory, "run_ssh_readonly", partly):
            _, path = inventory.collect_inventory(TARGET)
            self.assertTrue(path.exists())

    def test_latest_inventory_returns_newest_or_none(self):
        with temp_home():
            self.assertIsNone(inventory.latest_inventory())
            with patch.object(inventory, "run_ssh_readonly", self.fake_ssh):
                data, _ = inventory.collect_inventory(TARGET)
            self.assertEqual(inventory.latest_inventory(), data)


if __name__ == "__main__":
    unittest.main()
