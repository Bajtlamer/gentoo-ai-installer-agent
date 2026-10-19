"""MCP server: a thin adapter over gentoo_agent.core and gentoo_agent.jobs.

Never write to stdout: it carries the protocol. Every tool returns a
CallToolResult with structured_content; errors carry {code, message, details}
and is_error. Approval is recorded only after a human accepts an elicitation
dialog built by this server, and the server never returns passwords.
"""

from __future__ import annotations

import json
import logging
import subprocess

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, StrictBool, StrictStr

from gentoo_agent.core import (
    paths,
    phases as core_phases,
    provision_phases as core_provision_phases,
    provision_plan as core_provision_plan,
)
from gentoo_agent.core.approval import (
    APPROVAL_TTL_MINUTES,
    approval_status,
    approval_summary,
    erase_phrase,
    record_approval,
)
from gentoo_agent.core.config import load_json, load_plan, target_config
from gentoo_agent.core.errors import (
    APPROVAL_DECLINED,
    APPROVAL_REQUIRED,
    CONFIG_INVALID,
    HOST_KEY_UNKNOWN,
    INTERNAL_ERROR,
    INVENTORY_MISSING,
    PLAN_HASH_MISMATCH,
    SSH_UNREACHABLE,
    AgentError,
)
from gentoo_agent.core.hardware import summarize_inventory
from gentoo_agent.core.install_plan import create_install_plan, load_install_plan, plan_kind, save_install_plan
from gentoo_agent.core.inventory import (
    collect_inventory,
    collect_provision_inventory,
    latest_inventory,
    latest_provision_inventory,
    record_inventory,
)
from gentoo_agent.core.provision_facts import summarize_provision_inventory
from gentoo_agent.core.resolve import resolve_config as resolve_install
from gentoo_agent.core.runner import output_tail
from gentoo_agent.core.runs import credentials_path, read_log, read_run, run_status
from gentoo_agent.core.ssh import HOST_KEY_MESSAGE, host_key_failed, remote_shell
from gentoo_agent.jobs.launcher import cancel_run as launcher_cancel, resume_run as launcher_resume, start_run


logger = logging.getLogger("gentoo_agent.mcp")

INSTRUCTIONS = (
    "Installs Gentoo on a disposable target over SSH. Workflow: detect_hardware(refresh=true), "
    "resolve_config to preview, create_plan, approve_plan (a human confirms in a dialog), "
    "start_install, then get_run_status and get_run_log until the run ends. Never ask for or "
    "pass passwords as text; reference environment variables with {\"env\": \"NAME\"}. "
    "To provision an INSTALLED running system instead (it touches no disk): detect_provision(refresh=true), "
    "create_provision_plan(accept_licenses=[...], package_use=[...]), approve_plan (a human confirms in a dialog), start_install, "
    "then get_run_status and get_run_log until the run ends. The HUMAN reboots the target; afterwards "
    "verify_install proves the GPU. License names and package.use lines come only from the human or from "
    "emerge --pretend output that the human shows you: never invent or guess license names or package.use "
    "lines, and never pass a license the human did not name."
)

server = MCPServer("gentoo-agent", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
REFRESHABLE_READ = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
REMOTE_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
HUMAN_CONFIRMS = {"anthropic/requiresUserInteraction": True}


def ok_result(data: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, indent=2))], structured_content=data)


def error_result(error: AgentError) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"{error.code}: {error.message}")],
                          structured_content=error.to_dict(), is_error=True)


def respond(function, *args, **kwargs) -> CallToolResult:
    try:
        return ok_result(function(*args, **kwargs))
    except AgentError as error:
        return error_result(error)
    except Exception:  # never surface a bare traceback to the client
        logger.exception("tool %s failed unexpectedly", getattr(function, "__qualname__", function))
        return error_result(AgentError(INTERNAL_ERROR, "The tool failed unexpectedly; see the server log.", {}))


def plan_view(plan: dict) -> dict:
    kind = plan_kind(plan)
    view = {
        "plan_id": plan["plan_id"],
        "kind": kind,
        "plan_hash": plan["plan_hash"],
        "created_at": plan["created_at"],
        "summary": approval_summary(plan),
        "target": plan["target"],
        "disks": plan.get("disks", []),
        "dual_boot_entries": (plan.get("install") or {}).get("dual_boot_entries", []),
        "erases_existing_os": plan.get("erases_existing_os"),
        "secure_boot": plan.get("secure_boot"),
        "warnings": plan.get("warnings", []),
        "phases": [{"id": phase["id"], "destructive": phase["destructive"]} for phase in plan["phases"]],
        "approval": approval_status(plan["plan_id"]),
    }
    if kind == "provision":
        view["identity"] = plan["identity"]
        view["provision"] = plan["provision"]
    return view


