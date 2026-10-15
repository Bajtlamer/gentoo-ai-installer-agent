"""Install plans: the frozen, hashed description of exactly what will run.

A plan holds the validated configuration, the SSH target, the target disk
identity, and every generated phase command. plan_hash covers all of it, so an
approval of the hash is an approval of these exact commands on this exact disk.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from gentoo_agent.core import paths
from gentoo_agent.core.config import load_json, now, save_json
from gentoo_agent.core.disks import disk_from_inventory
from gentoo_agent.core.errors import (
    CONFIG_INVALID,
    INVENTORY_MISSING,
    PLAN_HASH_MISMATCH,
    PLAN_NOT_FOUND,
    AgentError,
)
from gentoo_agent.core.phases import PHASES, check_command_for, command_for
from gentoo_agent.core.validate import validated_settings


PLAN_SCHEMA_VERSION = 1
PLAN_ID_LENGTH = 16
PLAN_ID_PATTERN = re.compile(r"[0-9a-f]{16}")
UNHASHED_FIELDS = {"plan_id", "plan_hash", "created_at"}
TARGET_FIELDS = ("name", "host", "port", "user", "identity_file", "known_hosts_file")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_plan_hash(plan: dict) -> str:
    body = {key: value for key, value in plan.items() if key not in UNHASHED_FIELDS}
    return hashlib.sha256(canonical(body)).hexdigest()


def _check_erase_authorized(target: dict, install: dict) -> None:
    flags = {
        "install.disk_must_be_erased": install.get("disk_must_be_erased"),
        "install.target_is_disposable": install.get("target_is_disposable"),
        "install.disk_erase_authorized": install.get("disk_erase_authorized"),
        "target.target_is_disposable": target.get("target_is_disposable"),
        "target.disk_erase_authorized": target.get("disk_erase_authorized"),
    }
    missing = [name for name, value in flags.items() if value is not True]
    if missing:
        raise AgentError(
            CONFIG_INVALID,
            "The storage phase always erases the target disk; the configuration does not fully authorize that.",
            {"missing": missing},
        )


def create_install_plan(target: dict, install: dict, project_plan: dict, inventory: dict | None) -> dict:
    validated_settings(target, install)
    if inventory is None:
        raise AgentError(INVENTORY_MISSING, "No inventory exists; run inspect first.", {})
    _check_erase_authorized(target, install)
    disk = disk_from_inventory(inventory, install["disk"])
    destructive = {item["id"]: bool(item.get("destructive", True)) for item in project_plan.get("phases", [])}
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "target": {field: target.get(field) for field in TARGET_FIELDS},
        "install": install,
        "disks": [disk],
        "inventory_hash": hashlib.sha256(canonical(inventory)).hexdigest(),
        "phases": [
            {
                "id": phase,
                "destructive": destructive.get(phase, True),
                "apply": command_for(phase, target, install),
                "check": check_command_for(phase, target, install),
            }
            for phase in PHASES
        ],
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    plan["plan_id"] = plan["plan_hash"][:PLAN_ID_LENGTH]
    plan["created_at"] = now()
    return plan


def plan_path(plan_id: str) -> Path:
    if not PLAN_ID_PATTERN.fullmatch(str(plan_id or "")):
        raise AgentError(CONFIG_INVALID, f"Invalid plan id: {plan_id!r}", {"plan_id": plan_id})
    return paths.plans_dir() / f"{plan_id}.json"


def load_install_plan(plan_id: str) -> dict:
    path = plan_path(plan_id)
    if not path.exists():
        raise AgentError(PLAN_NOT_FOUND, f"Plan {plan_id} does not exist.", {"plan_id": plan_id})
    plan = load_json(path)
    if plan.get("plan_id") != plan_id or plan.get("plan_hash") != compute_plan_hash(plan):
        raise AgentError(PLAN_HASH_MISMATCH, f"Plan file {path} was modified after it was created.", {"plan_id": plan_id})
    return plan


def save_install_plan(plan: dict) -> dict:
    path = plan_path(plan["plan_id"])
    if path.exists():
        existing = load_install_plan(plan["plan_id"])
        if existing["plan_hash"] != plan["plan_hash"]:
            raise AgentError(PLAN_HASH_MISMATCH, "A different plan already uses this plan id.", {"plan_id": plan["plan_id"]})
        return existing
    save_json(path, plan)
    return plan
