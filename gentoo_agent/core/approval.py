"""Human approval of a stored plan, bound to its plan_hash.

Only a human writes approval records: through the TTY-only CLI command or the
MCP server's elicitation dialog. No function here prompts; front ends do that and then
call record_approval(). require_approved_plan() is the single execution gate.
Records are signed (HMAC-SHA256, key in secrets/approval.key); only processes
that can read secrets/ can create valid ones.
"""

from __future__ import annotations

import datetime as dt
import json
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
RECORD_KEYS = {"plan_id", "plan_hash", "channel", "approved_at", "expires_at", "erase_confirmed", "signature"}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def erase_phrase(plan: dict) -> str | None:
    """The extra words a human types when the plan erases a disk that holds Windows; None otherwise."""
    erases = plan.get("erases_existing_os")
    return f"ERASE WINDOWS {erases['disk']}" if erases else None


def approval_phrase(plan: dict) -> str:
    extra = erase_phrase(plan)
    return f"APPROVE {plan['plan_id']}" + (f" {extra}" if extra else "")


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


SHOWN_LIMIT = 200


def _shown(value: object) -> str:
    """Render one interpolated value so it can never fake lines in the dialog.

    Short printable strings appear as is; anything else (newlines, control
    characters, non-strings, long values) is shown JSON-escaped and truncated.
    """
    if isinstance(value, str) and value.isprintable() and len(value) <= SHOWN_LIMIT:
        return value
    text = json.dumps(value, ensure_ascii=True, default=str)
    if len(text) > SHOWN_LIMIT:
        text = text[:SHOWN_LIMIT] + "..."
    return text


def approval_summary(plan: dict) -> str:
    plan = _stored_plan(plan)
    target = plan["target"]
    install = plan["install"]
    user = (install.get("username") or "none") if install.get("create_user") else "none"
    erase = "YES, all data on the disk will be destroyed" if install.get("disk_must_be_erased") else "no"
    destructive = [_shown(phase["id"]) for phase in plan["phases"] if phase["destructive"]]
    # Erase and destructive lines come before any value read from the target or inventory.
    lines = []
    erases = plan.get("erases_existing_os")
    if erases:
        evidence = ", ".join(_shown(item) for item in erases["evidence"])
        lines.append(f"WARNING: {_shown(erases['disk'])} contains Windows ({evidence}). It will be erased.")
    lines += [
        f"Plan: {_shown(plan['plan_id'])}",
        f"Plan hash: {_shown(plan['plan_hash'])}",
        f"Disk erase: {erase}",
        "Destructive phases: " + ", ".join(destructive),
        *(f"WARNING: {_shown(warning)}" for warning in plan.get("warnings", [])),
        (
            f"Target: {_shown(target['user'])}@{_shown(target['host'])}:{_shown(str(target.get('port') or 22))} "
            f"({_shown(target['name'])})"
        ),
        f"Created: {_shown(plan['created_at'])}",
        f"Architecture: {_shown(install.get('architecture'))}",
        (
            f"Install: boot={_shown(install.get('boot_mode'))} table={_shown(install.get('partition_table'))} "
            f"hostname={_shown(install.get('hostname'))} user={_shown(user)} init={_shown(install.get('init_system'))}"
        ),
    ]
    if install.get("network_method", "dhcp") == "static":
        static = install.get("network_static") or {}
        dns = ",".join(str(item) for item in static.get("dns") or [])
        lines.append(
            f"Network: static interface={_shown(static.get('interface'))} address={_shown(static.get('address'))} "
            f"gateway={_shown(static.get('gateway'))} dns={_shown(dns)}"
        )
    else:
        lines.append(f"Network: {_shown(install.get('network_method', 'dhcp'))}")
    for entry in install.get("dual_boot_entries", []):
        lines.append(
            f"Dual boot: Windows on {_shown(entry['disk'])} (EFI UUID {_shown(entry['esp_uuid'])}) will be added to "
            "the GRUB menu. Gentoo becomes the default; Windows may ask once for its BitLocker recovery key. "
            # Fast Boot connects only the boot disk, so GRUB cannot see the Windows disk (VMware default).
            'If GRUB reports "no such device" for it, turn off Fast Boot in the firmware '
            '(VMware: efi.quickBoot.enabled = "FALSE" in the .vmx).'
        )
    password_source = install.get("password_source", "environment")
    passwords = f"Passwords: source={_shown(password_source)}"
    if password_source == "environment":
        # Variable names only; values are never read here.
        passwords += (
            f" root_password_env={_shown(install.get('root_password_env'))}"
            f" user_password_env={_shown(install.get('user_password_env'))}"
        )
    lines.append(passwords)

    def yes_no(value: object) -> str:
        return "yes" if value else "no"

    groups = ",".join(str(group) for group in install.get("user_groups", ["wheel"])) if install.get("create_user") else "none"
    lines.append(
        f"Access: allow_root_password_ssh={yes_no(install.get('allow_root_password_ssh', False))} "
        f"configure_sudo={yes_no(install.get('configure_sudo', True))} user_groups={_shown(groups)}"
    )
    for index, disk in enumerate(plan["disks"]):
        # The install disk comes first; the others hold Windows and are only read.
        label = "Disk (erased)" if index == 0 else "Windows disk (kept, not written)"
        lines.append(
            f"{label}: {_shown(disk['path'])}  size={_shown(str(disk['size_bytes']))} bytes  "
            f"model={_shown(disk['model'] or 'unknown')}  "
            f"serial={_shown(disk['serial'] or 'none')}  wwn={_shown(disk['wwn'] or 'none')}"
        )
        if identity_strength(disk, plan["disks"]) == "weak":
            lines.append("WARNING: this disk has no unique serial number or WWN; drift detection relies on path, size, and model only.")
    return "\n".join(lines)


def approval_path(plan_id: str) -> Path:
    plan_path(plan_id)  # validates the id format
    return paths.approval_dir() / f"{plan_id}.json"


def record_approval(plan: dict, channel: str, *, at: dt.datetime | None = None, erase_confirmed: bool = False) -> dict:
    """erase_confirmed: the front end checked the typed ERASE WINDOWS <disk> confirmation."""
    if not isinstance(channel, str) or channel not in CHANNELS:
        raise AgentError(CONFIG_INVALID, f"Unknown approval channel: {channel!r}", {"channel": str(channel)})
    if not isinstance(erase_confirmed, bool):
        raise AgentError(CONFIG_INVALID, "erase_confirmed must be boolean.", {})
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
        "erase_confirmed": erase_confirmed,
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
    if not isinstance(record["erase_confirmed"], bool):
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
    record = _validated_record(plan, at or _utcnow())
    if plan.get("erases_existing_os") and record["erase_confirmed"] is not True:
        raise AgentError(
            APPROVAL_REQUIRED,
            f"Plan {plan_id} erases a disk that holds Windows; the approval must include the typed confirmation "
            f"{erase_phrase(plan)}. A human must run: gentoo-agent approve {plan_id}",
            {"plan_id": plan_id, "command": f"gentoo-agent approve {plan_id}"},
        )
    return plan, record


def require_approved_plan(plan_id: str, *, at: dt.datetime | None = None) -> dict:
    return _check(plan_id, at)[0]


def approval_status(plan_id: str, *, at: dt.datetime | None = None) -> dict:
    try:
        _, record = _check(plan_id, at)
    except AgentError as error:
        return {"approved": False, "code": error.code, "message": error.message}
    return {"approved": True, "channel": record["channel"], "expires_at": record["expires_at"]}