@server.tool(annotations=READ_ONLY)
def list_phases() -> CallToolResult:
    """List the runnable installation phases, in order, and which ones change the target."""
    def run() -> dict:
        described = {item.get("id"): item for item in load_plan().get("phases", []) if isinstance(item, dict)}
        return {"phases": [
            {"id": phase, "description": described.get(phase, {}).get("description", ""),
             "destructive": bool(described.get(phase, {}).get("destructive", True)),
             "handbook_sections": described.get(phase, {}).get("handbook_sections", [])}
            for phase in core_phases.PHASES
        ]}
    return respond(run)


@server.tool(annotations=READ_ONLY)
def get_plan(plan_id: str) -> CallToolResult:
    """Show a stored plan, its summary, and its approval status."""
    return respond(lambda: plan_view(load_install_plan(plan_id)))


@server.tool(annotations=READ_ONLY)
def get_run_log(run_id: str, offset: int = 0, limit: int = 50) -> CallToolResult:
    """Read part of a run's log (offset and limit count log lines)."""
    return respond(read_log, run_id, offset, limit)


def _resolved(overrides: dict | None, fallbacks: dict | None) -> tuple[dict, dict | None, dict]:
    target = target_config()
    defaults = load_json(paths.config_dir() / "install.default.json")
    inventory = latest_inventory()
    return target, inventory, resolve_install(target, overrides, fallbacks, defaults, inventory)


@server.tool(annotations=REFRESHABLE_READ)
def detect_hardware(refresh: bool = False) -> CallToolResult:
    """Summarize the target: CPU architecture (amd64 or arm64), firmware boot mode, and disks with
    serial/WWN. refresh=true runs the read-only SSH inventory first; otherwise the latest saved
    inventory is used."""
    def run() -> dict:
        target = target_config()
        if refresh:
            inventory, path = collect_inventory(target)
            record_inventory(target, path)
        else:
            inventory = latest_inventory()
            if inventory is None:
                raise AgentError(INVENTORY_MISSING, "No inventory exists; call detect_hardware with refresh=true.", {})
        return summarize_inventory(inventory)
    return respond(run)


@server.tool(annotations=READ_ONLY)
def resolve_config(overrides: dict | None = None, fallbacks: dict | None = None) -> CallToolResult:
    """Preview the merged install config (overrides > inferred > fallbacks > defaults) with the
    source of every value. Writes nothing. Passwords only as {"env": "NAME"}."""
    return respond(lambda: _resolved(overrides, fallbacks)[2])


@server.tool(annotations=LOCAL_WRITE)
def create_plan(overrides: dict | None = None, fallbacks: dict | None = None) -> CallToolResult:
    """Freeze the resolved config, the disk identity, and every generated command into a plan.
    Same inputs as resolve_config. Returns plan_id, plan_hash, and a summary for the human."""
    def run() -> dict:
        target, inventory, resolved = _resolved(overrides, fallbacks)
        plan = save_install_plan(create_install_plan(target, resolved["install"], load_plan(), inventory))
        return {**plan_view(plan), "sources": resolved["sources"], "warnings": resolved["warnings"] + plan["warnings"]}
    return respond(run)


@server.tool(annotations=REFRESHABLE_READ)
def detect_provision(refresh: bool = False) -> CallToolResult:
    """Summarize the running, installed system for provisioning: host identity, CPU threads, memory, GPUs,
    kernel, and the Portage settings a plan would replace. refresh=true runs the read-only SSH inventory
    first; otherwise the latest saved provision inventory is used."""
    def run() -> dict:
        if refresh:
            inventory, _ = collect_provision_inventory(target_config())
        else:
            inventory = latest_provision_inventory()
            if inventory is None:
                raise AgentError(INVENTORY_MISSING, "No provision inventory exists; call detect_provision with refresh=true.", {})
        return summarize_provision_inventory(inventory)
    return respond(run)


@server.tool(annotations=LOCAL_WRITE)
def create_provision_plan(accept_licenses: list[str], package_use: list[str] | None = None,
                          profile: str = "nvidia-driver") -> CallToolResult:
    """Freeze a provision plan for the running system: Portage tuning (parallel jobs derived from the CPU
    threads and the memory) and the NVIDIA driver. accept_licenses lists the license names the plan
    accepts; they are accepted only if a human approves the plan. package_use lines come only from the human's emerge --pretend output
    (never invent them); they are written to package.use next to the plan's own nvidia-drivers line. The plan erases no disk. Returns
    plan_id, plan_hash, and a summary for the human."""
    def run() -> dict:
        plan = core_provision_plan.create_provision_plan(
            target_config(), {"profile": profile, "accept_licenses": accept_licenses, "package_use": package_use or []}, latest_provision_inventory())
        return plan_view(save_install_plan(plan))
    return respond(run)


