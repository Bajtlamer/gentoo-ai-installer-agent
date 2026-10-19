"""Parsers for the provision inventory: the GPU, CPU, memory, kernel, and Portage facts of a running system.

Pure functions. Every value comes from the target and is untrusted: it must match a strict format or the
inventory counts as unusable (INVENTORY_MISSING). Nothing unchecked reaches a program or the approval dialog.
"""

from __future__ import annotations

import re

from gentoo_agent.core.disks import inventory_results
from gentoo_agent.core.errors import INVENTORY_MISSING, AgentError
from gentoo_agent.core.identity import IDENTITY_LABEL, parse_identity


VENDORS = {"10de": "nvidia"}
VENDOR_NAMES = {"10de": "NVIDIA"}
# One line per PCI device from the gpu command: address, class, vendor, device (hex as sysfs prints it).
SYSFS_DEVICE = re.compile(
    r"(?P<address>[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]) 0x(?P<class>[0-9a-f]{6}) "
    r"0x(?P<vendor>[0-9a-f]{4}) 0x(?P<device>[0-9a-f]{4})"
)
DISPLAY_BASE_CLASS = "03"
KERNEL_RELEASE = re.compile(r"[0-9A-Za-z._+-]{1,64}")
TEXT_LIMIT = 120


def _unusable(label: str) -> AgentError:
    return AgentError(INVENTORY_MISSING, f"The provision inventory has no usable {label} result; collect it again.",
                      {"label": label})


def _text(value: str, label: str) -> str:
    if not value.isprintable() or len(value) > TEXT_LIMIT:
        raise _unusable(label)
    return value


def parse_gpus(stdout: object) -> list[dict]:
    """Display devices (PCI base class 03) with the vendor (nvidia or other), PCI id, and a name built here."""
    if not isinstance(stdout, str):
        raise _unusable("gpu")
    gpus = []
    for line in stdout.splitlines():
        match = SYSFS_DEVICE.fullmatch(line)
        if match is None or match["class"][:2] != DISPLAY_BASE_CLASS:
            continue
        vendor = match["vendor"]
        gpus.append({"vendor": VENDORS.get(vendor, "other"),
                     "pci_id": f"{vendor}:{match['device']}",
                     "name": f"{VENDOR_NAMES.get(vendor, 'Unknown vendor')} display controller"})
    return gpus


def parse_cpu(stdout: object) -> dict:
    if not isinstance(stdout, str):
        raise _unusable("cpu")
    threads = re.search(r"^threads=(\d{1,4})$", stdout, re.MULTILINE)
    if threads is None or int(threads[1]) < 1:
        raise _unusable("cpu")
    model = re.search(r"^Model name:\s+(.+)$", stdout, re.MULTILINE)
    return {"threads": int(threads[1]), "model": _text(model[1].strip(), "cpu") if model else "unknown"}


def parse_memory(stdout: object) -> int:
    """MemTotal in KiB."""
    if not isinstance(stdout, str):
        raise _unusable("memory")
    total = re.search(r"^MemTotal:\s+(\d{1,12}) kB$", stdout, re.MULTILINE)
    if total is None or int(total[1]) < 1:
        raise _unusable("memory")
    return int(total[1])


def make_conf_overrides(stdout: object) -> list[str]:
    """Names of the Portage variables the user already sets outside the plan's own block."""
    if not isinstance(stdout, str):
        return []
    return sorted(set(re.findall(r"^(MAKEOPTS|EMERGE_DEFAULT_OPTS)=", stdout, re.MULTILINE)))


def summarize_provision_inventory(inventory: dict) -> dict:
    results = inventory_results(inventory)

    def stdout(label: str) -> object:
        item = results.get(label)
        if item is None or item.get("returncode") != 0:
            raise _unusable(label)
        return item.get("stdout")

    kernel = str(stdout("kernel")).strip()
    if not KERNEL_RELEASE.fullmatch(kernel):
        raise _unusable("kernel")
    memory_kib = parse_memory(stdout("memory"))
    conf = results.get("portage_conf") or {}
    return {
        "collected_at": inventory.get("collected_at"),
        "identity": parse_identity(stdout(IDENTITY_LABEL)),
        "kernel": kernel,
        "cpu": parse_cpu(stdout("cpu")),
        "memory_kib": memory_kib,
        "memory_gib": memory_kib // (1024 * 1024),
        "gpus": parse_gpus(stdout("gpu")),
        "portage_overrides": make_conf_overrides(conf.get("stdout")) if conf.get("returncode") == 0 else [],
    }
