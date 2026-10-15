"""Hardware summary for discovery. Plan 4 extends this with CPU, GPU, NIC, and module data."""

from __future__ import annotations

import hashlib

from gentoo_agent.core.disks import identity_strength, parse_disk_identities
from gentoo_agent.core.install_plan import canonical
from gentoo_agent.core.resolve import LIVE_BOOT_MODES


def summarize_inventory(inventory: dict) -> dict:
    results = {item.get("label"): item for item in inventory.get("results", [])}
    probe = results.get("boot_mode") or {}
    # Report a boot mode only from a clean probe; anything else is "unknown", never a guess.
    stdout = probe.get("stdout") if probe.get("returncode") == 0 else None
    boot_mode = LIVE_BOOT_MODES.get(stdout, "unknown") if isinstance(stdout, str) else "unknown"
    identity = results.get("disk_identity") or {}
    disks = parse_disk_identities(identity.get("stdout", "")) if identity.get("returncode") == 0 else []
    return {
        "target": inventory.get("target"),
        "collected_at": inventory.get("collected_at"),
        "inventory_hash": hashlib.sha256(canonical(inventory)).hexdigest(),
        "boot_mode": boot_mode,
        "disks": [{**disk, "strength": identity_strength(disk)} for disk in disks],
    }
