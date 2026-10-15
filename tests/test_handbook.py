import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import handbook


class FakeResponse:
    headers = {"Content-Type": "text/html; charset=UTF-8"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"<html><head><title>Handbook:AMD64 - Gentoo Wiki</title></head></html>"


class HandbookTests(unittest.TestCase):
    def test_fetch_records_url_title_and_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_dir = root / "inventory"
            with patch.object(handbook, "ROOT", root), patch.object(handbook, "INVENTORY_DIR", inventory_dir), patch.object(
                handbook.urllib.request, "urlopen", return_value=FakeResponse()
            ):
                metadata = handbook.fetch_handbook({"handbook_url": "https://wiki.gentoo.org/wiki/Handbook:AMD64"})
            self.assertTrue((root / metadata["saved_html"]).exists())
        self.assertEqual(metadata["url"], "https://wiki.gentoo.org/wiki/Handbook:AMD64")
        self.assertEqual(metadata["title"], "Handbook:AMD64 - Gentoo Wiki")
        self.assertEqual(metadata["content_type"], "text/html; charset=UTF-8")
        self.assertIn("retrieved_at", metadata)


if __name__ == "__main__":
    unittest.main()
