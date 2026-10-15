"""MCP server: a thin adapter over gentoo_agent.core and gentoo_agent.jobs.

Never write to stdout: it carries the protocol. Every tool returns a
CallToolResult with structured_content; errors carry {code, message, details}
and is_error. Approval is recorded only after a human accepts an elicitation
dialog built by this server, and the server never returns passwords.
"""

from __future__ import annotations

import json
import logging

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from gentoo_agent.core.approval import approval_status, approval_summary
from gentoo_agent.core.config import load_plan
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.install_plan import load_install_plan
from gentoo_agent.core.runs import read_log


logger = logging.getLogger("gentoo_agent.mcp")

INSTRUCTIONS = (
    "Installs Gentoo on a disposable target over SSH. Workflow: detect_hardware(refresh=true), "
    "resolve_config to preview, create_plan, approve_plan (a human confirms in a dialog), "
    "start_install, then get_run_status and get_run_log until the run ends. Never ask for or "
    "pass passwords as text; reference environment variables with {\"env\": \"NAME\"}."
)

server = MCPServer("gentoo-agent", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
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


def plan_view(plan: dict) -> dict:
    return {
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "created_at": plan["created_at"],
        "summary": approval_summary(plan),
        "target": plan["target"],
        "disks": plan["disks"],
        "phases": [{"id": phase["id"], "destructive": phase["destructive"]} for phase in plan["phases"]],
        "approval": approval_status(plan["plan_id"]),
    }


def _not_implemented() -> CallToolResult:
    return error_result(AgentError(CONFIG_INVALID, "not implemented", {}))


@server.tool(annotations=READ_ONLY)
def list_phases() -> CallToolResult:
    """List the installation phases, their order, and which ones change the target."""
    return respond(lambda: {"phases": [
        {"id": item["id"], "description": item.get("description", ""),
         "destructive": bool(item.get("destructive", True)), "handbook_sections": item.get("handbook_sections", [])}
        for item in load_plan().get("phases", [])
    ]})


@server.tool(annotations=READ_ONLY)
def get_plan(plan_id: str) -> CallToolResult:
    """Show a stored plan, its summary, and its approval status."""
    return respond(lambda: plan_view(load_install_plan(plan_id)))


@server.tool(annotations=READ_ONLY)
def get_run_log(run_id: str, offset: int = 0, limit: int = 50) -> CallToolResult:
    """Read part of a run's log (offset and limit count log lines)."""
    return respond(read_log, run_id, offset, limit)


# Placeholders, replaced in Tasks 6 to 8.
@server.tool(annotations=LOCAL_WRITE)
def detect_hardware(refresh: bool = False) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=READ_ONLY)
def resolve_config(overrides: dict | None = None, fallbacks: dict | None = None) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=LOCAL_WRITE)
def create_plan(overrides: dict | None = None, fallbacks: dict | None = None) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=LOCAL_WRITE)
async def approve_plan(plan_id: str, ctx: Context) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def start_install(plan_id: str) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def run_phase(plan_id: str, phases: list[str]) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def resume_run(run_id: str) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=LOCAL_WRITE)
def cancel_run(run_id: str) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=READ_ONLY)
def get_run_status(run_id: str) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=REMOTE_READ)
def verify_install(plan_id: str) -> CallToolResult:
    return _not_implemented()


def main() -> int:
    logging.basicConfig(level=logging.INFO)  # stderr; stdout carries the protocol
    server.run()
    return 0
