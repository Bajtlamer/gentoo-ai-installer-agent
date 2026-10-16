"""Shared test helpers."""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


@contextmanager
def temp_home():
    """Point GENTOO_AGENT_HOME at a fresh temporary directory for the duration of the block."""
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GENTOO_AGENT_HOME": tmp}):
        yield Path(tmp).resolve()


def storage_result(*disks: str) -> dict:
    """A storage inventory result that lists the given disk paths."""
    lines = ["NAME PATH SIZE TYPE"] + [f"{path.rsplit('/', 1)[-1]} {path} 40G disk" for path in disks]
    return {"label": "storage", "returncode": 0, "stdout": "\n".join(lines) + "\n"}


def fresh_inventory(*results: dict) -> dict:
    """An inventory collected now, so plan creation accepts its age."""
    from gentoo_agent.core.config import now

    return {"target": "gentoo", "collected_at": now(), "results": list(results)}
