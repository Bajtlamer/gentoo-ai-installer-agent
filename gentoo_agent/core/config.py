"""Local JSON configuration: loading, atomic saving, timestamps."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core import paths


def _read_umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


_UMASK = _read_umask()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, value: dict, *, mode: int | None = None) -> None:
    """Write JSON atomically through a unique temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        if mode is None:
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o666 & ~_UMASK
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def target_config() -> dict:
    path = paths.config_dir() / "target.json"
    if not path.exists():
        raise AgentError(CONFIG_INVALID, "Missing config/target.json; copy target.example.json first.", {"path": str(path)})
    return load_json(path)


def install_config() -> dict:
    target = target_config()
    configured_path = paths.root() / target.get("install_config", "config/install.json")
    if not configured_path.exists():
        raise AgentError(CONFIG_INVALID, f"Missing installation configuration: {configured_path}", {"path": str(configured_path)})
    return load_json(configured_path)


def load_plan() -> dict:
    return load_json(paths.config_dir() / "plan.json")
