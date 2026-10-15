"""Hardware summary for discovery. Plan 4 extends this with CPU, GPU, NIC, and module data."""

from __future__ import annotations

import hashlib

from gentoo_agent.core.disks import identity_strength, parse_disk_identities
from gentoo_agent.core.install_plan import canonical


def summarize_inventory(inventory: dict) -> dict:
    results = {item.get("label"): item for item in inventory.get("results", [])}
    boot = str(results.get("boot_mode", {}).get("stdout", "")).strip()
    identity = results.get("disk_identity") or {}
    disks = parse_disk_identities(identity.get("stdout", "")) if identity.get("returncode") == 0 else []
    return {
        "target": inventory.get("target"),
        "collected_at": inventory.get("collected_at"),
        "inventory_hash": hashlib.sha256(canonical(inventory)).hexdigest(),
        "boot_mode": "uefi" if boot == "uefi" else "bios",
        "disks": [{**disk, "strength": identity_strength(disk)} for disk in disks],
    }
