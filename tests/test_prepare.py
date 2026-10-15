import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gentoo_agent import prepare


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


if __name__ == "__main__":
    unittest.main()