class ApprovalAnswer(BaseModel):
    approve: StrictBool


class EraseWindowsApprovalAnswer(BaseModel):
    """For a plan that erases a disk holding Windows: confirm must be exactly ERASE WINDOWS <disk>."""
    approve: StrictBool
    confirm: StrictStr


def approval_question(plan: dict) -> str:
    """The question under the approval summary; a provision plan has no disk, only the host identity."""
    scope = "the commands above and the host identity shown" if plan_kind(plan) == "provision" else "the commands and disk above"
    return (
        f"Approve this plan? The approval is valid for {APPROVAL_TTL_MINUTES} minutes "
        f"and covers exactly {scope}. Check 'approve' and accept only if you, "
        "the human operator, agree."
    )


@server.tool(annotations=LOCAL_WRITE)
async def approve_plan(plan_id: str, ctx: Context) -> CallToolResult:
    """Ask the human, in a dialog built by this server, to approve a stored plan for 60 minutes.
    The model cannot answer the dialog. Without dialog support, returns the terminal command."""
    try:
        plan = load_install_plan(plan_id)
        summary = approval_summary(plan)
    except AgentError as error:
        return error_result(error)
    fallback = AgentError(
        APPROVAL_REQUIRED,
        "A human must approve this plan. Ask the operator to run this in their own terminal "
        f"(do not run it yourself): gentoo-agent approve {plan_id}",
        {"plan_id": plan_id, "command": f"gentoo-agent approve {plan_id}"},
    )
    capabilities = ctx.client_capabilities
    if capabilities is None or capabilities.elicitation is None:
        return error_result(fallback)
    required = erase_phrase(plan)
    message = f"{summary}\n\n{approval_question(plan)}"
    if required is not None:
        message += f" This plan erases a disk that holds Windows: type exactly {required} in 'confirm'."
    declined = error_result(AgentError(APPROVAL_DECLINED, "Approval not recorded.", {"plan_id": plan_id}))
    try:
        answer = await ctx.elicit(message, ApprovalAnswer if required is None else EraseWindowsApprovalAnswer)
    except ValueError:  # accepted with content that does not match the form (for example no confirm text)
        logger.warning("elicitation answer for plan %s did not match the form", plan_id)
        return declined
    except Exception:  # unsupported transport, client error, or timeout: fail closed
        logger.exception("elicitation failed for plan %s", plan_id)
        return error_result(fallback)
    data = getattr(answer, "data", None)
    if answer.action != "accept" or data is None or data.approve is not True:
        return declined
    if required is not None and getattr(data, "confirm", None) != required:
        return declined
    try:
        record = record_approval(plan, "elicitation", erase_confirmed=required is not None)
    except AgentError as error:
        return error_result(error)
    return ok_result({"approved": True, "plan_id": plan_id, "channel": "elicitation",
                      "approved_until": record["expires_at"]})


PROMPT_UNAVAILABLE = (
    "password_source 'prompt' cannot be used through MCP; reference environment variables or generate passwords."
)


def _refuse_prompt(plan_id: str) -> None:
    if (load_install_plan(plan_id).get("install") or {}).get("password_source") == "prompt":
        raise AgentError(CONFIG_INVALID, PROMPT_UNAVAILABLE, {"field": "password_source"})


def _start(plan_id: str, phase_ids: list[str] | None = None) -> dict:
    _refuse_prompt(plan_id)
    run_id = start_run(plan_id, phase_ids=phase_ids)
    return {"run_id": run_id, "plan_id": plan_id, "status": "starting"}


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def start_install(plan_id: str) -> CallToolResult:
    """Start the approved plan in the background. An install plan erases the target disk; a provision plan changes the installed system and erases nothing. Returns run_id."""
    return respond(_start, plan_id)


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def run_phase(plan_id: str, phases: list[str]) -> CallToolResult:
    """Run only the listed phases of an approved plan (in plan order), in the background."""
    return respond(_start, plan_id, phases)


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def resume_run(run_id: str) -> CallToolResult:
    """Start a new run of the same approved plan; completed phases are checked and skipped."""
    def run() -> dict:
        _refuse_prompt(read_run(run_id)["plan_id"])
        return {"run_id": launcher_resume(run_id), "resumed_from": run_id}
    return respond(run)


@server.tool(annotations=LOCAL_WRITE)
def cancel_run(run_id: str) -> CallToolResult:
    """Stop a run at the next phase boundary."""
    return respond(launcher_cancel, run_id)


