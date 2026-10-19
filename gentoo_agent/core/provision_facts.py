"""Parsers for the provision inventory: the GPU, CPU, memory, kernel, and Portage facts of a running system.

Pure functions. Every value comes from the target and is untrusted: it must match a strict format or the
inventory counts as unusable (INVENTORY_MISSING). Nothing unchecked reaches a program or the approval dialog.
"""

from __future__ import annotations

import re

from gentoo_agent.core.disks import inventory_results
from gentoo_agent.core.errors import INVENTORY_MISSING, AgentError
from gentoo_agent.core.identity import IDENTITY_LABEL, parse_identity
from gentoo_agent.core.provision import GIB_IN_KIB


VENDORS = {"10de": "nvidia", "1002": "amd", "8086": "intel"}
VENDOR_NAMES = {"10de": "NVIDIA", "1002": "AMD", "8086": "Intel"}
# One line per PCI device from the gpu command: address, class, vendor, device (hex as sysfs prints it).
SYSFS_DEVICE = re.compile(
    r"(?P<address>[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]) 0x(?P<class>[0-9a-f]{6}) "
    r"0x(?P<vendor>[0-9a-f]{4}) 0x(?P<device>[0-9a-f]{4})"
)
DISPLAY_BASE_CLASS = "03"
KERNEL_RELEASE = re.compile(r"[0-9A-Za-z._+-]{1,64}")
TEXT_LIMIT = 120
ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64"}
# A profile of an overlay is listed as repo:path; the name still has to be strict because it is shown and compared.
PROFILE_NAME = r"(?:[a-z0-9][a-z0-9._+-]{0,63}:)?[a-z0-9][a-z0-9/._+-]{0,127}"
PROFILE_NAME_LINE = re.compile(PROFILE_NAME)
PROFILE_LIST_LINE = re.compile(rf"\s*\[(\d+)\]\s+({PROFILE_NAME})\s+\((stable|dev|exp)\)(?: (\*))?")
PROFILE_HEADER = "Current /etc/portage/make.profile symlink:"
PROFILES_HEADER = "Available profile symlink targets:"
SYNC_URI_LINE = re.compile(r"sync-uri\s*=\s*(.*)")
HTTPS_URI = re.compile(r"https://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]{1,200}")


def _unusable(label: str) -> AgentError:
    return AgentError(INVENTORY_MISSING, f"The provision inventory has no usable {label} result; collect it again.",
                      {"label": label})


def _text(value: str, label: str) -> str:
    if not value.isprintable() or len(value) > TEXT_LIMIT:
        raise _unusable(label)
    return value


def parse_gpus(stdout: object) -> list[dict]:
    """Display devices (PCI base class 03) with the vendor (nvidia, amd, intel, or other), PCI id, and a name built here."""
    if not isinstance(stdout, str):
        raise _unusable("gpu")
    gpus = []
    for line in stdout.splitlines():
        match = SYSFS_DEVICE.fullmatch(line)
        if match is None or match["class"][:2] != DISPLAY_BASE_CLASS:
            continue
        vendor = match["vendor"]
        gpus.append({"vendor": VENDORS.get(vendor, "other"),
                     "pci_id": f"{vendor}:{match['device']}", "vendor_id": vendor,
                     "name": f"{VENDOR_NAMES.get(vendor, 'Unknown vendor')} display controller"})
    return gpus


def parse_arch(stdout: object) -> str:
    if not isinstance(stdout, str) or stdout.strip() not in ARCHITECTURES:
        raise _unusable("arch")
    return ARCHITECTURES[stdout.strip()]


def parse_portage_profile(stdout: object) -> str:
    """The profile name from eselect profile show: the one line that is not the header."""
    if not isinstance(stdout, str):
        raise _unusable("portage_profile")
    lines = [line.strip() for line in stdout.splitlines() if line.strip() and line.strip() != PROFILE_HEADER]
    if len(lines) != 1 or not PROFILE_NAME_LINE.fullmatch(lines[0]):
        raise _unusable("portage_profile")
    return lines[0]


