"""Phase selection, dry-run rendering, and execution of an approved plan.

execute_plan runs the commands stored in the plan (never regenerated ones),
after confirming the live disk still matches the plan. The target is never
rebooted. Every outcome, including exceptions and interrupts, is recorded in
the run record.
"""

from __future__ import annotations

from typing import Callable

from gentoo_agent.core.config import now
from gentoo_agent.core.disks import check_drift
from gentoo_agent.core.errors import CONFIG_INVALID, PHASE_FAILED, TARGET_DRIFT, AgentError
from gentoo_agent.core.phases import PHASES, command_for
from gentoo_agent.core.runs import append_log, cancel_requested, read_run, update_run
from gentoo_agent.core.ssh import remote_shell


EventSink = Callable[[str], None]
OUTPUT_TAIL_LINES = 40


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


def output_tail(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


def execute_plan(
    plan: dict,
    run_id: str,
    password_records: str | None,
    *,
    on_event: EventSink | None = None,
    on_output: EventSink | None = None,
) -> dict:
    emit = on_event or (lambda message: None)
    target = plan["target"]
    phases = plan["phases"]
    completed: list[str] = []
    authentication_applied = False
    current: str | None = None

    def finish(status: str, *, error: dict | None = None, tail: str = "") -> dict:
        update_run(
            run_id,
            status=status,
            finished_at=now(),
            completed_phases=list(completed),
            last_error=error["message"] if error else None,
            error=error,
        )
        return {
            "status": status,
            "failed_phase": current if error else None,
            "error": error,
            "output_tail": tail,
            "completed_phases": list(completed),
            "authentication_applied": authentication_applied,
        }

    def step(phase: dict, kind: str, label: str, command: str, input_data: str | None = None) -> dict:
        result = remote_shell(target, label, command, stream=True, input_data=input_data, on_output=on_output)
        append_log(run_id, {
            "phase": phase["id"],
            "kind": kind,
            "label": label,
            "command": command,
            "returncode": result["returncode"],
            "stdout": result["stdout"],
            "finished_at": result["finished_at"],
        })
        return result

    def phase_error(message: str, returncode: int) -> dict:
        return AgentError(PHASE_FAILED, message, {"phase": current, "returncode": returncode}).to_dict()

    record = read_run(run_id)
    if record.get("status") != "running":
        raise AgentError(
            CONFIG_INVALID,
            f"Run {run_id} must be claimed before execution (status {record.get('status')}).",
            {"run_id": run_id},
        )
    update_run(run_id, phase_count=len(phases), phase_index=0)
    try:
        if not isinstance(plan.get("disks"), list) or not plan["disks"]:
            raise AgentError(TARGET_DRIFT, "The plan has no disk identity to verify.", {})
        emit("checking that the target disk still matches the plan")
        check_drift(target, plan["disks"])
        for index, phase in enumerate(phases, start=1):
            if cancel_requested(run_id):
                emit("cancel requested; stopping before the next phase")
                current = None
                return finish("cancelled")
            current = phase["id"]
            update_run(run_id, current_phase=current, phase_index=index, completed_phases=list(completed))
            emit(f"===== checking {current} ({index}/{len(phases)}) =====")
            check = step(phase, "check", f"{current}-check", phase["check"])
            if check["returncode"] == 0:
                emit(f"{current}: already complete; skipped")
                completed.append(current)
                continue
            emit(f"===== applying {current} =====")
            input_data = None
            if current == "authentication":
                input_data = password_records
                authentication_applied = True  # apply was attempted; passwords may be set even if it failed
            result = step(phase, "apply", current, phase["apply"], input_data)
            if result["returncode"] != 0:
                return finish(
                    "failed",
                    error=phase_error(f"{current} apply returned {result['returncode']}", result["returncode"]),
                    tail=output_tail(result["stdout"]),
                )
            emit(f"===== verifying {current} =====")
            verify = step(phase, "verify", f"{current}-verify", phase["check"])
            if verify["returncode"] != 0:
                return finish(
                    "failed",
                    error=phase_error(f"{current} verification returned {verify['returncode']}", verify["returncode"]),
                    tail=output_tail(verify["stdout"]),
                )
            completed.append(current)
    except AgentError as error:
        return finish("failed", error=error.to_dict())
    except Exception as exc:
        return finish(
            "failed",
            error=AgentError(PHASE_FAILED, f"Installation stopped: {exc}", {"phase": current, "exception": type(exc).__name__}).to_dict(),
        )
    except BaseException:
        finish("interrupted", error=AgentError(PHASE_FAILED, "Installation was interrupted.", {"phase": current}).to_dict())
        raise
    # Outside the try: a transient bookkeeping error must not turn a success into "failed".
    current = None
    return finish("installed_pending_reboot")
