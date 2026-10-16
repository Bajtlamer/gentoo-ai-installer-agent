"""Supported CPU architectures: the one table the generators, validation, and discovery read.

Adding an architecture means one entry here plus a golden fixture. Gentoo
names (amd64, arm64) are the configuration values; machine is what uname -m
prints on the live system.
"""

from __future__ import annotations

from gentoo_agent.core.errors import CONFIG_INVALID, INVENTORY_MISSING, AgentError


ARCHITECTURES = {
    "amd64": {"machine": "x86_64", "grub_efi_target": "x86_64-efi", "grub_efi_binary": "grubx64.efi",
              "grub_efi_fallback": "BOOTX64.EFI", "bios": True},
    "arm64": {"machine": "aarch64", "grub_efi_target": "arm64-efi", "grub_efi_binary": "grubaa64.efi",
              "grub_efi_fallback": "BOOTAA64.EFI", "bios": False},
}
MACHINES = {spec["machine"]: name for name, spec in ARCHITECTURES.items()}
STAGE_BASE = "https://distfiles.gentoo.org/releases/{arch}/autobuilds/"
STAGE_LIST = STAGE_BASE + "latest-stage3-{arch}-openrc.txt"


def architecture_spec(name: object) -> dict:
    if not isinstance(name, str) or name not in ARCHITECTURES:
        raise AgentError(CONFIG_INVALID, f"architecture must be one of: {', '.join(sorted(ARCHITECTURES))}",
                         {"field": "architecture"})
    return ARCHITECTURES[name]


def live_architecture(inventory: dict) -> str:
    """The Gentoo architecture of the live system, from the identity probe's arch= line."""
    results = {item.get("label"): item for item in inventory.get("results", []) if isinstance(item, dict)}
    item = results.get("identity") or {}
    stdout = item.get("stdout") if item.get("returncode") == 0 else None
    machine = None
    if isinstance(stdout, str):
        machine = next((line[len("arch="):].strip() for line in stdout.splitlines() if line.startswith("arch=")), None)
    if not machine:
        raise AgentError(INVENTORY_MISSING, "The live architecture is unknown; refresh the inventory.", {"label": "identity"})
    if machine not in MACHINES:
        raise AgentError(CONFIG_INVALID, f"The live system is {machine}, which this installer does not support.",
                         {"machine": machine})
    return MACHINES[machine]
