"""Disk identity: what a plan binds to, and what is re-checked before any change."""

from __future__ import annotations

import json
import subprocess

from gentoo_agent.core.errors import (
    CONFIG_INVALID,
    DISK_AMBIGUOUS,
    INVENTORY_MISSING,
    SSH_UNREACHABLE,
    TARGET_DRIFT,
    AgentError,
)
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS, run_ssh_readonly


IDENTITY_LABEL = "disk_identity"
IDENTITY_FIELDS = ("path", "size_bytes", "model", "serial", "wwn")


def _text(value: object) -> str:
    return str(value or "").strip()


def parse_disk_identities(stdout: str) -> list[dict]:
    try:
        devices = json.loads(stdout or "{}").get("blockdevices", [])
    except json.JSONDecodeError as exc:
        raise AgentError(INVENTORY_MISSING, "disk_identity output is not valid lsblk JSON", {"label": IDENTITY_LABEL}) from exc
    return [
        {
            "path": _text(device.get("path")),
            "size_bytes": int(device.get("size") or 0),
            "model": _text(device.get("model")),
            "serial": _text(device.get("serial")),
            "wwn": _text(device.get("wwn")),
        }
        for device in devices
        if device.get("type") == "disk"
    ]


def identity_strength(disk: dict) -> str:
    return "strong" if disk.get("serial") or disk.get("wwn") else "weak"


def disk_from_inventory(inventory: dict | None, path: str) -> dict:
    results = {item.get("label"): item for item in (inventory or {}).get("results", [])}
    item = results.get(IDENTITY_LABEL)
    if item is None or item.get("returncode") != 0:
        raise AgentError(INVENTORY_MISSING, "The latest inventory has no disk identity; run inspect again.", {"label": IDENTITY_LABEL})
    parsed = parse_disk_identities(item.get("stdout", ""))
    for disk in parsed:
        if disk["path"] == path:
            if identity_strength(disk) == "weak":
                twins = [
                    other["path"]
                    for other in parsed
                    if other["path"] != path
                    and other["size_bytes"] == disk["size_bytes"]
                    and other["model"] == disk["model"]
                ]
                if twins:
                    raise AgentError(
                        DISK_AMBIGUOUS,
                        f"Disk {path} has no serial number or WWN and another disk has the same size and model; it cannot be identified reliably.",
                        {"disk": path, "twins": twins},
                    )
            return disk
    raise AgentError(CONFIG_INVALID, f"Configured disk {path} is not in the latest inventory.", {"disk": path})


def read_live_identity(target: dict, path: str) -> dict:
    try:
        result = run_ssh_readonly(target, IDENTITY_LABEL, READ_ONLY_COMMANDS[IDENTITY_LABEL])
    except subprocess.TimeoutExpired as exc:
        raise AgentError(SSH_UNREACHABLE, f"Timed out reading disk identity from {target['host']}", {"host": target["host"]}) from exc
    except OSError as exc:
        raise AgentError(SSH_UNREACHABLE, f"Could not run ssh to read disk identity from {target['host']}: {exc}", {"host": target["host"]}) from exc
    if result["returncode"] != 0:
        raise AgentError(
            SSH_UNREACHABLE,
            f"Could not read disk identity from {target['host']}: {result['stderr'].strip()}",
            {"host": target["host"], "returncode": result["returncode"]},
        )
    for disk in parse_disk_identities(result["stdout"]):
        if disk["path"] == path:
            return disk
    raise AgentError(TARGET_DRIFT, f"Disk {path} is no longer present on the target.", {"disk": path})


def same_disk(expected: dict, actual: dict) -> bool:
    return all(expected.get(field) == actual.get(field) for field in IDENTITY_FIELDS)


def check_drift(target: dict, expected_disks: list[dict]) -> None:
    for expected in expected_disks:
        actual = read_live_identity(target, expected["path"])
        if not same_disk(expected, actual):
            raise AgentError(
                TARGET_DRIFT,
                f"Disk {expected['path']} does not match the approved plan.",
                {"expected": expected, "actual": actual},
            )
