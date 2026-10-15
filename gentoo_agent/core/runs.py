"""Run records under runs/<run_id>/: status, single-run lock, cancel flag, log, credentials.

While a run is active only its worker writes run.json. A reader may mark the
run interrupted when the worker process no longer exists.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
from pathlib import Path

from gentoo_agent.core import paths
from gentoo_agent.core.config import load_json, now, save_json
from gentoo_agent.core.errors import CONFIG_INVALID, RUN_ALREADY_ACTIVE, RUN_NOT_FOUND, AgentError


RUN_ID_PATTERN = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
ACTIVE_STATUSES = {"starting", "running"}
STARTING_GRACE = dt.timedelta(seconds=60)
CREDENTIALS_MAX_AGE = dt.timedelta(hours=24)
MAX_LOG_PAGE = 500


def new_run_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def run_dir(run_id: str) -> Path:
    if not RUN_ID_PATTERN.fullmatch(str(run_id or "")):
        raise AgentError(CONFIG_INVALID, f"Invalid run id: {run_id!r}", {"run_id": run_id})
    return paths.runs_dir() / run_id


def write_run(run_id: str, record: dict) -> None:
    record["updated_at"] = now()
    save_json(run_dir(run_id) / "run.json", record)


def read_run(run_id: str) -> dict:
    path = run_dir(run_id) / "run.json"
    if not path.exists():
        raise AgentError(RUN_NOT_FOUND, f"Run {run_id} does not exist.", {"run_id": run_id})
    return load_json(path)


def update_run(run_id: str, **changes: object) -> dict:
    record = read_run(run_id)
    record.update(changes)
    write_run(run_id, record)
    return record


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_active(run_id: str) -> bool:
    try:
        record = read_run(run_id)
    except AgentError:
        return False
    if record.get("status") not in ACTIVE_STATUSES:
        return False
    if record.get("pid") is None:
        started = dt.datetime.fromisoformat(record["started_at"])
        return dt.datetime.now(dt.timezone.utc) - started < STARTING_GRACE
    return pid_alive(record["pid"])


def lock_path() -> Path:
    return paths.runs_dir() / "active.lock"


def _lock_holder() -> str | None:
    try:
        return lock_path().read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def acquire_lock(run_id: str) -> None:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = _lock_holder()
            if holder and _is_active(holder):
                raise AgentError(RUN_ALREADY_ACTIVE, f"Run {holder} is still active.", {"run_id": holder})
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(run_id)
        return
    raise AgentError(RUN_ALREADY_ACTIVE, "Could not acquire the run lock.", {})


def release_lock(run_id: str) -> None:
    if _lock_holder() == run_id:
        lock_path().unlink(missing_ok=True)


def request_cancel(run_id: str) -> None:
    read_run(run_id)
    (run_dir(run_id) / "cancel").touch()


def cancel_requested(run_id: str) -> bool:
    return (run_dir(run_id) / "cancel").exists()


def run_status(run_id: str) -> dict:
    purge_stale_credentials()
    record = read_run(run_id)
    if record.get("status") in ACTIVE_STATUSES and not _is_active(run_id):
        record = update_run(
            run_id,
            status="interrupted",
            last_error="The worker process ended without recording a result.",
            finished_at=now(),
        )
        release_lock(run_id)
    return record


def credentials_path(run_id: str) -> Path:
    return run_dir(run_id) / "credentials.json"


def write_credentials(run_id: str, payload: dict) -> None:
    save_json(credentials_path(run_id), {**payload, "written_at": now()}, mode=0o600)


def take_credentials(run_id: str) -> dict | None:
    path = credentials_path(run_id)
    if not path.exists():
        return None
    data = load_json(path)
    path.unlink()
    return data


def purge_stale_credentials(*, at: dt.datetime | None = None) -> None:
    root = paths.runs_dir()
    if not root.exists():
        return
    moment = at or dt.datetime.now(dt.timezone.utc)
    for path in root.glob("*/credentials.json"):
        written = dt.datetime.fromisoformat(load_json(path)["written_at"])
        if moment - written > CREDENTIALS_MAX_AGE:
            path.unlink(missing_ok=True)


def append_log(run_id: str, entry: dict) -> None:
    path = run_dir(run_id) / "log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_log(run_id: str, offset: int = 0, limit: int = 50) -> dict:
    if offset < 0 or not 1 <= limit <= MAX_LOG_PAGE:
        raise AgentError(CONFIG_INVALID, f"offset must be >= 0 and limit between 1 and {MAX_LOG_PAGE}", {"offset": offset, "limit": limit})
    read_run(run_id)
    path = run_dir(run_id) / "log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    entries = [json.loads(line) for line in lines[offset:offset + limit]]
    return {"entries": entries, "next_offset": offset + len(entries), "total": len(lines)}
