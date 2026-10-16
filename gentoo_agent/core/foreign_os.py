"""Other operating systems on the target's disks, and the Secure Boot state.

Pure: reads a saved inventory and never contacts the target. Every value in
the inventory is untrusted. A value that does not match its strict format
makes the inventory unusable (INVENTORY_MISSING, refresh it); it never reaches
a generated program or the approval dialog.
"""

from __future__ import annotations

import re

from gentoo_agent.core.disks import _block_devices, inventory_results
from gentoo_agent.core.errors import INVENTORY_MISSING, AgentError
from gentoo_agent.core.validate import DEVICE_PATH, ESP_UUID, WINDOWS_LOADER


PARTITIONS_LABEL = "partitions"
SECURE_BOOT_LABEL = "secure_boot"
ESP_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MSR_TYPE = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
BASIC_DATA_TYPE = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
# MBR partition types are printed as hex bytes (0x7 for NTFS, 0xef for an EFI system partition).
MBR_TYPE = re.compile(r"0x[0-9a-f]{1,2}")
MBR_ESP_TYPE = "0xef"
PARTUUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SECURE_BOOT_STATES = {"1": "on", "0": "off"}


def _unusable(message: str) -> AgentError:
    return AgentError(INVENTORY_MISSING, f"{message}; refresh the inventory.", {"label": PARTITIONS_LABEL})


def _text(device: dict, key: str) -> str:
    value = device.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _unusable(f"The partitions inventory has a non-text {key}")
    return value


def _path(device: dict) -> str:
    path = _text(device, "path")
    if not DEVICE_PATH.fullmatch(path):
        raise _unusable("The partitions inventory lists an unsafe device path")
    return path


def _partitions(disk: dict) -> list[dict]:
    children = disk.get("children")
    if children is None:
        return []
    if not isinstance(children, list) or not all(isinstance(child, dict) for child in children):
        raise _unusable("The partitions inventory is malformed")
    return [child for child in children if _text(child, "type") == "part"]


def _checked(record: dict) -> dict:
    if not all(isinstance(item, str) and item.isprintable() for item in record["evidence"]):
        raise _unusable("The partitions inventory has unprintable evidence")
    return record


def _windows_record(disk_path: str, partitions: list[dict]) -> dict | None:
    esp: dict | None = None
    esp_path = ""
    windows: list[str] = []
    bios_ntfs: list[str] = []
    any_esp = False
    for part in partitions:
        path = _path(part)
        parttype = _text(part, "parttype").lower()
        fstype = _text(part, "fstype").lower()
        if parttype in (ESP_TYPE, MBR_ESP_TYPE):
            any_esp = True
        if parttype == ESP_TYPE and fstype == "vfat" and esp is None:
            esp, esp_path = part, path
        elif parttype == MSR_TYPE:
            windows.append(f"msr {path}")
        elif parttype == BASIC_DATA_TYPE and fstype == "ntfs":
            windows.append(f"ntfs {path}")
        elif MBR_TYPE.fullmatch(parttype) and fstype == "ntfs":
            bios_ntfs.append(f"ntfs {path}")
    if esp is not None and windows:
        esp_uuid = _text(esp, "uuid")
        esp_partuuid = _text(esp, "partuuid").lower()
        if not ESP_UUID.fullmatch(esp_uuid):
            raise _unusable(f"The EFI system partition {esp_path} has an unexpected UUID")
        if not PARTUUID.fullmatch(esp_partuuid):
            raise _unusable(f"The EFI system partition {esp_path} has an unexpected PARTUUID")
        return _checked({"disk": disk_path, "os": "windows", "esp_uuid": esp_uuid, "esp_partuuid": esp_partuuid,
                         "evidence": [f"esp vfat {esp_path}", *windows]})
    if bios_ntfs and not any_esp:
        return _checked({"disk": disk_path, "os": "windows-bios", "evidence": bios_ntfs})
    return None


def detect_foreign_os(inventory: dict) -> list[dict]:
    """One record per disk that holds Windows: UEFI ("windows") or BIOS ("windows-bios")."""
    item = inventory_results(inventory).get(PARTITIONS_LABEL)
    if item is None or item.get("returncode") != 0:
        raise AgentError(INVENTORY_MISSING, "The latest inventory has no partition list; refresh it.",
                         {"label": PARTITIONS_LABEL})
    records = []
    for disk in _block_devices(item.get("stdout"), PARTITIONS_LABEL):
        if _text(disk, "type") != "disk":
            continue
        record = _windows_record(_path(disk), _partitions(disk))
        if record is not None:
            records.append(record)
    return records


def parse_secure_boot(inventory: dict) -> str:
    """'on', 'off', or 'unknown' (missing probe, failed probe, or anything other than 1 or 0)."""
    item = inventory_results(inventory).get(SECURE_BOOT_LABEL) or {}
    stdout = item.get("stdout") if item.get("returncode") == 0 else None
    return SECURE_BOOT_STATES.get(stdout.strip(), "unknown") if isinstance(stdout, str) else "unknown"


def dual_boot_entries(install: dict, records: list[dict]) -> list[dict]:
    """GRUB menu entries for every UEFI Windows on a disk other than the install disk.

    Empty with dual_boot 'off' or a BIOS install. Windows in BIOS mode is never an entry.
    """
    if install.get("dual_boot", "auto") != "auto" or install.get("boot_mode") != "uefi":
        return []
    return [
        {"os": "windows", "disk": record["disk"], "esp_uuid": record["esp_uuid"], "loader": WINDOWS_LOADER}
        for record in sorted(records, key=lambda item: item["disk"])
        if record["os"] == "windows" and record["disk"] != install.get("disk")
    ]
