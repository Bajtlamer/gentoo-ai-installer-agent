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
