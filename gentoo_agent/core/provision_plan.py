"""Provision plans: the frozen, hashed description of changes to an installed, running system.

Same plan file, hash, and approval as an install plan. The plan holds the host identity instead of the
disks, the validated settings with the facts they were derived from, and every generated program.
"""

from __future__ import annotations

import hashlib

from gentoo_agent.core.config import now
from gentoo_agent.core.errors import CONFIG_INVALID, INVENTORY_MISSING, PLAN_HASH_MISMATCH, AgentError
from gentoo_agent.core.install_plan import (
    PLAN_ID_LENGTH,
    PLAN_SCHEMA_VERSION,
    TARGET_FIELDS,
    check_inventory_fresh,
    canonical,
    compute_plan_hash,
)
from gentoo_agent.core.hardware_profile import hardware_profile
from gentoo_agent.core.profiles import get_profile
from gentoo_agent.core.provision import derive_tuning, validated_tuning
from gentoo_agent.core.provision_facts import summarize_provision_inventory


def create_provision_plan(target: dict, settings: dict, inventory: dict | None) -> dict:
    if target.get("user") != "root":
        raise AgentError(CONFIG_INVALID, "Provision plans run as root; the SSH user must be root.", {"field": "user"})
    profile = get_profile(settings.get("profile") if isinstance(settings, dict) else None)
    settings = profile.validate(settings)
    if inventory is None:
        raise AgentError(INVENTORY_MISSING, "No provision inventory exists; collect it first.", {})
    check_inventory_fresh(inventory)
    facts = summarize_provision_inventory(inventory)
    nvidia = [gpu for gpu in facts["gpus"] if gpu["vendor"] == "nvidia"]
    reason = profile.applicable(hardware_profile(facts))
    if reason:
        raise AgentError(CONFIG_INVALID, reason, {"profile": profile.id, "nvidia_gpus": len(nvidia)})
    provision = {
        **settings,
        "gpu": nvidia[0],
        "cpu": facts["cpu"],
        "memory_gib": facts["memory_gib"],
        "tuning": validated_tuning(derive_tuning(facts["cpu"]["threads"], facts["memory_kib"])),
    }
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "kind": "provision",
        "target": {field: target.get(field) for field in TARGET_FIELDS},
        "identity": facts["identity"],
        "provision": provision,
        "warnings": [f"/etc/portage/make.conf already sets {name}; the plan's value replaces it."
                     for name in facts["portage_overrides"]],
        "inventory_hash": hashlib.sha256(canonical(inventory)).hexdigest(),
        "phases": [
            {
                "id": phase,
                "destructive": profile.flags[phase][0],
                "run": profile.flags[phase][1],
                "apply": profile.command_for(phase, target, provision),
                "check": profile.check_command_for(phase, target, provision),
            }
            for phase in profile.phases
        ],
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    plan["plan_id"] = plan["plan_hash"][:PLAN_ID_LENGTH]
    plan["created_at"] = now()
    return plan


def verify_provision_programs(plan: dict) -> None:
    """Require the stored phases to be exactly what the plan's settings generate.

    The approval summary is built from the settings while the runner executes the stored programs; the plan
    hash covers both but cannot tell whether they belong together.
    """
    provision = plan.get("provision")
    profile = get_profile(provision.get("profile") if isinstance(provision, dict) else None)
    phases = plan.get("phases")
    if not isinstance(phases, list) or [phase.get("id") for phase in phases] != list(profile.phases):
        raise AgentError(PLAN_HASH_MISMATCH, "The stored phases do not match the provision phases.",
                         {"plan_id": plan.get("plan_id")})
    for phase in phases:
        phase_id = phase["id"]
        destructive, run = profile.flags[phase_id]
        if phase.get("destructive") != destructive or phase.get("run") != run:
            raise AgentError(PLAN_HASH_MISMATCH, f"The stored {phase_id} phase flags do not match the plan settings.",
                             {"plan_id": plan.get("plan_id"), "phase": phase_id})
        generated = {
            "apply": profile.command_for(phase_id, plan["target"], plan["provision"]),
            "check": profile.check_command_for(phase_id, plan["target"], plan["provision"]),
        }
        for kind, expected in generated.items():
            if phase.get(kind) != expected:
                raise AgentError(PLAN_HASH_MISMATCH,
                                 f"The stored {phase_id} {kind} program does not match the plan settings.",
                                 {"plan_id": plan.get("plan_id"), "phase": phase_id})
