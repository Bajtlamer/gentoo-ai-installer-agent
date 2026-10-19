"""Validated settings of a provision plan and the Portage tuning rule."""

from __future__ import annotations

import re

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError


PROFILE_NVIDIA = "nvidia-driver"
LICENSE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,63}")
MAX_LICENSES = 8
TUNING_KEYS = ("jobs", "emerge_jobs", "load_average")
GIB_IN_KIB = 1024 * 1024


def validated_provision(provision: object) -> dict:
    """The profile and the accepted licenses; anything else in the input is dropped."""
    if not isinstance(provision, dict):
        raise AgentError(CONFIG_INVALID, "The provision settings must be an object.", {"field": "provision"})
    if provision.get("profile") != PROFILE_NVIDIA:
        raise AgentError(CONFIG_INVALID, f"Unknown provision profile: {provision.get('profile')!r}.", {"field": "profile"})
    licenses = provision.get("accept_licenses")
    if (
        not isinstance(licenses, list)
        or not 1 <= len(licenses) <= MAX_LICENSES
        or not all(isinstance(item, str) and LICENSE.fullmatch(item) for item in licenses)
        or len(set(licenses)) != len(licenses)
    ):
        raise AgentError(
            CONFIG_INVALID,
            f"accept_licenses must be 1 to {MAX_LICENSES} distinct license names (letters, digits, +, _, ., -).",
            {"field": "accept_licenses"},
        )
    return {"profile": provision["profile"], "accept_licenses": list(licenses)}


def derive_tuning(threads: int, mem_kib: int) -> dict:
    """Portage parallelism from the CPU threads and the memory.

    MAKEOPTS jobs: the smaller of the thread count and the RAM in GiB divided by 2 (the rule of thumb of the
    Gentoo Handbook for MAKEOPTS; confirmed against the Handbook in Task 13 of the plan). Parallel emerge jobs
    stay small because every one of them runs its own make; the load average keeps the machine busy without
    swamping it.
    """
    mem_gib = mem_kib // GIB_IN_KIB
    return {
        "jobs": max(1, min(threads, mem_gib // 2)),
        "emerge_jobs": max(1, min(4, threads // 8)),
        "load_average": threads,
    }


def validated_tuning(tuning: object) -> dict:
    if not isinstance(tuning, dict) or not all(
        isinstance(tuning.get(key), int) and not isinstance(tuning.get(key), bool) and 1 <= tuning[key] <= 4096
        for key in TUNING_KEYS
    ):
        raise AgentError(CONFIG_INVALID, "The Portage tuning values must be integers from 1 to 4096.", {"field": "tuning"})
    return {key: tuning[key] for key in TUNING_KEYS}
