"""Shared test helpers."""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


@contextmanager
def temp_home():
    """Point GENTOO_AGENT_HOME at a fresh temporary directory for the duration of the block."""
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GENTOO_AGENT_HOME": tmp}):
        yield Path(tmp).resolve()


def storage_result(*disks: str) -> dict:
    """A storage inventory result that lists the given disk paths."""
    lines = ["NAME PATH SIZE TYPE"] + [f"{path.rsplit('/', 1)[-1]} {path} 40G disk" for path in disks]
    return {"label": "storage", "returncode": 0, "stdout": "\n".join(lines) + "\n"}


def fresh_inventory(*results: dict) -> dict:
    """An inventory collected now, so plan creation accepts its age."""
    from gentoo_agent.core.config import now

    return {"target": "gentoo", "collected_at": now(), "results": list(results)}


def identity_result(machine: str = "x86_64") -> dict:
    """The identity inventory result as the live system prints it (uname -m in the arch= line)."""
    return {"label": "identity", "returncode": 0,
            "stdout": f"hostname=livecd\n\narch={machine}\n\nkernel=6.18.54-gentoo-dist\n"}


ESP_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MSR_TYPE = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
BASIC_DATA_TYPE = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
LINUX_TYPE = "0fc63daf-8483-4772-8e79-3d69d8477de4"


def partition(disk: str, number: int, parttype: str, fstype: str | None = None, uuid: str | None = None,
              partuuid: str | None = None) -> dict:
    """One partition as lsblk -J prints it in the partitions inventory."""
    path = f"{disk}{'p' if disk[-1:].isdigit() else ''}{number}"
    return {"path": path, "type": "part", "pkname": disk.rsplit("/", 1)[-1], "parttype": parttype,
            "parttypename": None, "fstype": fstype, "uuid": uuid, "partuuid": partuuid, "size": 104857600,
            "label": None}


def windows_disk(path: str = "/dev/sda", esp_uuid: str = "ABCD-1234") -> dict:
    """A GPT disk with Windows in UEFI mode: ESP, Microsoft reserved, and an NTFS basic data partition."""
    return {"path": path, "type": "disk", "pkname": None, "parttype": None, "fstype": None, "uuid": None,
            "partuuid": None, "size": 256060514304, "label": None, "children": [
                partition(path, 1, ESP_TYPE, "vfat", esp_uuid, "6b8c1a2e-0d4f-4b6a-9c3e-1f2a3b4c5d6e"),
                partition(path, 2, MSR_TYPE),
                partition(path, 3, BASIC_DATA_TYPE, "ntfs", "01D9C2A1B2C3D4E5", "7c9d2b3f-1e5a-4c7b-8d4f-2a3b4c5d6e7f"),
            ]}


def windows_bios_disk(path: str = "/dev/sda") -> dict:
    """An MBR disk with Windows installed in BIOS mode: NTFS partitions and no EFI system partition."""
    return {"path": path, "type": "disk", "pkname": None, "parttype": None, "fstype": None, "uuid": None,
            "partuuid": None, "size": 256060514304, "label": None, "children": [
                partition(path, 1, "0x7", "ntfs", "4A1B2C3D4E5F6071", "1a2b3c4d-01"),
                partition(path, 2, "0x7", "ntfs", "5B2C3D4E5F607182", "1a2b3c4d-02"),
            ]}


def linux_disk(path: str = "/dev/sda") -> dict:
    """A GPT disk with a Linux system: ESP and ext4, no Microsoft partitions."""
    return {"path": path, "type": "disk", "pkname": None, "parttype": None, "fstype": None, "uuid": None,
            "partuuid": None, "size": 256060514304, "label": None, "children": [
                partition(path, 1, ESP_TYPE, "vfat", "1234-ABCD", "8d0e3c4a-2f6b-4d8c-9e5a-3b4c5d6e7f80"),
                partition(path, 2, LINUX_TYPE, "ext4", "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
                          "9e1f4d5b-3a7c-4e9d-8f6b-4c5d6e7f8091"),
            ]}


def partitions_result(*disks) -> dict:
    """The partitions inventory result; a string argument is an empty disk with that path."""
    devices = [disk if isinstance(disk, dict) else
               {"path": disk, "type": "disk", "pkname": None, "parttype": None, "fstype": None, "uuid": None,
                "partuuid": None, "size": 42949672960, "label": None}
               for disk in disks]
    return {"label": "partitions", "returncode": 0, "stdout": json.dumps({"blockdevices": devices})}


def secure_boot_result(stdout: str = "0") -> dict:
    """The secure_boot inventory result: 1, 0, or unknown."""
    return {"label": "secure_boot", "returncode": 0, "stdout": stdout}


REPO = Path(__file__).resolve().parents[1]
DISPOSABLE_TARGET = {
    "name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
    "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True,
}


def uefi_install() -> dict:
    """The uefi_gpt_user_sudo golden config (amd64, /dev/nvme0n1) with full erase authorization."""
    data = json.loads((REPO / "tests" / "golden" / "configs" / "uefi_gpt_user_sudo.json").read_text(encoding="utf-8"))
    data.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True})
    return data


def dual_boot_inventory(*, nvme: dict | None = None, sda: dict | None = None, secure_boot: str = "0",
                        sda_serial: str = "W1") -> dict:
    """A fresh UEFI amd64 inventory with /dev/nvme0n1 (install disk) and /dev/sda.

    nvme and sda are partitions entries (windows_disk(), linux_disk(), ...); None means an empty disk.
    """
    devices = [
        {"path": "/dev/nvme0n1", "type": "disk", "size": 512110190592, "model": "NVMe", "serial": "N1", "wwn": None},
        {"path": "/dev/sda", "type": "disk", "size": 256060514304, "model": "WinDisk", "serial": sda_serial,
         "wwn": None},
    ]
    return fresh_inventory(
        {"label": "boot_mode", "returncode": 0, "stdout": "uefi"},
        storage_result("/dev/nvme0n1", "/dev/sda"),
        {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": devices})},
        identity_result("x86_64"),
        partitions_result(nvme or "/dev/nvme0n1", sda or "/dev/sda"),
        secure_boot_result(secure_boot),
    )


def save_dual_boot_plan(install: dict | None = None, **inventory_options) -> dict:
    """Create and save a plan from uefi_install() and dual_boot_inventory(**inventory_options). Use inside temp_home()."""
    from gentoo_agent.core import install_plan, phases

    project = json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8"))
    with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAA test"):
        plan = install_plan.create_install_plan(DISPOSABLE_TARGET, install or uefi_install(), project,
                                                dual_boot_inventory(**inventory_options))
        return install_plan.save_install_plan(plan)