def parse_portage_profiles(stdout: object) -> list[dict]:
    if not isinstance(stdout, str):
        raise _unusable("portage_profiles")
    profiles = []
    for line in stdout.splitlines():
        if not line.strip() or line.strip() == PROFILES_HEADER:
            continue
        match = PROFILE_LIST_LINE.fullmatch(line)
        if match is None:
            raise _unusable("portage_profiles")
        profiles.append({"index": int(match[1]), "name": match[2], "stability": match[3],
                         "current": match[4] is not None})
    if not profiles:
        raise _unusable("portage_profiles")
    return profiles


def parse_binrepos(stdout: object) -> dict:
    """The https sync-uri values of binrepos.conf; comments, sections, and other keys are ignored."""
    if not isinstance(stdout, str):
        raise _unusable("binrepos")
    uris = []
    for line in stdout.splitlines():
        match = SYNC_URI_LINE.fullmatch(line.strip())
        if match is None:
            continue
        if not HTTPS_URI.fullmatch(match[1].strip()):
            raise _unusable("binrepos")
        uris.append(match[1].strip())
    return {"uris": uris}


def parse_cpu(stdout: object) -> dict:
    # The first "Model name:" line wins: lscpu on hybrid and ARM CPUs can print several.
    if not isinstance(stdout, str):
        raise _unusable("cpu")
    threads = re.search(r"^threads=(\d{1,4})$", stdout, re.MULTILINE)
    if threads is None or int(threads[1]) < 1:
        raise _unusable("cpu")
    model = re.search(r"^Model name:[ \t]+(.+)$", stdout, re.MULTILINE)
    return {"threads": int(threads[1]), "model": _text(model[1].strip(), "cpu") if model else "unknown"}


def parse_memory(stdout: object) -> int:
    """MemTotal in KiB."""
    if not isinstance(stdout, str):
        raise _unusable("memory")
    total = re.search(r"^MemTotal:[ \t]+(\d{1,12}) kB$", stdout, re.MULTILINE)
    if total is None or int(total[1]) < 1:
        raise _unusable("memory")
    return int(total[1])


def make_conf_overrides(stdout: object) -> list[str]:
    """Names of the Portage variables the user already sets outside the plan's own block.

    Matches the same plain assignments as the portage_conf sed command: not "export" and not indented lines.
    """
    if not isinstance(stdout, str):
        return []
    return sorted(set(re.findall(r"^(MAKEOPTS|EMERGE_DEFAULT_OPTS|VIDEO_CARDS)=", stdout, re.MULTILINE)))


def summarize_provision_inventory(inventory: dict) -> dict:
    results = inventory_results(inventory)

    def stdout(label: str) -> object:
        item = results.get(label)
        if item is None or item.get("returncode") != 0:
            raise _unusable(label)
        return item.get("stdout")

    raw = stdout("kernel")
    if not isinstance(raw, str):
        raise _unusable("kernel")
    kernel = raw.strip()
    if not KERNEL_RELEASE.fullmatch(kernel):
        raise _unusable("kernel")
    memory_kib = parse_memory(stdout("memory"))
    conf = results.get("portage_conf") or {}

    def optional(label: str, parser):
        # None when the inventory predates the command; unusable when the label is present but failed.
        return parser(stdout(label)) if label in results else None

    return {
        "collected_at": inventory.get("collected_at"),
        "identity": parse_identity(stdout(IDENTITY_LABEL)),
        "kernel": kernel,
        "cpu": parse_cpu(stdout("cpu")),
        "memory_kib": memory_kib,
        # memory_gib is the floored value in GiB.
        "memory_gib": memory_kib // GIB_IN_KIB,
        "gpus": parse_gpus(stdout("gpu")),
        "portage_overrides": make_conf_overrides(conf.get("stdout")) if conf.get("returncode") == 0 else [],
        "architecture": optional("arch", parse_arch),
        "portage_profile": optional("portage_profile", parse_portage_profile),
        "portage_profiles": optional("portage_profiles", parse_portage_profiles),
        "binrepos": optional("binrepos", parse_binrepos),
    }
