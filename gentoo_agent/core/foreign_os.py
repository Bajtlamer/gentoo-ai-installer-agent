"""Other operating systems on the target's disks, and the Secure Boot state.

Pure: reads a saved inventory and never contacts the target. Every value in
the inventory is untrusted. A value that does not match its strict format
makes the inventory unusable (INVENTORY_MISSING, refresh it); it never reaches
a generated program or the approval dialog.
"""

from __future__ import annotations

import re

from gentoo_agent.core.disks import IDENTITY_LABEL, _block_devices, inventory_results
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
# Filesystems (lowercased lsblk FSTYPE) that only Windows writes, on any partition table or a whole disk.
WINDOWS_FILESYSTEMS = ("ntfs", "bitlocker", "refs")
# Filesystems Windows can boot from (NTFS, or NTFS inside BitLocker); ReFS is data only.
WINDOWS_BOOT_FILESYSTEMS = ("ntfs", "bitlocker")
# Partition types only Windows uses (lowercased PARTTYPE), with their evidence word.
WINDOWS_PARTITION_TYPES = {
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3": "ldm-metadata",
    "af9b60a0-1431-4f62-bc68-3311714a69ad": "ldm-data",
    "e75caf8f-f680-4cee-afa3-b001e56efc2d": "storage-spaces",
    "0x42": "dynamic-disk",
}
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


def _scan(disk_path: str, disk_fstype: str, partitions: list[dict]) -> dict:
    """What one disk holds: its first EFI system partition and every Windows marker, with evidence."""
    if partitions and not any(_text(part, "parttype") for part in partitions):
        # lsblk printed partitions without any type (no udev metadata): Windows could be hidden there.
        # A missing filesystem alone is normal, for example a fresh layout before mkfs. "All", not "any":
        # udev metadata goes missing per disk, and a valid partition table gives every partition a type.
        raise _unusable(f"The partitions inventory has no partition type for {disk_path}")
    scan: dict = {"disk": disk_path, "esp": None, "esp_path": "", "gpt_esp": False, "esp_type": False,
                  "markers": [], "strict": False, "mbr": False, "system": False}
    if disk_fstype in WINDOWS_FILESYSTEMS:
        # A filesystem on the whole disk, without a partition table.
        scan["markers"].append(f"{disk_fstype} {disk_path}")
    for part in partitions:
        path = _path(part)
        parttype = _text(part, "parttype").lower()
        fstype = _text(part, "fstype").lower()
        if parttype in (ESP_TYPE, MBR_ESP_TYPE):
            scan["esp_type"] = True
            if fstype == "vfat" and scan["esp"] is None:
                scan.update(esp=part, esp_path=path, gpt_esp=parttype == ESP_TYPE)
            continue
        if parttype == MSR_TYPE:
            scan["markers"].append(f"msr {path}")
            scan.update(strict=True, system=True)
        elif parttype in WINDOWS_PARTITION_TYPES:
            scan["markers"].append(f"{WINDOWS_PARTITION_TYPES[parttype]} {path}")
        elif fstype in WINDOWS_FILESYSTEMS:
            scan["markers"].append(f"{fstype} {path}")
            if fstype in WINDOWS_BOOT_FILESYSTEMS:
                scan["strict"] = scan["strict"] or (parttype == BASIC_DATA_TYPE and fstype == "ntfs")
                # Looks like a Windows system disk (GPT): only such a disk can boot from another disk's ESP.
                scan["system"] = scan["system"] or parttype == BASIC_DATA_TYPE
                scan["mbr"] = scan["mbr"] or bool(MBR_TYPE.fullmatch(parttype))
    return scan


def _record(scan: dict) -> dict | None:
    """The record for a disk with Windows markers of its own, or None."""
    if not scan["markers"]:
        return None
    disk_path, esp_path = scan["disk"], scan["esp_path"]
    if scan["gpt_esp"] and scan["strict"]:
        # The strict rule: the only one that builds a GRUB menu entry.
        esp_uuid = _text(scan["esp"], "uuid")
        if not ESP_UUID.fullmatch(esp_uuid):
            raise _unusable(f"The EFI system partition {esp_path} has an unexpected UUID")
        return _checked({"disk": disk_path, "os": "windows", "esp_uuid": esp_uuid,
                         "evidence": [f"esp vfat {esp_path}", *scan["markers"]]})
    if scan["mbr"] and not scan["esp_type"]:
        return _checked({"disk": disk_path, "os": "windows-bios", "evidence": list(scan["markers"])})
    evidence = ([f"esp vfat {esp_path}"] if scan["esp"] is not None else []) + scan["markers"]
    return _checked({"disk": disk_path, "os": "windows-data", "evidence": evidence})


