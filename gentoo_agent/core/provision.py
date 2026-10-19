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

    MAKEOPTS jobs: the smaller of the thread count and the RAM in GiB divided by 2 (a rule of thumb for
    MAKEOPTS; to be checked against the Gentoo Handbook on the real server, not cited as a source). Parallel emerge jobs
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
    """The three Portage tuning values, each an integer from 1 to 4096; anything else in the input is dropped."""
    if not isinstance(tuning, dict):
        raise AgentError(CONFIG_INVALID, "The Portage tuning must be an object.", {"field": "tuning"})
    for key in TUNING_KEYS:
        value = tuning.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 4096:
            raise AgentError(CONFIG_INVALID, f"The Portage tuning value {key!r} must be an integer from 1 to 4096.",
                             {"field": "tuning"})
    return {key: tuning[key] for key in TUNING_KEYS}
