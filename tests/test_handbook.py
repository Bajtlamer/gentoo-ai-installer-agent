import unittest
from unittest.mock import patch

from gentoo_agent.core import handbook
from tests.helpers import temp_home


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
        with temp_home() as home, patch.object(handbook.urllib.request, "urlopen", return_value=FakeResponse()):
            metadata = handbook.fetch_handbook({"handbook_url": "https://wiki.gentoo.org/wiki/Handbook:AMD64"})
            self.assertTrue((home / metadata["saved_html"]).exists())
            self.assertTrue(metadata["saved_html"].startswith("inventory/"))
        self.assertEqual(metadata["url"], "https://wiki.gentoo.org/wiki/Handbook:AMD64")
        self.assertEqual(metadata["title"], "Handbook:AMD64 - Gentoo Wiki")
        self.assertEqual(metadata["content_type"], "text/html; charset=UTF-8")
        self.assertIn("retrieved_at", metadata)

    def test_network_failure_is_reported_with_a_stable_code(self):
        import urllib.error

        from gentoo_agent.core.errors import HANDBOOK_UNAVAILABLE, AgentError

        with temp_home(), patch.object(
            handbook.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
        ):
            with self.assertRaises(AgentError) as caught:
                handbook.fetch_handbook({"handbook_url": "https://wiki.gentoo.org/wiki/Handbook:AMD64"})
        self.assertEqual(caught.exception.code, HANDBOOK_UNAVAILABLE)
        self.assertEqual(caught.exception.details, {"url": "https://wiki.gentoo.org/wiki/Handbook:AMD64"})


if __name__ == "__main__":
    unittest.main()
