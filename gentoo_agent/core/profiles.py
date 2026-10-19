"""The profile registry: which provision profiles exist, when each applies, and the programs each one generates."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from gentoo_agent.core import desktop_phases
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.hardware_profile import hardware_profile
from gentoo_agent.core.provision import (PROFILE_DESKTOP, PROFILE_NVIDIA, derive_tuning, validated_desktop,
                                           validated_provision, validated_tuning)
from gentoo_agent.core.provision_facts import summarize_provision_inventory
from gentoo_agent.core.provision_phases import PHASE_FLAGS, PHASES, check_command_for, command_for


@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    applicable: Callable[[dict], str | None]
    validate: Callable[[object], dict]
    phases: tuple[str, ...]
    flags: dict[str, tuple[bool, bool]]
    command_for: Callable[[str, dict, dict], str]
    check_command_for: Callable[[str, dict, dict], str]
    # (validated settings, provision facts, hardware profile) -> the plan's "provision" block
    build: Callable[[dict, dict, dict], dict]
    # stored plan -> the profile-specific lines of the approval dialog
    summary: Callable[[dict], list[str]]


def _nvidia_applicable(hardware: dict) -> str | None:
    count = sum(1 for gpu in hardware.get("gpus") or [] if gpu.get("vendor") == "nvidia")
    if count != 1:
        return f"Expected exactly one NVIDIA GPU on the target, found {count}."
    return None


def _nvidia_build(settings: dict, facts: dict, hardware: dict) -> dict:
    # applicable() already required exactly one NVIDIA GPU.
    nvidia = [gpu for gpu in facts["gpus"] if gpu["vendor"] == "nvidia"]
    return {
        **settings,
        "gpu": nvidia[0],
        "cpu": facts["cpu"],
        "memory_gib": facts["memory_gib"],
        "tuning": validated_tuning(derive_tuning(facts["cpu"]["threads"], facts["memory_kib"])),
    }


def _nvidia_summary(plan: dict) -> list[str]:
    # approval imports provision_plan, which imports this module; import it only when the summary is built.
    from gentoo_agent.core.approval import nvidia_summary
    return nvidia_summary(plan)


PLASMA_SUFFIX = "/desktop/plasma"
PLAIN_PROFILE = re.compile(r"default/linux/amd64/[0-9]+\.[0-9]+")


def plasma_profile(hardware: dict) -> tuple[str | None, str | None]:
    """(target Portage profile, refusal reason). Exactly one of them is None."""
    if hardware.get("architecture") != "amd64":
        return None, "The desktop-plasma profile supports only amd64."
    current = hardware.get("portage_profile")
    if current is None:
        return None, "The inventory has no Portage profile facts: collect the provision inventory again (refresh=true)."
    if current.endswith(PLASMA_SUFFIX) and PLAIN_PROFILE.fullmatch(current[:-len(PLASMA_SUFFIX)]):
        base = current[:-len(PLASMA_SUFFIX)]
    elif PLAIN_PROFILE.fullmatch(current):
        base = current
    else:
        # The name was parsed by a strict regex in provision_facts, so it is safe to show.
        return None, f"Only the plain OpenRC profile default/linux/amd64/<version> is supported, found {current}."
    target = base + PLASMA_SUFFIX
    offered = sum(1 for entry in hardware.get("portage_profiles") or [] if entry.get("name") == target)
    if offered == 0:
        return None, f"The Plasma profile {target} is not offered by this system."
    if offered > 1:
        return None, f"The Plasma profile {target} is offered more than once."
    return target, None


def desktop_applicable(hardware: dict) -> str | None:
    _, reason = plasma_profile(hardware)
    if reason is not None:
        return reason
    if hardware.get("video_cards") is None:
        return "A display device has an unknown vendor."
    if not hardware.get("gpus"):
        return "No display device was detected."
    return None


def desktop_build(settings: dict, facts: dict, hardware: dict) -> dict:
    # applicable() already ran plasma_profile successfully.
    target, _ = plasma_profile(hardware)
    return {
        **settings,
        "gpus": hardware["gpus"],
        "video_cards": hardware["video_cards"],
        "nvidia": "nvidia" in hardware["gpu_vendors"],
        "portage_profile": hardware["portage_profile"],
        "plasma_profile": target,
        "binrepos": hardware["binrepos"] or {"uris": []},
        "cpu": facts["cpu"],
        "memory_gib": facts["memory_gib"],
    }


def _desktop_summary(plan: dict) -> list[str]:
    # Placeholder: the real approval lines are written in the plan's Task 4.
    return ["Purpose: desktop-plasma"]


PROFILES: dict[str, Profile] = {
    PROFILE_NVIDIA: Profile(
        id=PROFILE_NVIDIA,
        title="NVIDIA proprietary driver",
        applicable=_nvidia_applicable,
        validate=validated_provision,
        phases=PHASES,
        flags=PHASE_FLAGS,
        command_for=lambda phase, target, provision: command_for(phase, target, provision),
        check_command_for=lambda phase, target, provision: check_command_for(phase, target, provision),
        build=_nvidia_build,
        summary=_nvidia_summary,
    ),
    PROFILE_DESKTOP: Profile(
        id=PROFILE_DESKTOP,
        title="KDE Plasma desktop (Wayland session, OpenRC)",
        applicable=desktop_applicable,
        validate=validated_desktop,
        phases=desktop_phases.PHASES,
        flags=desktop_phases.PHASE_FLAGS,
        command_for=lambda phase, target, provision: desktop_phases.command_for(phase, target, provision),
        check_command_for=lambda phase, target, provision: desktop_phases.check_command_for(phase, target, provision),
        build=desktop_build,
        summary=_desktop_summary,
    ),
}


def get_profile(profile_id: object) -> Profile:
    if not isinstance(profile_id, str) or profile_id not in PROFILES:
        raise AgentError(CONFIG_INVALID, f"Unknown provision profile: {profile_id!r}.", {"field": "profile"})
    return PROFILES[profile_id]


def profile_overview(hardware: dict) -> list[dict]:
    rows = []
    for profile in PROFILES.values():
        reason = profile.applicable(hardware)
        rows.append({"id": profile.id, "title": profile.title, "applicable": reason is None, "reason": reason})
    return rows


def provision_overview(inventory: dict) -> dict:
    """The provision facts plus the hardware profile and which profiles apply to it."""
    facts = summarize_provision_inventory(inventory)
    hardware = hardware_profile(facts)
    return {**facts, "hardware": hardware, "profiles": profile_overview(hardware)}
