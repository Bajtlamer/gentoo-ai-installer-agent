import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core.config import load_json, save_json


class SaveJsonTests(unittest.TestCase):
    def test_concurrent_writers_never_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            errors = []

            def writer(number):
                try:
                    for index in range(200):
                        save_json(target, {"writer": number, "index": index})
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(number,)) for number in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertIn("writer", load_json(target))
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["state.json"])

    def test_failed_write_keeps_old_content_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            save_json(target, {"ok": True})
            with self.assertRaises(TypeError):
                save_json(target, {"bad": object()})
            self.assertEqual(load_json(target), {"ok": True})
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["state.json"])

    def test_mode_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "secret.json"
            save_json(target, {"value": 1}, mode=0o600)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_default_mode_preserves_existing_file_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.json"
            save_json(target, {"v": 1}, mode=0o600)
            save_json(target, {"v": 2})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_default_mode_for_new_files_follows_the_umask(self):
        from gentoo_agent.core import config
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "_UMASK", 0o077):
            target = Path(tmp) / "new.json"
            save_json(target, {"v": 1})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
