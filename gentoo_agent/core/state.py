"""Resumable local workflow state in state/state.json."""

from __future__ import annotations

from pathlib import Path

from gentoo_agent.core.config import load_json, now, save_json
from gentoo_agent.core import paths


def state_path() -> Path:
    return paths.state_dir() / "state.json"


def load_state() -> dict:
    path = state_path()
    return load_json(path) if path.exists() else {
        "schema_version": 1,
        "completed_phases": [],
    }


def save_state(state: dict) -> None:
    state["updated_at"] = now()
    save_json(state_path(), state)
