"""Human approval of a stored plan, bound to its plan_hash.

Only a human writes approval records: the TTY-only CLI command now, and MCP
elicitation in plan 3. No function here prompts; front ends do that and then
call record_approval(). require_approved_plan() is the single execution gate.
Records are signed (HMAC-SHA256, key in secrets/approval.key); only processes
that can read secrets/ can create valid ones.
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
from gentoo_agent.core.signing import load_key, load_or_create_key, sign, verify


APPROVAL_TTL_MINUTES = 60
CHANNELS = {"cli", "elicitation"}
CLOCK_SKEW = dt.timedelta(seconds=60)
RECORD_KEYS = {"plan_id", "plan_hash", "channel", "approved_at", "expires_at", "signature"}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def approval_phrase(plan: dict) -> str:
    return f"APPROVE {plan['plan_id']}"


def _stored_plan(plan: dict) -> dict:
    """Load the stored plan and require the caller's dict to match it by hash."""
    stored = load_install_plan(plan["plan_id"])
    if stored["plan_hash"] != plan.get("plan_hash"):
        raise AgentError(
            PLAN_HASH_MISMATCH,
            f"The supplied plan does not match stored plan {plan['plan_id']}.",
            {"plan_id": plan["plan_id"]},
        )
    return stored


def approval_summary(plan: dict) -> str:
    plan = _stored_plan(plan)
    target = plan["target"]
    install = plan["install"]
    user = (install.get("username") or "none") if install.get("create_user") else "none"
    erase = "YES, all data on the disk will be destroyed" if install.get("disk_must_be_erased") else "no"
    lines = [
        f"Plan: {plan['plan_id']}",
        f"Plan hash: {plan['plan_hash']}",
        f"Target: {target['user']}@{target['host']}:{target.get('port') or 22} ({target['name']})",
        f"Created: {plan['created_at']}",
        (
            f"Install: boot={install.get('boot_mode')} table={install.get('partition_table')} "
            f"hostname={install.get('hostname')} user={user} init={install.get('init_system')}"
        ),
    ]
    for disk in plan["disks"]:
        lines.append(
            f"Disk: {disk['path']}  size={disk['size_bytes']} bytes  model={disk['model'] or 'unknown'}  "
            f"serial={disk['serial'] or 'none'}  wwn={disk['wwn'] or 'none'}"
        )
        if identity_strength(disk) == "weak":
            lines.append("WARNING: this disk has no serial number or WWN; drift detection relies on path, size, and model only.")
    lines.append(f"Disk erase: {erase}")
    lines.append("Destructive phases: " + ", ".join(phase["id"] for phase in plan["phases"] if phase["destructive"]))
    return "\n".join(lines)


def approval_path(plan_id: str) -> Path:
    plan_path(plan_id)  # validates the id format
    return paths.approval_dir() / f"{plan_id}.json"


def record_approval(plan: dict, channel: str, *, at: dt.datetime | None = None) -> dict:
    if not isinstance(channel, str) or channel not in CHANNELS:
        raise AgentError(CONFIG_INVALID, f"Unknown approval channel: {channel!r}", {"channel": str(channel)})
    if at is not None and at.tzinfo is None:
        raise AgentError(CONFIG_INVALID, "Approval time must be timezone-aware.", {})
    stored = _stored_plan(plan)
    approved_at = at or _utcnow()
    record = {
        "plan_id": stored["plan_id"],
        "plan_hash": stored["plan_hash"],
        "channel": channel,
        "approved_at": approved_at.isoformat(),
        "expires_at": (approved_at + dt.timedelta(minutes=APPROVAL_TTL_MINUTES)).isoformat(),
    }
    record["signature"] = sign(record, load_or_create_key())
    save_json(approval_path(stored["plan_id"]), record)
    return record


def _validated_record(plan: dict, at: dt.datetime) -> dict:
    plan_id = plan["plan_id"]

    def invalid() -> AgentError:
        return AgentError(
            APPROVAL_REQUIRED,
            f"The approval record for plan {plan_id} is invalid; approve the plan again.",
            {"plan_id": plan_id},
        )

    try:
        record = load_json(approval_path(plan_id))
    except Exception:
        raise invalid() from None
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        raise invalid()
    key = load_key()
    unsigned = {name: value for name, value in record.items() if name != "signature"}
    if key is None or not verify(unsigned, record["signature"], key):
        raise invalid()
    if record["plan_id"] != plan_id or not isinstance(record["channel"], str) or record["channel"] not in CHANNELS:
        raise invalid()
    moments = []
    for key in ("approved_at", "expires_at"):
        value = record[key]
        if not isinstance(value, str):
            raise invalid()
        try:
            moment = dt.datetime.fromisoformat(value)
        except ValueError:
            raise invalid() from None
        if moment.tzinfo is None:
            raise invalid()
        moments.append(moment)
    approved_at, expires_at = moments
    if expires_at != approved_at + dt.timedelta(minutes=APPROVAL_TTL_MINUTES):
        raise invalid()
    if approved_at > at + CLOCK_SKEW:
        raise invalid()
    if record["plan_hash"] != plan["plan_hash"]:
        raise AgentError(PLAN_HASH_MISMATCH, f"The approval for plan {plan_id} is for a different plan hash.", {"plan_id": plan_id})
    if expires_at <= at:
        raise AgentError(
            APPROVAL_EXPIRED,
            f"The approval for plan {plan_id} expired at {record['expires_at']}; approve it again.",
            {"plan_id": plan_id, "expires_at": record["expires_at"]},
        )
    return record


def _check(plan_id: str, at: dt.datetime | None) -> tuple[dict, dict]:
    plan = load_install_plan(plan_id)
    if not approval_path(plan_id).exists():
        raise AgentError(
            APPROVAL_REQUIRED,
            f"Plan {plan_id} is not approved. A human must run: gentoo-agent approve {plan_id}",
            {"plan_id": plan_id, "command": f"gentoo-agent approve {plan_id}"},
        )
    return plan, _validated_record(plan, at or _utcnow())


def require_approved_plan(plan_id: str, *, at: dt.datetime | None = None) -> dict:
    return _check(plan_id, at)[0]


def approval_status(plan_id: str, *, at: dt.datetime | None = None) -> dict:
    try:
        _, record = _check(plan_id, at)
    except AgentError as error:
        return {"approved": False, "code": error.code, "message": error.message}
    return {"approved": True, "channel": record["channel"], "expires_at": record["expires_at"]}
