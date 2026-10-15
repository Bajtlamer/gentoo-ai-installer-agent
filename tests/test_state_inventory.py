import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import inventory, state
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS


TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root"}


class StateTests(unittest.TestCase):
    def test_missing_state_has_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(state, "STATE_DIR", Path(tmp)):
            self.assertEqual(state.load_state(), {"schema_version": 1, "completed_phases": []})

    def test_save_state_stamps_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(state, "STATE_DIR", Path(tmp)):
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
        with tempfile.TemporaryDirectory() as tmp, patch.object(inventory, "INVENTORY_DIR", Path(tmp)), patch.object(
            inventory, "run_ssh_readonly", self.fake_ssh
        ):
            data, path = inventory.collect_inventory(TARGET, on_progress=seen.append)
            self.assertTrue(path.exists())
        self.assertEqual(seen, list(READ_ONLY_COMMANDS))
        self.assertEqual(len(data["results"]), len(READ_ONLY_COMMANDS))
        self.assertTrue(data["read_only"])
        self.assertEqual(data["target_address"], "root@192.0.2.10:22")
        errors = next(item for item in data["results"] if item["label"] == "errors")
        self.assertIn("timed out", errors["error"])

    def test_latest_inventory_returns_newest_or_none(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(inventory, "INVENTORY_DIR", Path(tmp)):
            self.assertIsNone(inventory.latest_inventory())
            with patch.object(inventory, "run_ssh_readonly", self.fake_ssh):
                data, _ = inventory.collect_inventory(TARGET)
            self.assertEqual(inventory.latest_inventory(), data)


if __name__ == "__main__":
    unittest.main()
