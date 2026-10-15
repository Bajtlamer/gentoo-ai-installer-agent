"""Start, resume, and cancel background installation runs.

start_run() is the only code path that starts work. It enforces the approval
gate, resolves passwords before any phase runs, and hands them to a detached
worker over a pipe. Nothing here prints or prompts; a password prompt, when
configured, comes from the front end as a callback.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import gentoo_agent
from gentoo_agent.core import paths
from gentoo_agent.core.approval import require_approved_plan
from gentoo_agent.core.config import now
from gentoo_agent.core.credentials import PasswordPrompt, password_input
from gentoo_agent.core.errors import PHASE_FAILED, RUN_ALREADY_ACTIVE, AgentError
from gentoo_agent.core.runs import (
    ACTIVE_STATUSES,
    acquire_lock,
    new_run_id,
    release_lock,
    request_cancel,
    run_dir,
    run_status,
    update_run,
)


PACKAGE_PARENT = Path(gentoo_agent.__file__).resolve().parents[1]


def worker_command(run_id: str) -> list[str]:
    # -P: never prepend the working directory to sys.path, so it cannot shadow modules.
    return [sys.executable, "-P", "-m", "gentoo_agent.jobs.worker", run_id]


def worker_environment(exclude: set[str] | frozenset[str] = frozenset()) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if name not in exclude}
    environment["GENTOO_AGENT_HOME"] = str(paths.root())
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(PACKAGE_PARENT) + (os.pathsep + existing if existing else "")
    return environment


def start_run(plan_id: str, *, prompt: PasswordPrompt | None = None, resumed_from: str | None = None) -> str:
    plan = require_approved_plan(plan_id)
    install = plan["install"]
    password_variables = {
        str(name) for name in (install.get("root_password_env"), install.get("user_password_env")) if name
    }
    password_records, generated = password_input(install, prompt)
    run_id = new_run_id()
    acquire_lock(run_id, {
        "run_id": run_id,
        "plan_id": plan_id,
        "plan_hash": plan["plan_hash"],
        "status": "starting",
        "pid": None,
        "started_at": now(),
        "phase_count": len(plan["phases"]),
        "phase_index": 0,
        "current_phase": None,
        "completed_phases": [],
        "last_error": None,
        "resumed_from": resumed_from,
    })
    process = None
    handing_over = False
    try:
        with (run_dir(run_id) / "worker.err").open("ab") as worker_errors:
            process = subprocess.Popen(
                worker_command(run_id),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=worker_errors,
                cwd=paths.root(),
                env=worker_environment(exclude=password_variables),
                start_new_session=True,
            )
        update_run(run_id, pid=process.pid)
        payload = {"password_records": password_records, "generated_credentials": generated}
        handing_over = True
        process.stdin.write(json.dumps(payload).encode("utf-8"))
        process.stdin.close()
    except BaseException as exc:
        if process is not None:
            # A worker that never got its payload must not linger.
            try:
                process.kill()
            except OSError:
                pass
        error = AgentError(PHASE_FAILED, f"Could not start the worker: {exc}", {"run_id": run_id})
        try:
            update_run(run_id, status="failed", last_error=error.message, error=error.to_dict(), finished_at=now())
        except AgentError:
            pass
        release_lock(run_id)
        if handing_over and isinstance(exc, OSError):  # BrokenPipeError: the worker is already gone
            raise AgentError(PHASE_FAILED, "The worker exited before receiving its start payload.",
                             {"run_id": run_id}) from exc
        raise
    return run_id


def resume_run(run_id: str, *, prompt: PasswordPrompt | None = None) -> str:
    record = run_status(run_id)
    if record["status"] in ACTIVE_STATUSES:
        raise AgentError(RUN_ALREADY_ACTIVE, f"Run {run_id} is still active.", {"run_id": run_id})
    return start_run(record["plan_id"], prompt=prompt, resumed_from=run_id)


def cancel_run(run_id: str) -> dict:
    record = run_status(run_id)
    if record["status"] not in ACTIVE_STATUSES:
        return {"run_id": run_id, "cancel_requested": False, "status": record["status"]}
    request_cancel(run_id)
    return {"run_id": run_id, "cancel_requested": True, "status": record["status"]}
