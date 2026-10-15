"""Human approval of a stored plan, bound to its plan_hash.

Only a human writes approval records: the TTY-only CLI command now, and MCP
elicitation in plan 3. No function here prompts; front ends do that and then
call record_approval(). require_approved_plan() is the single execution gate.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from gentoo_agent.core import paths
from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.disks import identity_strength
from gentoo_agent.core.errors import (
    APPROVAL_EXPIRED,
    APPROVAL_REQUIRED,
    CONFIG_INVALID,
    PLAN_HASH_MISMATCH,
    AgentError,
)
from gentoo_agent.core.install_plan import load_install_plan, plan_path


APPROVAL_TTL_MINUTES = 60
CHANNELS = {"cli", "elicitation"}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def approval_phrase(plan: dict) -> str:
    return f"APPROVE {plan['plan_id']}"


def approval_summary(plan: dict) -> str:
    target = plan["target"]
    lines = [
        f"Plan: {plan['plan_id']}",
        f"Plan hash: {plan['plan_hash']}",
        f"Target: {target['user']}@{target['host']}:{target.get('port') or 22} ({target['name']})",
    ]
    for disk in plan["disks"]:
        lines.append(
            f"Disk: {disk['path']}  size={disk['size_bytes']} bytes  model={disk['model'] or 'unknown'}  "
            f"serial={disk['serial'] or 'none'}  wwn={disk['wwn'] or 'none'}"
        )
        if identity_strength(disk) == "weak":
            lines.append("WARNING: this disk has no serial number or WWN; drift detection relies on path, size, and model only.")
    lines.append("Disk erase: YES, all data on the disk will be destroyed")
    lines.append("Destructive phases: " + ", ".join(phase["id"] for phase in plan["phases"] if phase["destructive"]))
    return "\n".join(lines)


def approval_path(plan_id: str) -> Path:
    plan_path(plan_id)  # validates the id format
    return paths.approval_dir() / f"{plan_id}.json"


def record_approval(plan: dict, channel: str, *, at: dt.datetime | None = None) -> dict:
    if channel not in CHANNELS:
        raise AgentError(CONFIG_INVALID, f"Unknown approval channel: {channel!r}", {"channel": channel})
    approved_at = at or _utcnow()
    record = {
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "channel": channel,
        "approved_at": approved_at.isoformat(),
        "expires_at": (approved_at + dt.timedelta(minutes=APPROVAL_TTL_MINUTES)).isoformat(),
    }
    save_json(approval_path(plan["plan_id"]), record)
    return record


def require_approved_plan(plan_id: str, *, at: dt.datetime | None = None) -> dict:
    plan = load_install_plan(plan_id)
    path = approval_path(plan_id)
    if not path.exists():
        raise AgentError(
            APPROVAL_REQUIRED,
            f"Plan {plan_id} is not approved. A human must run: gentoo-agent approve {plan_id}",
            {"plan_id": plan_id, "command": f"gentoo-agent approve {plan_id}"},
        )
    record = load_json(path)
    if record.get("plan_hash") != plan["plan_hash"]:
        raise AgentError(PLAN_HASH_MISMATCH, f"The approval for plan {plan_id} is for a different plan hash.", {"plan_id": plan_id})
    if dt.datetime.fromisoformat(record["expires_at"]) <= (at or _utcnow()):
        raise AgentError(
            APPROVAL_EXPIRED,
            f"The approval for plan {plan_id} expired at {record['expires_at']}; approve it again.",
            {"plan_id": plan_id, "expires_at": record["expires_at"]},
        )
    return plan


def approval_status(plan_id: str, *, at: dt.datetime | None = None) -> dict:
    try:
        require_approved_plan(plan_id, at=at)
    except AgentError as error:
        return {"approved": False, "code": error.code, "message": error.message}
    record = load_json(approval_path(plan_id))
    return {"approved": True, "channel": record["channel"], "expires_at": record["expires_at"]}
