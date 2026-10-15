"""Detached worker process: runs one approved plan and records the outcome.

Started only by jobs.launcher.start_run(). It reads the password records from
stdin, claims the run, re-checks the approval, runs the plan, writes generated
credentials for one-time pickup, and always releases the run lock.
"""

from __future__ import annotations

import json
import os
import sys

from gentoo_agent.core.approval import require_approved_plan
from gentoo_agent.core.config import now
from gentoo_agent.core.errors import PLAN_HASH_MISMATCH, AgentError
from gentoo_agent.core.runner import execute_plan
from gentoo_agent.core.runs import claim_run, release_lock, update_run, write_credentials


def run(run_id: str, payload: dict) -> dict:
    result: dict = {"authentication_applied": False}
    try:
        try:
            record = claim_run(run_id, os.getpid())
        except AgentError as error:
            # Not our run, or no longer startable: record nothing on it.
            result = {"status": "failed", "error": error.to_dict(), "authentication_applied": False}
            return result
        plan = require_approved_plan(record["plan_id"])
        if plan["plan_hash"] != record.get("plan_hash"):
            raise AgentError(PLAN_HASH_MISMATCH, "The plan changed after the run was started.", {"run_id": run_id})
        result = execute_plan(plan, run_id, payload.get("password_records"))
        return result
    except AgentError as error:
        update_run(run_id, status="failed", last_error=error.message, error=error.to_dict(), finished_at=now())
        result = {"status": "failed", "error": error.to_dict(), "authentication_applied": False}
        return result
    finally:
        generated = payload.get("generated_credentials") or {}
        if generated:
            write_credentials(run_id, {"credentials": generated, "applied": bool(result.get("authentication_applied"))})
        release_lock(run_id)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 2
    payload = json.loads(sys.stdin.read() or "{}")
    result = run(arguments[0], payload)
    return 0 if result.get("status") == "installed_pending_reboot" else 1


if __name__ == "__main__":
    raise SystemExit(main())
