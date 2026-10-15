"""Run records under runs/<run_id>/: status, single-run lock, cancel flag, log, credentials.

Every read-modify-write of run.json and every change to the lock file happens
under one inter-process mutex (flock on runs/.mutex). Readers of run.json do
not need it because writes are atomic. The mutex is not re-entrant, so public
functions take it once and delegate to private *_unlocked helpers.

Background runs are supported on macOS and Linux only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

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


@contextmanager
def _mutex() -> Iterator[None]:
    """Exclusive inter-process mutex. Not re-entrant: never nest it."""
    if fcntl is None:
        raise AgentError(CONFIG_INVALID, "Background runs need macOS or Linux; on Windows use WSL.", {})
    root = paths.runs_dir()
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".mutex").open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _parse_aware(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _is_active_status(value: object) -> bool:
    return isinstance(value, str) and value in ACTIVE_STATUSES


def _write_run_unlocked(run_id: str, record: dict) -> None:
    record["updated_at"] = now()
    save_json(run_dir(run_id) / "run.json", record)


def _update_run_unlocked(run_id: str, **changes: object) -> dict:
    record = read_run(run_id)
    record.update(changes)
    _write_run_unlocked(run_id, record)
    return record


def write_run(run_id: str, record: dict) -> None:
    run_dir(run_id)
    with _mutex():
        _write_run_unlocked(run_id, record)


def read_run(run_id: str) -> dict:
    path = run_dir(run_id) / "run.json"
    try:
        record = load_json(path)
    except FileNotFoundError:
        raise AgentError(RUN_NOT_FOUND, f"Run {run_id} does not exist.", {"run_id": run_id}) from None
    except (OSError, ValueError) as error:
        raise AgentError(CONFIG_INVALID, f"Run record {run_id} is unreadable: {error}", {"run_id": run_id}) from None
    if not isinstance(record, dict):
        raise AgentError(CONFIG_INVALID, f"Run record {run_id} is not a JSON object.", {"run_id": run_id})
    return record


def update_run(run_id: str, **changes: object) -> dict:
    run_dir(run_id)
    with _mutex():
        return _update_run_unlocked(run_id, **changes)


def pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass  # not our child: probe it with a null signal
    else:
        return reaped == 0  # nonzero means our child exited and is now reaped
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_path() -> Path:
    return paths.runs_dir() / "active.lock"


def _lock_holder() -> str | None:
    try:
        return lock_path().read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def _lock_recent() -> bool:
    """True when the lock file exists and was written within STARTING_GRACE."""
    try:
        written = dt.datetime.fromtimestamp(lock_path().stat().st_mtime, dt.timezone.utc)
    except FileNotFoundError:
        return False
    return dt.datetime.now(dt.timezone.utc) - written < STARTING_GRACE


def _record_active(record: dict | None, fallback: bool) -> bool:
    """Whether a run record describes a live run; `fallback` decides malformed records."""
    if not isinstance(record, dict):
        return fallback
    status = record.get("status")
    if not isinstance(status, str):
        return fallback
    if status not in ACTIVE_STATUSES:
        return False
    pid = record.get("pid")
    if pid is None:
        started = _parse_aware(record.get("started_at"))
        if started is None:
            return fallback
        return dt.datetime.now(dt.timezone.utc) - started < STARTING_GRACE
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return fallback
    return pid_alive(pid)


def _holder_active(run_id: str) -> bool:
    """Fail closed: a holder whose record is missing or broken counts as active
    until the lock file is older than STARTING_GRACE. Caller holds the mutex."""
    try:
        record = read_run(run_id)
    except AgentError:
        record = None
    return _record_active(record, fallback=_lock_recent())


def _write_lock_unlocked(run_id: str) -> None:
    path = lock_path()
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".active.lock.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(run_id)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _release_lock_unlocked(run_id: str) -> None:
    if _lock_holder() == run_id:
        lock_path().unlink(missing_ok=True)


def acquire_lock(run_id: str, record: dict) -> None:
    """Take the single-run lock and write the run record, both under the mutex.

    The record is written before the lock names the run, so a holder always has
    a record. Raises RUN_ALREADY_ACTIVE while the current holder is active.
    """
    run_dir(run_id)
    with _mutex():
        if lock_path().exists():
            holder = _lock_holder()
            active = _holder_active(holder) if holder else _lock_recent()
            if active:
                message = f"Run {holder} is still active." if holder else "Another run holds the run lock."
                raise AgentError(RUN_ALREADY_ACTIVE, message, {"run_id": holder})
        _write_run_unlocked(run_id, record)
        _write_lock_unlocked(run_id)


def release_lock(run_id: str) -> None:
    with _mutex():
        _release_lock_unlocked(run_id)


def claim_run(run_id: str, pid: int) -> dict:
    """Called by the worker before doing anything: starting -> running with its pid."""
    run_dir(run_id)
    with _mutex():
        record = read_run(run_id)
        status = record.get("status")
        if status != "starting":
            raise AgentError(CONFIG_INVALID, f"Run {run_id} is no longer startable (status {status}).",
                             {"run_id": run_id, "status": status})
        if _lock_holder() != run_id:
            raise AgentError(CONFIG_INVALID, f"Run {run_id} does not hold the run lock.", {"run_id": run_id})
        record["status"] = "running"
        record["pid"] = pid
        _write_run_unlocked(run_id, record)
        return record


def request_cancel(run_id: str) -> None:
    read_run(run_id)
    (run_dir(run_id) / "cancel").touch()


def cancel_requested(run_id: str) -> bool:
    return (run_dir(run_id) / "cancel").exists()


def run_status(run_id: str) -> dict:
    purge_stale_credentials()
    run_dir(run_id)
    with _mutex():
        record = read_run(run_id)
        if _is_active_status(record.get("status")):
            fallback = _lock_recent() if _lock_holder() == run_id else False
            if not _record_active(record, fallback=fallback):
                record = _update_run_unlocked(
                    run_id,
                    status="interrupted",
                    last_error="The worker process ended without recording a result.",
                    finished_at=now(),
                )
                _release_lock_unlocked(run_id)
        return record


def credentials_path(run_id: str) -> Path:
    return run_dir(run_id) / "credentials.json"


def write_credentials(run_id: str, payload: dict) -> None:
    save_json(credentials_path(run_id), {**payload, "written_at": now()}, mode=0o600)


def take_credentials(run_id: str) -> dict | None:
    """Return the credentials once: the first caller to rename the file owns it."""
    path = credentials_path(run_id)
    claimed = path.with_name(f".credentials.{secrets.token_hex(4)}.taken")
    try:
        os.rename(path, claimed)
    except FileNotFoundError:
        return None
    try:
        return load_json(claimed)
    except ValueError as error:
        raise AgentError(CONFIG_INVALID, f"Credentials of run {run_id} are unreadable: {error}", {"run_id": run_id}) from None
    finally:
        claimed.unlink(missing_ok=True)


def _credentials_written_at(path: Path) -> dt.datetime:
    try:
        written = _parse_aware(load_json(path).get("written_at"))
    except (OSError, ValueError, AttributeError):
        written = None
    if written is None:
        written = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
    return written


def purge_stale_credentials(*, at: dt.datetime | None = None) -> None:
    """Delete credentials older than CREDENTIALS_MAX_AGE. Never raises on bad files."""
    root = paths.runs_dir()
    if not root.exists():
        return
    moment = at or dt.datetime.now(dt.timezone.utc)
    candidates = [*root.glob("*/credentials.json"), *root.glob("*/.credentials.*.taken")]
    for path in candidates:
        try:
            if moment - _credentials_written_at(path) > CREDENTIALS_MAX_AGE:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def append_log(run_id: str, entry: dict) -> None:
    path = run_dir(run_id) / "log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_log(run_id: str, offset: int = 0, limit: int = 50) -> dict:
    """Page through complete log lines; a partially written last line is skipped."""
    if offset < 0 or not 1 <= limit <= MAX_LOG_PAGE:
        raise AgentError(CONFIG_INVALID, f"offset must be >= 0 and limit between 1 and {MAX_LOG_PAGE}", {"offset": offset, "limit": limit})
    read_run(run_id)
    path = run_dir(run_id) / "log.jsonl"
    try:
        lines = path.read_bytes().split(b"\n")[:-1]
    except FileNotFoundError:
        lines = []
    entries = []
    for index, line in enumerate(lines[offset:offset + limit], start=offset):
        try:
            entries.append(json.loads(line.decode("utf-8")))
        except ValueError:
            entries.append({"corrupt": True, "line": index})
    return {"entries": entries, "next_offset": offset + len(entries), "total": len(lines)}
