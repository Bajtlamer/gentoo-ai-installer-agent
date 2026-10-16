"""Read-only target inventory collected with the SSH allowlist."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Callable

from gentoo_agent.core.config import load_json, now, save_json
from gentoo_agent.core import paths
from gentoo_agent.core.errors import HOST_KEY_UNKNOWN, SSH_UNREACHABLE, AgentError
from gentoo_agent.core.ssh import HOST_KEY_MESSAGE, READ_ONLY_COMMANDS, host_key_failed, run_ssh_readonly
from gentoo_agent.core.state import load_state, save_state


def _unreachable(result: dict) -> bool:
    """ssh exits with 255 when it cannot connect or authenticate; a timeout is recorded as an error."""
    return "error" in result or result.get("returncode") == 255


def collect_inventory(target: dict, on_progress: Callable[[str], None] | None = None) -> tuple[dict, Path]:
    results = []
    for label, command in READ_ONLY_COMMANDS.items():
        if on_progress is not None:
            on_progress(label)
        try:
            results.append(run_ssh_readonly(target, label, command))
        except subprocess.TimeoutExpired as exc:
            results.append({"label": label, "command": command, "error": str(exc)})
        except OSError as exc:
            raise AgentError(SSH_UNREACHABLE, f"Could not run ssh to inspect {target.get('host')}: {exc}",
                             {"host": target.get("host"), "label": label}) from exc
    if all(_unreachable(item) for item in results):
        # An inventory without a single answer from the target is not evidence: never save it.
        if any(host_key_failed(item) for item in results):
            raise AgentError(HOST_KEY_UNKNOWN, HOST_KEY_MESSAGE, {"host": target["host"]})
        raise AgentError(SSH_UNREACHABLE, "Could not reach the target over SSH; check the address, key, and host key.",
                         {"host": target["host"]})
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data = {
        "target": target["name"],
        "target_address": f"{target['user']}@{target['host']}:{target.get('port', 22)}",
        "collected_at": now(),
        "read_only": True,
        "results": results,
    }
    path = paths.inventory_dir() / f"inventory-{stamp}.json"
    save_json(path, data)
    return data, path


def record_inventory(target: dict, path: Path) -> None:
    state = load_state()
    state.update({"target": target["name"], "last_inventory": str(path.relative_to(paths.root()))})
    save_state(state)


def latest_inventory() -> dict | None:
    files = sorted(paths.inventory_dir().glob("inventory-*.json"), key=lambda item: item.stat().st_mtime)
    return load_json(files[-1]) if files else None
