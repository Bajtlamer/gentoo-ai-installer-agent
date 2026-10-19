"""The profile registry: which provision profiles exist, when each applies, and the programs each one generates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.hardware_profile import hardware_profile
from gentoo_agent.core.provision import PROFILE_NVIDIA, validated_provision
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


def _nvidia_applicable(hardware: dict) -> str | None:
    count = sum(1 for gpu in hardware.get("gpus") or [] if gpu.get("vendor") == "nvidia")
    if count != 1:
        return f"Expected exactly one NVIDIA GPU on the target, found {count}."
    return None


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