@server.tool(annotations=LOCAL_WRITE)  # may mark a dead run interrupted and purge stale credentials
def get_run_status(run_id: str) -> CallToolResult:
    """Run status, phase index, current phase, and last error. Never returns passwords."""
    def run() -> dict:
        record = run_status(run_id)
        waiting = credentials_path(run_id).exists()
        applied = _credentials_applied(run_id) if waiting else None
        result = {**record, "credentials_waiting": waiting, "credentials_applied": applied}
        if waiting:
            command = f"gentoo-agent run status {run_id}"
            if applied is False:
                result["credentials_hint"] = (
                    "Passwords were generated but the authentication phase did not run; they were not applied. "
                    f"Read them yourself in a terminal: {command}"
                )
            else:
                result["credentials_hint"] = f"Generated passwords are waiting. Read them yourself in a terminal: {command}"
        return result
    return respond(run)


def _credentials_applied(run_id: str) -> bool | None:
    """The 'applied' flag of a waiting credentials file: True, False, or None while pending.

    Reads the file without taking or deleting it, and returns nothing else from it.
    """
    try:
        data = load_json(credentials_path(run_id))
    except (OSError, ValueError):
        return None
    applied = data.get("applied") if isinstance(data, dict) else None
    return applied if isinstance(applied, bool) else None


@server.tool(annotations=REMOTE_READ)
def verify_install(plan_id: str) -> CallToolResult:
    """Run each phase's read-only check from the stored plan and report which are complete."""
    def run() -> dict:
        plan = load_install_plan(plan_id)
        phases = []
        for phase in plan["phases"]:
            # Only run checks this code would generate for the plan; never trust the stored text alone.
            if plan_kind(plan) == "provision":
                expected = core_provision_phases.check_command_for(phase["id"], plan["target"], plan["provision"])
            else:
                expected = core_phases.check_command_for(phase["id"], plan["target"], plan["install"])
            if phase["check"] != expected:
                raise AgentError(PLAN_HASH_MISMATCH, f"The stored {phase['id']} check does not match the plan.",
                                 {"plan_id": plan_id, "phase": phase["id"]})
        for phase in plan["phases"]:
            try:
                result = remote_shell(plan["target"], f"{phase['id']}-verify-install", phase["check"], timeout=300)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AgentError(SSH_UNREACHABLE, f"Could not run the {phase['id']} check: {exc}",
                                 {"phase": phase["id"]}) from exc
            if host_key_failed(result):
                raise AgentError(HOST_KEY_UNKNOWN, HOST_KEY_MESSAGE,
                                 {"phase": phase["id"], "host": plan["target"].get("host")})
            if result["returncode"] == 255:  # ssh itself failed; the check did not run
                raise AgentError(SSH_UNREACHABLE, f"Could not reach the target over SSH to run the {phase['id']} check.",
                                 {"phase": phase["id"], "host": plan["target"].get("host")})
            phases.append({"id": phase["id"], "complete": result["returncode"] == 0,
                           "output_tail": output_tail(result["stdout"] + result["stderr"])})
        return {"plan_id": plan_id, "phases": phases}
    return respond(run)


@server.resource("gentoo://plan/{plan_id}")
def plan_resource(plan_id: str) -> str:
    return json.dumps(plan_view(load_install_plan(plan_id)), indent=2)


@server.resource("gentoo://run/{run_id}/state")
def run_state_resource(run_id: str) -> str:
    return json.dumps(run_status(run_id), indent=2)


@server.resource("gentoo://run/{run_id}/log")
def run_log_resource(run_id: str) -> str:
    return json.dumps(read_log(run_id, 0, 500), indent=2)


@server.prompt()
def install_gentoo(request: str = "") -> str:
    return (
        "Install Gentoo on the configured disposable target.\n"
        f"User request: {request}\n\n"
        "1. detect_hardware(refresh=true). If several disks are listed, ask the user which one.\n"
        "2. Translate the request into overrides and fallbacks; passwords only as {\"env\": \"NAME\"}.\n"
        "3. resolve_config to preview; report conflicts to the user instead of guessing.\n"
        "4. create_plan, then approve_plan. Never try to approve any other way.\n"
        "5. start_install, then poll get_run_status and read get_run_log until the run ends.\n"
        "6. On failure, report the error code and output tail; resume_run only if the user agrees.\n"
        "7. Never reboot the target. If credentials are waiting, tell the user to read them in a terminal.\n\n"
        "To provision an already installed running system instead (no disk is touched): "
        "detect_provision(refresh=true), create_provision_plan(accept_licenses=[...], package_use=[...]), approve_plan, "
        "start_install, then get_run_status and get_run_log. The human reboots; then verify_install. "
        "License names and package.use lines come only from the human or from emerge --pretend output the human shows; "
        "never invent or guess license names or package.use lines."
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO)  # stderr; stdout carries the protocol
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
