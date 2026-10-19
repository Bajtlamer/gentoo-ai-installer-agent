"""The hardware profile: what the first inventory says the machine is, as data a profile can decide on."""

from __future__ import annotations

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError

VIDEO_CARDS_BY_VENDOR = {"nvidia": ("nvidia",), "amd": ("amdgpu", "radeonsi"), "intel": ("intel",)}

# QEMU/Bochs, VMware, virtio, Red Hat.
VIRTUAL_VENDOR_IDS = frozenset({"1234", "15ad", "1af4", "1b36"})


def video_cards(gpus: list[dict]) -> list[str]:
    """VIDEO_CARDS values for the GPUs, sorted and without duplicates; an unknown vendor raises CONFIG_INVALID.

    No GPU gives an empty list. The vendor map is fixed here, never taken from the target.
    """
    cards: set[str] = set()
    for gpu in gpus:
        mapped = VIDEO_CARDS_BY_VENDOR.get(gpu.get("vendor"))
        if mapped is None:
            raise AgentError(CONFIG_INVALID, "A display device has a vendor with no known VIDEO_CARDS value.",
                             {"pci_id": gpu.get("pci_id")})
        cards.update(mapped)
    return sorted(cards)


def hardware_profile(facts: dict) -> dict:
    """facts is the result of summarize_provision_inventory."""
    gpus = list(facts.get("gpus") or [])
    unknown = sorted({gpu.get("vendor_id") for gpu in gpus if gpu.get("vendor") == "other"})
    return {
        "architecture": facts.get("architecture"),
        "threads": facts["cpu"]["threads"],
        "memory_gib": facts["memory_gib"],
        "gpus": gpus,
        "gpu_vendors": sorted({gpu["vendor"] for gpu in gpus}),
        "video_cards": None if unknown else video_cards(gpus),
        "unknown_vendors": unknown,
        "virtual": bool(gpus) and all(gpu.get("vendor_id") in VIRTUAL_VENDOR_IDS for gpu in gpus),
    }
