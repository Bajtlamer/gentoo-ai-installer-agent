"""Guarded executor: per phase, read-only check, apply, then verify.

The target is never rebooted. State in state/state.json makes reruns resume:
a phase whose check passes is skipped.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from typing import Callable

from gentoo_agent.core.config import now, save_json
from gentoo_agent.core.credentials import PasswordPrompt, password_input
from gentoo_agent.core.errors import APPROVAL_REQUIRED, CONFIG_INVALID, PHASE_FAILED, AgentError
from gentoo_agent.core.paths import LOG_DIR
from gentoo_agent.core.phases import PHASES, check_command_for, command_for
from gentoo_agent.core.ssh import remote_shell
from gentoo_agent.core.state import load_state, save_state


EventSink = Callable[[str], None]


def install_phrase(target: dict, install: dict) -> str:
    return f"INSTALL {target['name']} ON {install['disk']}"


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


def execution_gate(target: dict, install: dict, plan: dict, confirmation: str) -> None:
    expected = install_phrase(target, install)
    if confirmation != expected:
        raise AgentError(APPROVAL_REQUIRED, f"Refusing execution. Use --confirm {expected!r} after review.")
    if install.get("review_status") != "approved_for_execution":
        raise AgentError(APPROVAL_REQUIRED, "Installation is not approved. Run the review/approval command first.")
    if not all(item.get("execution_enabled") is True for item in (target, install, plan)):
        raise AgentError(APPROVAL_REQUIRED, "Execution is disabled in target.json, install.json, or plan.json.")
    if install.get("disk_must_be_erased") and not (
        target.get("target_is_disposable")
        and target.get("disk_erase_authorized")
        and install.get("target_is_disposable")
        and install.get("disk_erase_authorized")
    ):
        raise AgentError(APPROVAL_REQUIRED, "Disk erase is not fully authorized for this target.")


def execute(
    target: dict,
    install: dict,
    phases: list[str],
    *,
    prompt: PasswordPrompt | None = None,
    on_event: EventSink | None = None,
    on_output: EventSink | None = None,
) -> dict:
    emit = on_event or (lambda message: None)
    state = load_state()
    state.update({"target": target["name"], "status": "running", "current_phase": phases[0]})
    state.setdefault("completed_phases", [])
    save_state(state)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results: list[dict] = []
    generated_credentials: dict[str, str] = {}
    log_path = LOG_DIR / f"install-{target['name']}-{stamp}.json"

    def record(result: dict, kind: str) -> None:
        result["kind"] = kind
        results.append(result)
        save_json(log_path, {
            "target": target["name"],
            "started_at": stamp,
            "finished_at": now(),
            "phases": results,
        })

    def failed(phase: str, result: dict, error: str) -> dict:
        state.update({"status": "failed", "last_error": error})
        save_state(state)
        return {
            "status": "failed",
            "failed_phase": phase,
            "returncode": result["returncode"] or 1,
            "stderr": result["stderr"],
            "log_path": str(log_path),
            "generated_credentials": generated_credentials,
        }

    try:
        for phase in phases:
            state["current_phase"] = phase
            save_state(state)
            emit(f"===== checking {phase} =====")
            emit(f"  {now()} read-only state check")
            check = remote_shell(target, f"{phase}-check", check_command_for(phase, target, install), stream=True, on_output=on_output)
            record(check, "check")
            if check["returncode"] == 0:
                emit(f"{phase}: already complete; skipped")
                if phase not in state["completed_phases"]:
                    state["completed_phases"].append(phase)
                save_state(state)
                continue

            if phase in state["completed_phases"]:
                state["completed_phases"].remove(phase)
            emit(f"===== applying {phase} (live output) =====")
            emit(f"  {now()} sending approved commands to target")
            input_data = None
            if phase == "authentication":
                input_data, phase_generated = password_input(install, prompt)
                generated_credentials.update(phase_generated)
            result = remote_shell(
                target,
                phase,
                command_for(phase, target, install),
                stream=True,
                input_data=input_data,
                on_output=on_output,
            )
            record(result, "apply")
            if result["returncode"] != 0:
                return failed(phase, result, f"{phase} apply returned {result['returncode']}")

            emit(f"===== verifying {phase} =====")
            emit(f"  {now()} read-only postcondition check")
            verify = remote_shell(target, f"{phase}-verify", check_command_for(phase, target, install), stream=True, on_output=on_output)
            record(verify, "verify")
            if verify["returncode"] != 0:
                return failed(phase, verify, f"{phase} verification returned {verify['returncode']}")
            if phase not in state["completed_phases"]:
                state["completed_phases"].append(phase)
            save_state(state)
        state.update({"status": "installed_pending_reboot", "current_phase": "validation", "last_error": None})
        save_state(state)
        return {
            "status": "installed_pending_reboot",
            "failed_phase": None,
            "returncode": 0,
            "stderr": "",
            "log_path": str(log_path),
            "generated_credentials": generated_credentials,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        state.update({"status": "failed", "last_error": str(exc)})
        save_state(state)
        raise AgentError(
            PHASE_FAILED,
            f"Installation stopped: {exc}",
            {"phase": state.get("current_phase"), "log_path": str(log_path)},
        ) from exc
