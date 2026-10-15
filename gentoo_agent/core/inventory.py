"""Read-only target inventory collected with the SSH allowlist."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Callable

from gentoo_agent.core.config import load_json, now, save_json
from gentoo_agent.core.paths import INVENTORY_DIR, ROOT
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS, run_ssh_readonly
from gentoo_agent.core.state import load_state, save_state


def collect_inventory(target: dict, on_progress: Callable[[str], None] | None = None) -> tuple[dict, Path]:
    results = []
    for label, command in READ_ONLY_COMMANDS.items():
        if on_progress is not None:
            on_progress(label)
        try:
            results.append(run_ssh_readonly(target, label, command))
        except subprocess.TimeoutExpired as exc:
            results.append({"label": label, "command": command, "error": str(exc)})
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data = {
        "target": target["name"],
        "target_address": f"{target['user']}@{target['host']}:{target.get('port', 22)}",
        "collected_at": now(),
        "read_only": True,
        "results": results,
    }
    path = INVENTORY_DIR / f"inventory-{stamp}.json"
    save_json(path, data)
    return data, path


def record_inventory(target: dict, path: Path) -> None:
    state = load_state()
    state.update({"target": target["name"], "last_inventory": str(path.relative_to(ROOT))})
    save_state(state)


def latest_inventory() -> dict | None:
    paths = sorted(INVENTORY_DIR.glob("inventory-*.json"), key=lambda item: item.stat().st_mtime)
    return load_json(paths[-1]) if paths else None
