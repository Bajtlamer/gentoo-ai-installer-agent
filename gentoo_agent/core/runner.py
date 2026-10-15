"""Phase selection, dry-run rendering, and (later in this plan) plan execution."""

from __future__ import annotations

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.phases import PHASES, command_for


def select_phases(from_phase: str | None) -> list[str]:
    start = from_phase or PHASES[0]
    if start not in PHASES:
        raise AgentError(CONFIG_INVALID, f"Unknown start phase: {start}", {"phase": start})
    return PHASES[PHASES.index(start):]


def render_dry_run(target: dict, install: dict, phases: list[str]) -> str:
    lines = [
        f"Target: {target['user']}@{target['host']}:{target.get('port', 22)}",
        f"Disk:   {install['disk']} ({install.get('boot_mode')}/{install.get('partition_table')})",
        "Mode:   DRY RUN - no SSH connection and no target changes",
    ]
    for phase in phases:
        lines.append(f"\n===== {phase} =====")
        lines.append(command_for(phase, target, install))
    return "\n".join(lines)