def _removable_disks(inventory: dict) -> set[str]:
    """Paths of removable or USB disks from the disk_identity result.

    Untrusted and optional: a missing, failed, or malformed result, a missing field, or a value of an
    unexpected type counts as not removable (so the disk stays protected).
    """
    item = inventory_results(inventory).get(IDENTITY_LABEL)
    if item is None or item.get("returncode") != 0:
        return set()
    try:
        devices = _block_devices(item.get("stdout"), IDENTITY_LABEL)
    except AgentError:
        return set()
    removable = set()
    for device in devices:
        path, tran, rm = device.get("path"), device.get("tran"), device.get("rm")
        if not isinstance(path, str):
            continue
        if rm is True or rm == 1 or rm == "1" or (isinstance(tran, str) and tran.strip().lower() == "usb"):
            removable.add(path)
    return removable


def detect_foreign_os(inventory: dict) -> list[dict]:
    """One record per disk that holds Windows data or boot files, in inventory order.

    "windows": the strict rule (a vfat EFI system partition with the GPT type, and a Microsoft
    reserved partition or an NTFS basic data partition on the same disk); only these records carry
    esp_uuid and become GRUB menu entries. "windows-bios": NTFS or BitLocker on MBR and no EFI system
    partition. "windows-data": any other Windows marker (a Microsoft reserved partition; NTFS,
    BitLocker, or ReFS on a partition or a whole disk; an LDM, Storage Spaces, or MBR dynamic disk
    partition type), and every fixed disk with a vfat EFI system partition while a fixed disk that looks
    like a Windows system disk (GPT, with a Microsoft reserved partition or an NTFS or BitLocker basic
    data partition) has no EFI system partition and no strict "windows" record exists anywhere, since
    that Windows may boot from it. Removable and USB disks never take part in that propagation.
    Every record protects its disk from an ordinary erase.
    """
    item = inventory_results(inventory).get(PARTITIONS_LABEL)
    if item is None or item.get("returncode") != 0:
        raise AgentError(INVENTORY_MISSING, "The latest inventory has no partition list; refresh it.",
                         {"label": PARTITIONS_LABEL})
    devices = _block_devices(item.get("stdout"), PARTITIONS_LABEL)
    disks = [disk for disk in devices if _text(disk, "type") == "disk"]
    # lsblk prints partitions nested under their disk or, on some systems, flat with PKNAME naming the disk.
    owned = {_path(disk): {_path(part): part for part in _partitions(disk)} for disk in disks}
    for part in devices:
        if _text(part, "type") != "part":
            continue
        parent = f"/dev/{_text(part, 'pkname')}"
        if parent not in owned:
            # A partition of an unknown disk could hide Windows.
            raise _unusable(f"The partitions inventory lists {_path(part)} without a known disk")
        owned[parent].setdefault(_path(part), part)
    scans = [_scan(_path(disk), _text(disk, "fstype").lower(), list(owned[_path(disk)].values())) for disk in disks]
    records = {scan["disk"]: _record(scan) for scan in scans}
    if any(record is not None and record["os"] == "windows" for record in records.values()):
        # A complete UEFI Windows boots from its own ESP; no other ESP is its boot ESP.
        return [record for record in records.values() if record is not None]
    removable = _removable_disks(inventory)
    without_esp = [scan["disk"] for scan in scans
                   if scan["system"] and scan["esp"] is None and scan["disk"] not in removable]
    for scan in scans:
        users = [disk for disk in without_esp if disk != scan["disk"]]
        if scan["esp"] is not None and records[scan["disk"]] is None and users and scan["disk"] not in removable:
            records[scan["disk"]] = _checked({
                "disk": scan["disk"], "os": "windows-data",
                "evidence": [f"esp vfat {scan['esp_path']}",
                             *(f"may hold the boot files of Windows on {disk}" for disk in users)],
            })
    return [record for record in records.values() if record is not None]


OS_DESCRIPTIONS = {
    "windows": "Windows (UEFI)",
    "windows-bios": "Windows (BIOS mode)",
    "windows-data": "Windows data or boot files",
}


def describe_os(os_name: str) -> str:
    """Human words for a record's os value."""
    return OS_DESCRIPTIONS.get(os_name, os_name)


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
