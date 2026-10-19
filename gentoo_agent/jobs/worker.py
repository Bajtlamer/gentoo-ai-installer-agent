"""Detached worker process: runs one approved plan and records the outcome.

Started only by jobs.launcher.start_run(). It reads and validates the start
payload from stdin, claims the run, re-checks the approval, writes generated
credentials for one-time pickup (pending until the plan returns), runs the
plan, and always releases the run lock. It holds runs/<run_id>/worker.lock
from before the claim until after the run lock is released, and hands its
descriptor to remote_shell for every phase, whose holder process keeps it
until ssh exits, so the lock outlives a killed worker until that remote
program exits. A missing or malformed payload fails
the run without executing anything.
"""

from __future__ import annotations

import json
import os
import sys

from gentoo_agent.core.approval import require_approved_plan
from gentoo_agent.core.config import now
from gentoo_agent.core.errors import CONFIG_INVALID, PLAN_HASH_MISMATCH, AgentError
from gentoo_agent.core.install_plan import plan_kind
from gentoo_agent.core.runner import execute_plan
from gentoo_agent.core.runs import claim_run, hold_worker_lock, release_lock, update_run, write_credentials


PAYLOAD_KEYS = {"password_records", "generated_credentials"}


def parse_payload(text: str) -> dict:
    """Validate the launcher's start payload; anything else raises CONFIG_INVALID."""
    invalid = AgentError(CONFIG_INVALID, "The worker did not receive a valid start payload.", {})
    try:
        payload = json.loads(text)
    except ValueError:
        raise invalid from None
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS:
        raise invalid
    records = payload["password_records"]
    generated = payload["generated_credentials"]
    if records is not None and not isinstance(records, str):
        raise invalid
    if not isinstance(generated, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in generated.items()
    ):
        raise invalid
    return payload


def fail_unstarted(run_id: str, error: AgentError) -> None:
    """Claim the run and mark it failed without executing anything."""
    try:
        claim_run(run_id, os.getpid())
    except AgentError:
        return
    try:
        update_run(run_id, status="failed", last_error=error.message, error=error.to_dict(), finished_at=now())
    finally:
        release_lock(run_id)


def plan_needs_passwords(plan: dict) -> bool:
    if plan_kind(plan) != "install":
        return False
    install = plan.get("install") or {}
    return bool(install.get("set_root_password", True) or install.get("create_user", True))


def run(run_id: str, payload: dict) -> dict:
    """Run one claimed plan. Generated credentials are written with "applied": None
    (pending) before execution starts, so they survive a killed worker, and are
    rewritten with the real outcome when execute_plan returns."""
    generated = payload.get("generated_credentials") or {}

    def save_credentials(applied: bool | None) -> None:
        if generated:
            write_credentials(run_id, {"credentials": generated, "applied": applied})

    def fail(error: AgentError) -> dict:
        update_run(run_id, status="failed", last_error=error.message, error=error.to_dict(), finished_at=now())
        return {"status": "failed", "error": error.to_dict(), "authentication_applied": False}

    try:
        # Taken before claim_run, so a run is never "running" without a held lock.
        worker_lock = hold_worker_lock(run_id)
    except AgentError as error:
        # Another worker owns this run: leave its record and run lock alone.
        return {"status": "failed", "error": error.to_dict(), "authentication_applied": False}
    try:
        try:
            record = claim_run(run_id, os.getpid())
        except AgentError as error:
            # Not our run, or no longer startable: record nothing on it.
            return {"status": "failed", "error": error.to_dict(), "authentication_applied": False}
        try:
            plan = require_approved_plan(record["plan_id"])
            if plan["plan_hash"] != record.get("plan_hash"):
                raise AgentError(PLAN_HASH_MISMATCH, "The plan changed after the run was started.", {"run_id": run_id})
            password_records = payload.get("password_records")
            if password_records is None and plan_needs_passwords(plan):
                raise AgentError(CONFIG_INVALID, "No password records were handed to the worker.", {"run_id": run_id})
        except AgentError as error:
            save_credentials(False)
            return fail(error)
        save_credentials(None)
        try:
            # remote_shell runs each phase's ssh under a holder process that keeps the
            # lock descriptor until ssh exits, so a killed worker's lock stays held
            # until that remote program has finished.
            result = execute_plan(plan, run_id, password_records, phase_ids=record.get("phase_ids"),
                                  keep_fds=(worker_lock.fileno(),), holder_python=sys.executable)
        except AgentError as error:
            return fail(error)  # credentials stay pending
        save_credentials(bool(result.get("authentication_applied")))
        return result
    finally:
        try:
            release_lock(run_id)
        finally:
            worker_lock.close()


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 2
    run_id = arguments[0]
    try:
        try:
            text = sys.stdin.read()
        except (OSError, ValueError):  # unreadable or undecodable stdin counts as no payload
            text = ""
        payload = parse_payload(text)
    except AgentError as error:
        fail_unstarted(run_id, error)
        return 1
    result = run(run_id, payload)
    return 0 if result.get("status") == "installed_pending_reboot" else 1


if __name__ == "__main__":
    raise SystemExit(main())
