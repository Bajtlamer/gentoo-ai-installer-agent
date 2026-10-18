"""Disk identity: what a plan binds to, and what is re-checked before any change."""

from __future__ import annotations

import json
import subprocess

from gentoo_agent.core.errors import (
    CONFIG_INVALID,
    DISK_AMBIGUOUS,
    HOST_KEY_UNKNOWN,
    INVENTORY_MISSING,
    SSH_UNREACHABLE,
    TARGET_DRIFT,
    AgentError,
)
from gentoo_agent.core.ssh import HOST_KEY_MESSAGE, READ_ONLY_COMMANDS, host_key_failed, run_ssh_readonly


IDENTITY_LABEL = "disk_identity"
IDENTITY_FIELDS = ("path", "size_bytes", "model", "serial", "wwn")


def _text(value: object) -> str:
    return str(value or "").strip()


def _block_devices(stdout: object, label: str) -> list[dict]:
    """The blockdevices list of lsblk -J output; INVENTORY_MISSING if malformed."""
    def malformed() -> AgentError:
        return AgentError(INVENTORY_MISSING, f"{label} output is not valid lsblk JSON", {"label": label})

    if not isinstance(stdout, str):
        raise malformed()
    try:
        data = json.loads(stdout or "{}")
    except (json.JSONDecodeError, RecursionError) as exc:
        raise malformed() from exc
    devices = data.get("blockdevices", []) if isinstance(data, dict) else None
    if not isinstance(devices, list) or not all(isinstance(device, dict) for device in devices):
        raise malformed()
    return devices


def _size_bytes(device: dict) -> int:
    value = device.get("size") or 0
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AgentError(INVENTORY_MISSING, "disk_identity lists a disk with an invalid size", {"label": IDENTITY_LABEL})
    try:
        return int(value)
    except ValueError as exc:
        raise AgentError(INVENTORY_MISSING, "disk_identity lists a disk with an invalid size", {"label": IDENTITY_LABEL}) from exc


def _identity(device: dict) -> dict:
    identity = {
        "path": _text(device.get("path")),
        "size_bytes": _size_bytes(device),
        "model": _text(device.get("model")),
        "serial": _text(device.get("serial")),
        "wwn": _text(device.get("wwn")),
    }
    # These values reach the human approval dialog; control characters could fake lines in it.
    if not all(identity[field].isprintable() for field in ("path", "model", "serial", "wwn")):
        raise AgentError(INVENTORY_MISSING, "disk identity contains unprintable characters", {"label": IDENTITY_LABEL})
    return identity


def parse_disk_identities(stdout: str) -> list[dict]:
    return [_identity(device) for device in _block_devices(stdout, IDENTITY_LABEL) if device.get("type") == "disk"]


def inventory_results(inventory: object) -> dict:
    """Inventory results keyed by label; INVENTORY_MISSING if the shape is wrong."""
    results = inventory.get("results") if isinstance(inventory, dict) else None
    if not isinstance(results, list):
        raise AgentError(INVENTORY_MISSING, "The inventory is malformed; refresh it.", {})
    return {item.get("label"): item for item in results if isinstance(item, dict) and isinstance(item.get("label"), str)}


def _flag(value: object) -> bool:
    return value is True or value == 1 or value == "1"


def _mountpoints(device: dict) -> list[str]:
    """Non-empty mountpoints of a storage_json device and all its children."""
    found: list[str] = []
    values = device.get("mountpoints")
    if not isinstance(values, list):
        values = [device.get("mountpoint")]
    found += [value for value in values if isinstance(value, str) and value]
    children = device.get("children")
    for child in children if isinstance(children, list) else []:
        if isinstance(child, dict):
            found += _mountpoints(child)
    return found


def _mounted_disks(results: dict) -> dict[str, list[str]]:
    item = results.get("storage_json")
    if item is None:
        return {}
    if item.get("returncode") != 0:
        raise AgentError(INVENTORY_MISSING, "The storage_json inventory failed; mount state is unknown. Refresh it.",
                         {"label": "storage_json"})
    devices = _block_devices(item.get("stdout"), "storage_json")
    return {_text(device.get("path")): _mountpoints(device) for device in devices}


def disk_candidates(inventory: dict) -> tuple[list[dict], list[dict]]:
    """Split inventory disks into (eligible identities, excluded identities with a reason).

    A disk is excluded when it is removable, attached over USB, read-only,
    or has a mounted partition. Missing fields count as not set; a missing
    storage_json result counts as nothing mounted.
    """
    results = inventory_results(inventory)
    item = results.get(IDENTITY_LABEL)
    if item is None or item.get("returncode") != 0:
        raise AgentError(INVENTORY_MISSING, "The latest inventory has no disk identity; refresh it.", {"label": IDENTITY_LABEL})
    mounted = _mounted_disks(results)
    eligible: list[dict] = []
    excluded: list[dict] = []
    for device in _block_devices(item.get("stdout"), IDENTITY_LABEL):
        if device.get("type") != "disk":
            continue
        identity = _identity(device)
        reasons = []
        if _flag(device.get("rm")):
            reasons.append("removable")
        if _text(device.get("tran")).lower() == "usb":
            reasons.append("usb transport")
        if _flag(device.get("ro")):
            reasons.append("read-only")
        mountpoints = mounted.get(identity["path"], [])
        if mountpoints:
            reasons.append("mounted at " + ", ".join(mountpoints))
        if reasons:
            excluded.append({**identity, "reason": "; ".join(reasons)})
        else:
            eligible.append(identity)
    return eligible, excluded


def identity_strength(disk: dict, others: list[dict] | tuple = ()) -> str:
    """Strong when a serial or WWN is present and no other disk in others shares it."""
    def unique(field: str) -> bool:
        value = disk.get(field)
        return bool(value) and not any(o.get("path") != disk.get("path") and o.get(field) == value for o in others)

    return "strong" if unique("serial") or unique("wwn") else "weak"


def disk_from_inventory(inventory: dict | None, path: str) -> dict:
    results = {item.get("label"): item for item in (inventory or {}).get("results", [])}
    item = results.get(IDENTITY_LABEL)
    if item is None or item.get("returncode") != 0:
        raise AgentError(INVENTORY_MISSING, "The latest inventory has no disk identity; run inspect again.", {"label": IDENTITY_LABEL})
    parsed = parse_disk_identities(item.get("stdout", ""))
    for disk in parsed:
        if disk["path"] == path:
            if identity_strength(disk, parsed) == "weak":
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
                        f"Disk {path} has no unique serial number or WWN and another disk has the same size and model; it cannot be identified reliably.",
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
    if host_key_failed(result):
        raise AgentError(HOST_KEY_UNKNOWN, HOST_KEY_MESSAGE, {"host": target["host"]})
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
