"""Filesystem locations used by the agent.

GENTOO_AGENT_HOME overrides the repository root, which keeps an editable
install (pip install -e .) and the tools/ shims pointing at the same files.
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(os.environ.get("GENTOO_AGENT_HOME") or Path(__file__).resolve().parents[2]).expanduser().resolve()
CONFIG = ROOT / "config"
STATE_DIR = ROOT / "state"
INVENTORY_DIR = ROOT / "inventory"
LOG_DIR = ROOT / "logs"
APPROVAL_DIR = ROOT / "approvals"
