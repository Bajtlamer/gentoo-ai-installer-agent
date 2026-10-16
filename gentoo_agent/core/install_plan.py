"""Install plans: the frozen, hashed description of exactly what will run.

A plan holds the validated configuration, the SSH target, the target disk
identity, and every generated phase command. plan_hash covers all of it, so an
approval of the hash is an approval of these exact commands on this exact disk.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from gentoo_agent.core import paths
from gentoo_agent.core.arch import live_architecture
from gentoo_agent.core.config import load_json, now, save_json
from gentoo_agent.core.disks import disk_candidates, disk_from_inventory
from gentoo_agent.core.foreign_os import detect_foreign_os, dual_boot_entries, parse_secure_boot
from gentoo_agent.core.errors import (
    CONFIG_CONFLICT,
    CONFIG_INVALID,
    INVENTORY_MISSING,
    PLAN_HASH_MISMATCH,
    PLAN_NOT_FOUND,
    AgentError,
)
from gentoo_agent.core.phases import PHASES, check_command_for, command_for
from gentoo_agent.core.validate import validate_installation, validated_settings


PLAN_SCHEMA_VERSION = 1
PLAN_ID_LENGTH = 16
PLAN_ID_PATTERN = re.compile(r"[0-9a-f]{16}")
UNHASHED_FIELDS = {"plan_id", "plan_hash", "created_at"}
INVENTORY_MAX_AGE = dt.timedelta(hours=24)
INVENTORY_MAX_SKEW = dt.timedelta(minutes=5)
GIB = 1024 ** 3
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


def _check_inventory_fresh(inventory: dict) -> None:
    collected = inventory.get("collected_at")
    try:
        moment = dt.datetime.fromisoformat(collected) if isinstance(collected, str) else None
    except ValueError:
        moment = None
    current = dt.datetime.now(dt.timezone.utc)
    if moment is None or moment.tzinfo is None or current - moment > INVENTORY_MAX_AGE:
        raise AgentError(INVENTORY_MISSING, "The latest inventory is undated or older than 24 hours; run inspect again.",
                         {"collected_at": collected})
    if moment - current > INVENTORY_MAX_SKEW:
        raise AgentError(INVENTORY_MISSING, "The latest inventory is dated more than 5 minutes in the future; "
                         "check the clock and run inspect again.", {"collected_at": collected})


def _check_review(target: dict, install: dict, project_plan: dict, inventory: dict) -> None:
    review = validate_installation(target, install, project_plan, inventory)
    if review["errors"]:
        raise AgentError(CONFIG_INVALID, "The configuration does not pass review: " + "; ".join(review["errors"]),
                         {"errors": review["errors"]})


def _check_disk_eligible(inventory: dict, path: str) -> None:
    """The same exclusions as resolve_config (removable, USB, read-only, mounted), for every front end."""
    _, excluded = disk_candidates(inventory)
    rejected = next((item for item in excluded if item["path"] == path), None)
    if rejected is not None:
        raise AgentError(CONFIG_CONFLICT, f"Disk {path} cannot be installed to: {rejected['reason']}.",
                         {"field": "disk", "disk": path, "reason": rejected["reason"]})


def _check_expected_disk(install: dict, disk: dict) -> None:
    """The operator's expected model and size, checked against the inventory before anything is approved.

    Preflight checks the same values on the live target; the size tolerance matches it (under 1 GiB).
    """
    model = install.get("expected_disk_model")
    if model and model != disk["model"]:
        raise AgentError(CONFIG_INVALID, f"Disk {disk['path']} is a {disk['model']!r}, not the expected {model!r}.",
                         {"field": "expected_disk_model"})
    size = install.get("expected_disk_size_gib")
    if size:
        if abs(disk["size_bytes"] - int(float(size) * GIB)) >= GIB:
            raise AgentError(CONFIG_INVALID, f"Disk {disk['path']} is {disk['size_bytes'] / GIB:.1f} GiB, "
                             f"not the expected {size} GiB.", {"field": "expected_disk_size_gib"})


def _check_secure_boot(install: dict, secure_boot: str) -> list[str]:
    """Refuse a UEFI install while Secure Boot is on; warn when its state is unknown."""
    if install.get("boot_mode") != "uefi":
        return []
    if secure_boot == "on":
        raise AgentError(CONFIG_CONFLICT, "Secure Boot is on, and the installed GRUB is not signed. Turn Secure Boot "
                         "off in the firmware setup, boot the live ISO again, and refresh the inventory.",
                         {"secure_boot": "on"})
    if secure_boot == "unknown":
        return ["Secure Boot state is unknown; if it is on, the firmware will refuse to start GRUB. "
                "Check the firmware setup before approving."]
    return []


def _foreign_os_warnings(install: dict, records: list[dict]) -> list[str]:
    others = [record for record in records if record["disk"] != install["disk"]]
    warnings = [f"Windows in BIOS mode on {record['disk']} will not be in the GRUB menu."
                for record in others if record["os"] == "windows-bios"]
    warnings += [f"{record['disk']} holds Windows data or boot files ({', '.join(record['evidence'])}) but no "
                 "complete UEFI Windows; it will not be added to the GRUB menu."
                 for record in others if record["os"] == "windows-data"]
    if install.get("dual_boot") == "off":
        warnings += [f"Windows on {record['disk']} will not be added to the GRUB menu (dual_boot is off)."
                     for record in others if record["os"] in ("windows", "windows-bios")]
    return warnings


def _erases_existing_os(install: dict, records: list[dict]) -> dict | None:
    record = next((item for item in records if item["disk"] == install["disk"]), None)
    if record is None:
        return None
    return {"disk": record["disk"], "os": record["os"], "evidence": list(record["evidence"])}


def _kept_disk(inventory: dict, path: str) -> dict:
    """The identity of a Windows disk that gets a menu entry; the drift check re-reads it during the run."""
    try:
        return disk_from_inventory(inventory, path)
    except AgentError as error:
        if error.code != CONFIG_INVALID:
            raise
        raise AgentError(INVENTORY_MISSING, f"Windows disk {path} is not in the disk identity inventory; refresh it.",
                         {"disk": path}) from None


def create_install_plan(target: dict, install: dict, project_plan: dict, inventory: dict | None) -> dict:
    # dual_boot_entries is derived below from the inventory; a value in the input is never used.
    install = {key: value for key, value in install.items() if key != "dual_boot_entries"}
    validated_settings(target, install)
    if inventory is None:
        raise AgentError(INVENTORY_MISSING, "No inventory exists; run inspect first.", {})
    _check_erase_authorized(target, install)
    _check_inventory_fresh(inventory)
    live = live_architecture(inventory)
    if install.get("architecture") != live:
        raise AgentError(CONFIG_CONFLICT, f"The configuration is for {install.get('architecture')}, "
                         f"but the live system is {live}.", {"configured": install.get("architecture"), "live": live})
    secure_boot = parse_secure_boot(inventory)
    warnings = _check_secure_boot(install, secure_boot)
    _check_review(target, install, project_plan, inventory)
    disk = disk_from_inventory(inventory, install["disk"])
    _check_disk_eligible(inventory, disk["path"])
    _check_expected_disk(install, disk)
    records = detect_foreign_os(inventory)
    install["dual_boot_entries"] = dual_boot_entries(install, records)
    validated_settings(target, install)
    warnings += _foreign_os_warnings(install, records)
    kept = [_kept_disk(inventory, entry["disk"]) for entry in install["dual_boot_entries"]]
    destructive = {item["id"]: bool(item.get("destructive", True)) for item in project_plan.get("phases", [])}
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "target": {field: target.get(field) for field in TARGET_FIELDS},
        "install": copy.deepcopy(install),
        # The install disk first; then every Windows disk with a menu entry (never written, drift-checked).
        "disks": [disk, *kept],
        "secure_boot": secure_boot,
        "erases_existing_os": _erases_existing_os(install, records),
        "warnings": warnings,
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


def _verify(plan: dict, path: Path) -> None:
    plan_hash = plan.get("plan_hash")
    if (
        not isinstance(plan_hash, str)
        or plan_hash != compute_plan_hash(plan)
        or plan.get("plan_id") != plan_hash[:PLAN_ID_LENGTH]
    ):
        raise AgentError(
            PLAN_HASH_MISMATCH,
            f"Plan file {path} was modified after it was created.",
            {"plan_id": plan.get("plan_id")},
        )


def load_install_plan(plan_id: str) -> dict:
    path = plan_path(plan_id)
    if not path.exists():
        raise AgentError(PLAN_NOT_FOUND, f"Plan {plan_id} does not exist.", {"plan_id": plan_id})
    try:
        plan = load_json(path)
    except ValueError:
        plan = None
    if not isinstance(plan, dict):
        raise AgentError(PLAN_HASH_MISMATCH, f"Plan file {path} is not a valid plan.", {"plan_id": plan_id})
    if plan.get("plan_id") != plan_id:
        raise AgentError(PLAN_HASH_MISMATCH, f"Plan file {path} was modified after it was created.", {"plan_id": plan_id})
    _verify(plan, path)
    return plan


def save_install_plan(plan: dict) -> dict:
    path = plan_path(plan["plan_id"])
    _verify(plan, path)
    if path.exists():
        existing = load_install_plan(plan["plan_id"])
        if existing["plan_hash"] != plan["plan_hash"]:
            raise AgentError(PLAN_HASH_MISMATCH, "A different plan already uses this plan id.", {"plan_id": plan["plan_id"]})
        return existing
    save_json(path, plan)
    return plan
