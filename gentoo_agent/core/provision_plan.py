"""Provision plans: the frozen, hashed description of changes to an installed, running system.

Same plan file, hash, and approval as an install plan. The plan holds the host identity instead of the
disks, the validated settings with the facts they were derived from, and every generated program.
"""

from __future__ import annotations

import hashlib

from gentoo_agent.core.config import now
from gentoo_agent.core.errors import CONFIG_INVALID, INVENTORY_MISSING, AgentError
from gentoo_agent.core.install_plan import (
    PLAN_ID_LENGTH,
    PLAN_SCHEMA_VERSION,
    TARGET_FIELDS,
    _check_inventory_fresh,
    canonical,
    compute_plan_hash,
)
from gentoo_agent.core.provision import derive_tuning, validated_provision
from gentoo_agent.core.provision_facts import summarize_provision_inventory
from gentoo_agent.core.provision_phases import PHASE_FLAGS, PHASES, check_command_for, command_for


def create_provision_plan(target: dict, settings: dict, inventory: dict | None) -> dict:
    if target.get("user") != "root":
        raise AgentError(CONFIG_INVALID, "Provision plans run as root; the SSH user must be root.", {"field": "user"})
    settings = validated_provision(settings)
    if inventory is None:
        raise AgentError(INVENTORY_MISSING, "No provision inventory exists; collect it first.", {})
    _check_inventory_fresh(inventory)
    facts = summarize_provision_inventory(inventory)
    nvidia = [gpu for gpu in facts["gpus"] if gpu["vendor"] == "nvidia"]
    if len(nvidia) != 1:
        raise AgentError(CONFIG_INVALID, f"Expected exactly one NVIDIA GPU on the target, found {len(nvidia)}.",
                         {"nvidia_gpus": len(nvidia)})
    provision = {
        **settings,
        "gpu": nvidia[0],
        "cpu": facts["cpu"],
        "memory_gib": facts["memory_gib"],
        "tuning": derive_tuning(facts["cpu"]["threads"], facts["memory_kib"]),
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
                "destructive": PHASE_FLAGS[phase][0],
                "run": PHASE_FLAGS[phase][1],
                "apply": command_for(phase, target, provision),
                "check": check_command_for(phase, target, provision),
            }
            for phase in PHASES
        ],
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    plan["plan_id"] = plan["plan_hash"][:PLAN_ID_LENGTH]
    plan["created_at"] = now()
    return plan
