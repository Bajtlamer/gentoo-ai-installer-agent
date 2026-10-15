"""Partition layout derived from disk, boot mode, and swap size."""

from __future__ import annotations

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError


EFI_SIZE_MIB = 512


def partition_device(disk: str, number: int) -> str:
    """Partition path for both /dev/sda and /dev/nvme0n1 styles."""
    return f"{disk}{'p' if disk[-1:].isdigit() else ''}{number}"


def build_layout(disk: str, boot_mode: str, swap_mb: int, root_filesystem: str = "ext4") -> tuple[str, list[dict]]:
    if boot_mode not in {"uefi", "bios"}:
        raise AgentError(CONFIG_INVALID, "boot_mode must be 'uefi' or 'bios'", {"field": "boot_mode"})
    if not isinstance(swap_mb, int) or isinstance(swap_mb, bool) or swap_mb < 0:
        raise AgentError(CONFIG_INVALID, "swap_mb must be a non-negative integer", {"field": "swap_mb"})
    entries: list[dict] = []
    if boot_mode == "uefi":
        entries.append({"size_mib": EFI_SIZE_MIB, "type": "efi-system", "filesystem": "vfat", "mount": "/boot"})
    entries.append({"size_mib": swap_mb, "type": "linux-swap", "mount": None})
    entries.append({"size": "remaining", "type": "linux-root", "filesystem": root_filesystem, "mount": "/"})
    layout = [{"device": partition_device(disk, number), **entry} for number, entry in enumerate(entries, start=1)]
    return ("gpt" if boot_mode == "uefi" else "mbr"), layout
