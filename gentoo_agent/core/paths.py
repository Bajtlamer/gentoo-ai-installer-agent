"""Filesystem locations used by the agent, resolved on every call.

GENTOO_AGENT_HOME overrides the repository root. Resolving at call time lets a
long-running server, a background worker, and tests follow the current
environment without patching module constants.
"""

from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    configured = os.environ.get("GENTOO_AGENT_HOME")
    base = Path(configured) if configured else Path(__file__).resolve().parents[2]
    return base.expanduser().resolve()


def config_dir() -> Path:
    return root() / "config"


def state_dir() -> Path:
    return root() / "state"


def inventory_dir() -> Path:
    return root() / "inventory"


def log_dir() -> Path:
    return root() / "logs"


def approval_dir() -> Path:
    return root() / "approvals"


def plans_dir() -> Path:
    return root() / "plans"


def runs_dir() -> Path:
    return root() / "runs"


def secrets_dir() -> Path:
    return root() / "secrets"
