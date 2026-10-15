"""Handbook snapshot: the procedure source, recorded with URL and retrieval time."""

from __future__ import annotations

import datetime as dt
import html.parser
import urllib.request

from gentoo_agent.core.config import now, save_json
from gentoo_agent.core import paths


class TitleParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def fetch_handbook(target: dict) -> dict:
    url = target["handbook_url"]
    request = urllib.request.Request(url, headers={"User-Agent": "gentoo-agent/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    inventory_dir = paths.inventory_dir()
    inventory_dir.mkdir(parents=True, exist_ok=True)
    html_path = inventory_dir / f"handbook-{stamp}.html"
    html_path.write_bytes(raw)
    parser = TitleParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    metadata = {
        "url": url,
        "retrieved_at": now(),
        "content_type": content_type,
        "bytes": len(raw),
        "title": "".join(parser.title_parts).strip(),
        "saved_html": str(html_path.relative_to(paths.root())),
    }
    save_json(inventory_dir /f"handbook-{stamp}.json", metadata)
    return metadata
