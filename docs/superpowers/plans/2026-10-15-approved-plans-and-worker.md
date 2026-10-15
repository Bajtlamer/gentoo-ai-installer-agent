# Approved Plans and Background Worker Implementation Plan (Plan 2 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flag-based execution gate with plans bound by `plan_hash`, human-only approval records, and a detached background worker that executes exactly the approved commands, so plan 3 can expose it through MCP.

**Architecture:** `create_install_plan()` freezes the validated config, the target disk identity (serial/WWN from a new read-only inventory command), and every generated phase command into `plans/<plan_id>.json`, hashed with SHA-256. `approve` (TTY-only CLI now, MCP elicitation in plan 3) writes `approvals/<plan_id>.json`. `jobs/launcher.start_run()` is the single entry point that starts work: it calls `require_approved_plan()`, resolves passwords before any phase runs, takes a lock, and spawns `python -m gentoo_agent.jobs.worker <run_id>`. The worker re-checks the approval and the live disk identity, then runs `core/runner.execute_plan()`, writing `runs/<run_id>/run.json` and `log.jsonl`. The old `execution_enabled` / `review_status` / `approve-install` / `install --execute` gate is removed.

**Tech Stack:** Python 3.12+ standard library, `unittest`, OpenSSH client.

**Spec:** `docs/superpowers/specs/2026-10-15-mcp-server-design.md`, build order steps 4 to 6. Decision recorded 2026-10-15: the `plan_hash` approval record replaces the execution flags entirely; the disposable-target and disk-erase flags remain as config-level authorization.

**Review findings from plan 1 addressed here:** gate inside the start path (Task 10), exception-safe executor (Task 9), paths resolved at call time (Task 3), passwords resolved before the first phase (Task 10), concurrent-safe JSON writes (Task 1), output tail instead of an always-empty stderr (Task 9), CLI-shaped confirmation phrase and `enable_execution` retired (Task 2), Handbook network errors wrapped (Task 4). Also: an empty password environment variable now means "generate" (spec decision), and a plan always requires full erase authorization because the storage phase always wipes the disk.

All commands run from the repository root. Use `python3` if `python` is not on `PATH`.

---

## File structure after this plan

```
gentoo_agent/
  cli.py                     + plan create/show, approve (TTY only), run start/status/log/cancel/resume
  core/
    paths.py                 functions resolved at call time (root(), config_dir(), plans_dir(), runs_dir(), ...)
    config.py                save_json: unique temp file, mode parameter
    errors.py                + new codes
    handbook.py              network errors -> HANDBOOK_UNAVAILABLE
    ssh.py                   + "disk_identity" read-only command
    disks.py         (new)   parse disk identity, live re-read, drift check
    install_plan.py  (new)   create, hash, save, load, integrity check
    approval.py      (new)   summary, record_approval, require_approved_plan, approval_status
    runs.py          (new)   run records, lock, cancel flag, log, one-time credentials
    runner.py                select_phases, render_dry_run, execute_plan
    credentials.py           empty env var -> generate
  jobs/
    __init__.py      (new)
    launcher.py      (new)   start_run, resume_run, cancel_run (the only way to start work)
    worker.py        (new)   detached process entry point
.claude/settings.json (new)  deny rules: the agent may not run `approve`
tests/
  helpers.py         (new)   temp_home()
  test_config_io.py, test_paths.py, test_cli.py, test_disks.py, test_install_plan.py,
  test_approval.py (rewritten), test_runs.py, test_execute_plan.py, test_launcher.py, test_worker.py
```

Removed: `core/approval.py` legacy functions (`approval_phrase(target, install)`, `enable_execution`), `runner.execute`, `runner.execution_gate`, `runner.install_phrase`, CLI `approve-install`, legacy `approve <phase>`, `install --confirm`; config keys `execution_enabled`, `review_status`, `dangerous_operations_enabled`.

---

### Task 1: Concurrency-safe `save_json`

`save_json` uses a fixed `<file>.tmp` name. Two writers (the worker and an MCP server reading state) race on it, and a failed write leaves the temp file behind.

**Files:**
- Modify: `gentoo_agent/core/config.py`
- Create: `tests/test_config_io.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_config_io.py`:

```python
import tempfile
import threading
import unittest
from pathlib import Path

from gentoo_agent.core.config import load_json, save_json


class SaveJsonTests(unittest.TestCase):
    def test_concurrent_writers_never_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            errors = []

            def writer(number):
                try:
                    for index in range(200):
                        save_json(target, {"writer": number, "index": index})
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(number,)) for number in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertIn("writer", load_json(target))
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["state.json"])

    def test_failed_write_keeps_old_content_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            save_json(target, {"ok": True})
            with self.assertRaises(TypeError):
                save_json(target, {"bad": object()})
            self.assertEqual(load_json(target), {"ok": True})
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["state.json"])

    def test_mode_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "secret.json"
            save_json(target, {"value": 1}, mode=0o600)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_config_io -v`
Expected: `test_failed_write_keeps_old_content_and_leaves_no_temporary_file` FAILS (a `state.json.tmp` remains), `test_mode_is_applied` ERRORS (`unexpected keyword argument 'mode'`), and `test_concurrent_writers_never_collide` almost always FAILS with `FileNotFoundError` entries in `errors`.

- [ ] **Step 3: Replace `save_json` in `gentoo_agent/core/config.py`**

Add `import os` and `import tempfile` to the imports and replace the function:

```python
def save_json(path: Path, value: dict, *, mode: int = 0o644) -> None:
    """Write JSON atomically through a unique temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
```

- [ ] **Step 4: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 60 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/config.py tests/test_config_io.py
git commit -m "Write JSON through unique temporary files"
```

---

### Task 2: Retire the flag-based execution gate

The `plan_hash` approval record (Tasks 6 to 11) becomes the only execution gate. This task removes the old gate first, so the two never coexist. Between this task and Task 11 the branch can render dry runs but cannot execute; that is intended.

**Files:**
- Modify: `gentoo_agent/core/runner.py`, `gentoo_agent/core/validate.py`, `gentoo_agent/cli.py`, `gentoo_agent/prepare.py`, `gentoo_agent/init_config.py`
- Delete: `gentoo_agent/core/approval.py`, `tests/test_approval.py`
- Modify: `config/plan.json`, `config/install.default.json`, `config/install.example.json`, `config/target.default.json`, `config/target.example.json`
- Modify: `tests/test_runner.py`, `tests/test_validate.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write the failing CLI test**

`tests/test_cli.py`:

```python
import io
import unittest
from contextlib import redirect_stderr

from gentoo_agent import cli


class InstallCommandTests(unittest.TestCase):
    def test_install_execute_points_to_the_plan_workflow(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.main(["install", "--execute"])
        self.assertEqual(code, 1)
        self.assertIn("error [APPROVAL_REQUIRED]: install --execute was replaced", stderr.getvalue())
        self.assertIn("gentoo-agent plan create", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_cli -v`
Expected: FAIL. The current code reaches `target_config()` and prints `error [CONFIG_INVALID]: Missing config/target.json...` (or, with a local config, the old gate message).

- [ ] **Step 3: Reduce `gentoo_agent/core/runner.py` to selection and dry run**

Replace the whole file with:

```python
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
```

- [ ] **Step 4: Delete the legacy approval module and its test**

Run:

```bash
git rm gentoo_agent/core/approval.py tests/test_approval.py
```

- [ ] **Step 5: Remove the flag checks from `gentoo_agent/core/validate.py`**

Delete this block:

```python
    execution_flags = [
        target.get("execution_enabled"),
        install.get("execution_enabled"),
        plan.get("execution_enabled"),
    ]
    if len(set(execution_flags)) != 1:
        errors.append("target, install, and plan execution_enabled flags must match")
    if any(execution_flags) and install.get("review_status") != "approved_for_execution":
        errors.append("execution_enabled=true requires approved_for_execution review status")
```

and this block:

```python
    if install.get("review_status") not in {
        "pending_operator_review", "reviewed_execution_disabled", "approved_for_execution"
    }:
        warnings.append("unrecognized review_status")
```

and replace the returned dict with:

```python
    return {
        "valid_for_execution": not errors,
        "errors": errors,
        "warnings": warnings,
        "target": target.get("name"),
        "installation": install,
    }
```

The `plan` parameter stays in the signature (callers pass it; plan 4 uses the phase list).

- [ ] **Step 6: Replace `gentoo_agent/cli.py`**

```python
"""Supervised Gentoo-over-SSH agent workflow tools."""

from __future__ import annotations

import argparse
import json
import sys

from gentoo_agent.core.config import install_config, load_plan, target_config
from gentoo_agent.core.errors import APPROVAL_REQUIRED, CONFIG_INVALID, AgentError
from gentoo_agent.core.handbook import fetch_handbook
from gentoo_agent.core.inventory import collect_inventory, latest_inventory, record_inventory
from gentoo_agent.core.phases import PHASES
from gentoo_agent.core.runner import render_dry_run, select_phases
from gentoo_agent.core.state import load_state, state_path
from gentoo_agent.core.validate import validate_installation, validated_settings


EXECUTE_REPLACED = (
    "install --execute was replaced by approved plans: run 'gentoo-agent plan create', "
    "approve the plan yourself in a terminal with 'gentoo-agent approve <plan_id>', "
    "then 'gentoo-agent run start <plan_id>'."
)


def cmd_handbook(_: argparse.Namespace) -> int:
    print(json.dumps(fetch_handbook(target_config()), indent=2))
    return 0


def cmd_inspect(_: argparse.Namespace) -> int:
    target = target_config()
    _, path = collect_inventory(target, on_progress=lambda label: print(f"Inspecting {label}...", file=sys.stderr))
    record_inventory(target, path)
    print(path)
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    if not state_path().exists():
        print("No state/state.json exists. Copy state.example.json first.")
        return 0
    print(json.dumps(load_state(), indent=2))
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    result = validate_installation(target_config(), install_config(), load_plan(), latest_inventory())
    print(json.dumps(result, indent=2))
    return 2 if result["errors"] else 0


def cmd_install(args: argparse.Namespace) -> int:
    if args.execute:
        raise AgentError(APPROVAL_REQUIRED, EXECUTE_REPLACED)
    target = target_config()
    install = install_config()
    validated_settings(target, install)
    if args.from_phase is None:
        print("Starting from the beginning; completed phases will be checked and skipped.")
    print(render_dry_run(target, install, select_phases(args.from_phase)))
    return 0


def cmd_phase(args: argparse.Namespace) -> int:
    plan = load_plan()
    phase = next((item for item in plan["phases"] if item["id"] == args.phase), None)
    if phase is None:
        raise AgentError(CONFIG_INVALID, f"Unknown phase: {args.phase}")
    if not phase.get("implemented"):
        raise AgentError(CONFIG_INVALID, f"Phase '{args.phase}' is not implemented. No target changes were made.")
    if args.phase == "discovery":
        return cmd_inspect(args)
    args.from_phase = args.phase
    args.execute = False
    return cmd_install(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("handbook", help="Fetch and record the configured Handbook URL").set_defaults(func=cmd_handbook)
    sub.add_parser("inspect", help="Run the allowlisted read-only SSH inventory").set_defaults(func=cmd_inspect)
    sub.add_parser("status", help="Print current local workflow state").set_defaults(func=cmd_status)
    sub.add_parser("validate", help="Validate installation settings without touching the target").set_defaults(func=cmd_validate)
    phase = sub.add_parser("phase", help="Print the dry run of one implemented phase")
    phase.add_argument("phase")
    phase.set_defaults(func=cmd_phase)
    install = sub.add_parser("install", help="Print the generated installation phases (dry run)")
    install.add_argument("--execute", action="store_true", help="Replaced by plan create, approve, and run start")
    install.add_argument("--dry-run", action="store_true", help="Print generated phases without connecting (default)")
    install.add_argument("--from-phase", choices=PHASES, default=None, help="First phase to print")
    install.set_defaults(func=cmd_install)
    return parser


def main(argv: list[str] | None = None) -> int:
    parsed = build_parser().parse_args(argv)
    try:
        result = parsed.func(parsed)
    except AgentError as error:
        print(f"error [{error.code}]: {error.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`tests/test_errors.py` patches `cli.cmd_status`; it still exists.

- [ ] **Step 7: Remove the flags from `gentoo_agent/prepare.py`**

In `ensure_target_config`, delete:

```python
    target["execution_enabled"] = False
    target["dangerous_operations_enabled"] = False
```

In `build_install_config`, delete these two entries from `install.update({...})`:

```python
        "execution_enabled": False,
        "review_status": "pending_operator_review",
```

In `main`, replace everything from `print("\nReview the printed configuration and config/install.json.")` up to (not including) the final `return 0` with:

```python
    print("\nReview the printed configuration and config/install.json.")
    print("Next: run 'gentoo-agent plan create', then approve the plan yourself in a terminal.")
```

- [ ] **Step 8: Remove the flags from `gentoo_agent/init_config.py`**

Delete from the `target.update({...})` call:

```python
        "execution_enabled": False,
        "dangerous_operations_enabled": False,
```

Delete from the `install.update({...})` call:

```python
        "execution_enabled": False,
        "review_status": "pending_operator_review",
```

Replace `print("Execution remains disabled; review both files before continuing.")` with:

```python
    print("Nothing can run until a plan is created and approved; review both files first.")
```

- [ ] **Step 9: Remove the flags from the JSON configs**

- `config/plan.json`: delete the `"execution_enabled"` and `"review_status"` lines.
- `config/install.default.json` and `config/install.example.json`: delete the `"execution_enabled"` and `"review_status"` lines and remove the trailing comma from the line that is now last.
- `config/target.default.json` and `config/target.example.json`: delete the `"dangerous_operations_enabled"` and `"execution_enabled"` lines and remove the trailing comma from `"install_config": "config/install.json",`.

Run:

```bash
python -c "import json; [json.load(open(f, encoding='utf-8')) for f in ['config/plan.json','config/install.default.json','config/install.example.json','config/target.default.json','config/target.example.json']]; print('JSON OK')"
grep -rnE "execution_enabled|review_status|dangerous_operations_enabled" config gentoo_agent
```

Expected: `JSON OK`, then no grep output.

- [ ] **Step 10: Update the affected tests**

In `tests/test_runner.py`:
- delete the `approved()` helper, the `ExecutionGateTests` class, and the `ExecuteTests` class (with its helpers and all five tests),
- remove imports that become unused (`tempfile`, `patch` only if unused by `DryRunTests`, `state`, `APPROVAL_REQUIRED`). `DryRunTests` still uses `patch`.

In `tests/test_validate.py`:
- delete `"review_status": "pending_operator_review",` from the `fixture()` update,
- delete `test_mismatched_execution_flags_are_an_error`.

- [ ] **Step 11: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 48 tests PASS (60 minus 13 removed plus 1 new), golden files unchanged (`git status --short tests/golden` prints nothing).

- [ ] **Step 12: Smoke-test the CLI**

Run:

```bash
python tools/gentoo_agent.py install --execute; echo "exit=$?"
python tools/gentoo_agent.py approve-install; echo "exit=$?"
```

Expected: first prints `error [APPROVAL_REQUIRED]: install --execute was replaced ...` and `exit=1`; second prints an argparse `invalid choice: 'approve-install'` error and `exit=2`.

- [ ] **Step 13: Commit**

```bash
git add -A gentoo_agent config tests
git commit -m "Retire the flag-based execution gate

execution_enabled, review_status, approve-install and install --execute
are removed; plan_hash-bound approval records replace them."
```

---

### Task 3: Paths resolved at call time

Module-level path constants are captured at import, so tests patch each module separately and a long-running server cannot follow `GENTOO_AGENT_HOME`. Replace them with functions.

**Files:**
- Replace: `gentoo_agent/core/paths.py`
- Modify: `gentoo_agent/core/config.py`, `state.py`, `inventory.py`, `handbook.py`, `ssh.py`, `gentoo_agent/init_config.py`, `gentoo_agent/prepare.py`
- Create: `tests/helpers.py`, `tests/test_paths.py`
- Modify: `tests/test_phases.py`, `tests/test_state_inventory.py`, `tests/test_handbook.py`

- [ ] **Step 1: Create the test helper**

`tests/helpers.py`:

```python
"""Shared test helpers."""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


@contextmanager
def temp_home():
    """Point GENTOO_AGENT_HOME at a fresh temporary directory for the duration of the block."""
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GENTOO_AGENT_HOME": tmp}):
        yield Path(tmp).resolve()
```

- [ ] **Step 2: Write the failing paths tests**

`tests/test_paths.py`:

```python
import unittest

from gentoo_agent.core import paths
from tests.helpers import temp_home


class PathsTests(unittest.TestCase):
    def test_root_follows_the_environment_at_call_time(self):
        with temp_home() as first:
            self.assertEqual(paths.root(), first)
            with temp_home() as second:
                self.assertEqual(paths.root(), second)

    def test_directories_live_under_root(self):
        with temp_home() as home:
            self.assertEqual(paths.config_dir(), home / "config")
            self.assertEqual(paths.state_dir(), home / "state")
            self.assertEqual(paths.inventory_dir(), home / "inventory")
            self.assertEqual(paths.log_dir(), home / "logs")
            self.assertEqual(paths.approval_dir(), home / "approvals")
            self.assertEqual(paths.plans_dir(), home / "plans")
            self.assertEqual(paths.runs_dir(), home / "runs")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m unittest tests.test_paths -v`
Expected: ERROR, `AttributeError: module 'gentoo_agent.core.paths' has no attribute 'root'`.

- [ ] **Step 4: Replace `gentoo_agent/core/paths.py`**

```python
"""Filesystem locations used by the agent, resolved on every call.

GENTOO_AGENT_HOME overrides the repository root. Resolving at call time lets a
long-running server, a background worker, and tests follow the current
environment without patching module constants.
"""

from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    configured = os.environ.get("GENTOO_AGENT_HOME")
    base = Path(configured) if configured else Path(__file__).resolve().parents[2]
    return base.expanduser().resolve()


def config_dir() -> Path:
    return root() / "config"


def state_dir() -> Path:
    return root() / "state"


def inventory_dir() -> Path:
    return root() / "inventory"


def log_dir() -> Path:
    return root() / "logs"


def approval_dir() -> Path:
    return root() / "approvals"


def plans_dir() -> Path:
    return root() / "plans"


def runs_dir() -> Path:
    return root() / "runs"
```

- [ ] **Step 5: Migrate every user of the old constants**

In each file, replace the `from gentoo_agent.core.paths import ...` line with `from gentoo_agent.core import paths` and change the uses:

| File | Old | New |
|---|---|---|
| `core/config.py` | `CONFIG / "target.json"` | `paths.config_dir() / "target.json"` |
| `core/config.py` | `ROOT / target.get(...)` | `paths.root() / target.get(...)` |
| `core/config.py` | `CONFIG / "plan.json"` | `paths.config_dir() / "plan.json"` |
| `core/state.py` | `STATE_DIR / "state.json"` | `paths.state_dir() / "state.json"` |
| `core/inventory.py` | `INVENTORY_DIR` (2 uses) | `paths.inventory_dir()` |
| `core/inventory.py` | `path.relative_to(ROOT)` | `path.relative_to(paths.root())` |
| `core/handbook.py` | `INVENTORY_DIR` (3 uses) | a local `inventory_dir = paths.inventory_dir()` set once at the top of `fetch_handbook`'s file-writing section, then `inventory_dir` |
| `core/handbook.py` | `html_path.relative_to(ROOT)` | `html_path.relative_to(paths.root())` |
| `core/ssh.py` | `cwd=ROOT` (3 uses) | `cwd=paths.root()` |
| `init_config.py` | `CONFIG / ...` (6 uses) | `paths.config_dir() / ...` |
| `prepare.py` | `CONFIG / ...` (5 uses) | `paths.config_dir() / ...` |
| `prepare.py` | `ROOT / ...`, `relative_to(ROOT)` (3 uses) | `paths.root() / ...`, `relative_to(paths.root())` |

Run: `grep -rnE "\b(ROOT|CONFIG|STATE_DIR|INVENTORY_DIR|LOG_DIR|APPROVAL_DIR)\b" gentoo_agent`
Expected: no output.

- [ ] **Step 6: Switch the tests that patched constants to `temp_home()`**

`tests/test_phases.py`, `ConfigLoadingTests.test_missing_target_config_is_invalid`:

```python
    def test_missing_target_config_is_invalid(self):
        with temp_home():
            with self.assertRaises(AgentError) as caught:
                config.target_config()
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
```

`tests/test_state_inventory.py`: replace the four `with tempfile.TemporaryDirectory() as tmp, patch.object(<module>, "<CONSTANT>", Path(tmp))...:` headers so the tests read:

```python
class StateTests(unittest.TestCase):
    def test_missing_state_has_defaults(self):
        with temp_home():
            self.assertEqual(state.load_state(), {"schema_version": 1, "completed_phases": []})

    def test_save_state_stamps_updated_at(self):
        with temp_home():
            state.save_state({"status": "running"})
            loaded = state.load_state()
        self.assertEqual(loaded["status"], "running")
        self.assertIn("updated_at", loaded)
```

and in `InventoryTests`:

```python
    def test_collect_inventory_runs_every_allowlisted_command_and_saves(self):
        seen = []
        with temp_home(), patch.object(inventory, "run_ssh_readonly", self.fake_ssh):
            data, path = inventory.collect_inventory(TARGET, on_progress=seen.append)
            self.assertTrue(path.exists())
        self.assertEqual(seen, list(READ_ONLY_COMMANDS))
        self.assertEqual(len(data["results"]), len(READ_ONLY_COMMANDS))
        self.assertTrue(data["read_only"])
        self.assertEqual(data["target_address"], "root@192.0.2.10:22")
        errors = next(item for item in data["results"] if item["label"] == "errors")
        self.assertIn("timed out", errors["error"])

    def test_latest_inventory_returns_newest_or_none(self):
        with temp_home():
            self.assertIsNone(inventory.latest_inventory())
            with patch.object(inventory, "run_ssh_readonly", self.fake_ssh):
                data, _ = inventory.collect_inventory(TARGET)
            self.assertEqual(inventory.latest_inventory(), data)
```

`tests/test_handbook.py`:

```python
    def test_fetch_records_url_title_and_creates_directory(self):
        with temp_home() as home, patch.object(handbook.urllib.request, "urlopen", return_value=FakeResponse()):
            metadata = handbook.fetch_handbook({"handbook_url": "https://wiki.gentoo.org/wiki/Handbook:AMD64"})
            self.assertTrue((home / metadata["saved_html"]).exists())
            self.assertTrue(metadata["saved_html"].startswith("inventory/"))
        self.assertEqual(metadata["url"], "https://wiki.gentoo.org/wiki/Handbook:AMD64")
        self.assertEqual(metadata["title"], "Handbook:AMD64 - Gentoo Wiki")
        self.assertEqual(metadata["content_type"], "text/html; charset=UTF-8")
        self.assertIn("retrieved_at", metadata)
```

Add `from tests.helpers import temp_home` to each of the three files and remove imports that become unused (`tempfile`, `Path`).

- [ ] **Step 7: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 50 tests PASS. Also run `python -m unittest tests.test_paths -v` from the repository root to confirm `from tests.helpers import temp_home` resolves both ways.

- [ ] **Step 8: Commit**

```bash
git add -A gentoo_agent tests
git commit -m "Resolve agent paths at call time"
```

---

### Task 4: Handbook network errors as `AgentError`

**Files:**
- Modify: `gentoo_agent/core/errors.py`, `gentoo_agent/core/handbook.py`
- Modify: `tests/test_handbook.py`

- [ ] **Step 1: Write the failing test** (append to `HandbookTests`)

```python
    def test_network_failure_is_reported_with_a_stable_code(self):
        import urllib.error

        from gentoo_agent.core.errors import HANDBOOK_UNAVAILABLE, AgentError

        with temp_home(), patch.object(
            handbook.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
        ):
            with self.assertRaises(AgentError) as caught:
                handbook.fetch_handbook({"handbook_url": "https://wiki.gentoo.org/wiki/Handbook:AMD64"})
        self.assertEqual(caught.exception.code, HANDBOOK_UNAVAILABLE)
        self.assertEqual(caught.exception.details, {"url": "https://wiki.gentoo.org/wiki/Handbook:AMD64"})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_handbook -v`
Expected: ERROR, `ImportError: cannot import name 'HANDBOOK_UNAVAILABLE'`.

- [ ] **Step 3: Add the code and wrap the request**

In `gentoo_agent/core/errors.py` add after `PHASE_FAILED`:

```python
HANDBOOK_UNAVAILABLE = "HANDBOOK_UNAVAILABLE"
```

In `gentoo_agent/core/handbook.py` import it (`from gentoo_agent.core.errors import HANDBOOK_UNAVAILABLE, AgentError`) and replace the request block with:

```python
    request = urllib.request.Request(url, headers={"User-Agent": "gentoo-agent/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
    except OSError as exc:
        raise AgentError(HANDBOOK_UNAVAILABLE, f"Could not fetch the Handbook from {url}: {exc}", {"url": url}) from exc
```

`urllib.error.URLError` and `TimeoutError` are both `OSError` subclasses.

- [ ] **Step 4: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 51 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core tests/test_handbook.py
git commit -m "Report Handbook network failures as HANDBOOK_UNAVAILABLE"
```

---

### Task 5: Disk identity and drift detection

A plan binds to the disk's permanent identity. A new read-only inventory command records serial and WWN; the worker re-reads it before any change.

**Files:**
- Modify: `gentoo_agent/core/errors.py`, `gentoo_agent/core/ssh.py`
- Create: `gentoo_agent/core/disks.py`, `tests/test_disks.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_disks.py`:

```python
import json
import unittest
from unittest.mock import patch

from gentoo_agent.core import disks
from gentoo_agent.core.errors import CONFIG_INVALID, INVENTORY_MISSING, SSH_UNREACHABLE, TARGET_DRIFT, AgentError
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS


LSBLK = json.dumps({"blockdevices": [
    {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK  ", "serial": "QM00001", "wwn": None},
    {"path": "/dev/sr0", "type": "rom", "size": 1073741312, "model": "QEMU DVD-ROM", "serial": "QM00003", "wwn": None},
    {"path": "/dev/vda", "type": "disk", "size": "21474836480", "model": None, "serial": None, "wwn": None},
]})
TARGET = {"name": "gentoo", "host": "192.0.2.10", "user": "root"}


def inventory_with(stdout: str, returncode: int = 0) -> dict:
    return {"results": [{"label": "disk_identity", "returncode": returncode, "stdout": stdout}]}


class DiskIdentityTests(unittest.TestCase):
    def test_inventory_has_a_disk_identity_command(self):
        self.assertIn("SERIAL", READ_ONLY_COMMANDS["disk_identity"])
        self.assertIn("WWN", READ_ONLY_COMMANDS["disk_identity"])

    def test_parse_keeps_disks_only_and_normalizes(self):
        parsed = disks.parse_disk_identities(LSBLK)
        self.assertEqual(parsed, [
            {"path": "/dev/sda", "size_bytes": 42949672960, "model": "QEMU HARDDISK", "serial": "QM00001", "wwn": ""},
            {"path": "/dev/vda", "size_bytes": 21474836480, "model": "", "serial": "", "wwn": ""},
        ])
        self.assertEqual(disks.identity_strength(parsed[0]), "strong")
        self.assertEqual(disks.identity_strength(parsed[1]), "weak")

    def test_inventory_without_identity_is_missing(self):
        with self.assertRaises(AgentError) as caught:
            disks.disk_from_inventory({"results": []}, "/dev/sda")
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_unknown_disk_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            disks.disk_from_inventory(inventory_with(LSBLK), "/dev/sdz")
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_check_drift_accepts_the_same_disk_and_rejects_a_different_one(self):
        expected = disks.disk_from_inventory(inventory_with(LSBLK), "/dev/sda")
        same = {"label": "disk_identity", "returncode": 0, "stdout": LSBLK, "stderr": ""}
        with patch.object(disks, "run_ssh_readonly", return_value=same):
            disks.check_drift(TARGET, [expected])
        swapped = LSBLK.replace("QM00001", "QM99999")
        with patch.object(disks, "run_ssh_readonly", return_value={**same, "stdout": swapped}):
            with self.assertRaises(AgentError) as caught:
                disks.check_drift(TARGET, [expected])
        self.assertEqual(caught.exception.code, TARGET_DRIFT)
        self.assertEqual(caught.exception.details["actual"]["serial"], "QM99999")

    def test_unreachable_target_is_reported(self):
        failed = {"label": "disk_identity", "returncode": 255, "stdout": "", "stderr": "ssh: connect to host"}
        with patch.object(disks, "run_ssh_readonly", return_value=failed):
            with self.assertRaises(AgentError) as caught:
                disks.read_live_identity(TARGET, "/dev/sda")
        self.assertEqual(caught.exception.code, SSH_UNREACHABLE)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_disks -v`
Expected: ERROR, `ImportError: cannot import name 'INVENTORY_MISSING'`.

- [ ] **Step 3: Add the error codes**

Append to the constants in `gentoo_agent/core/errors.py`:

```python
INVENTORY_MISSING = "INVENTORY_MISSING"
TARGET_DRIFT = "TARGET_DRIFT"
SSH_UNREACHABLE = "SSH_UNREACHABLE"
```

- [ ] **Step 4: Add the read-only command**

In `gentoo_agent/core/ssh.py`, add this entry to `READ_ONLY_COMMANDS` after `"storage_json"`:

```python
    "disk_identity": "lsblk -J -b -d -e7 -o PATH,TYPE,SIZE,MODEL,SERIAL,WWN",
```

- [ ] **Step 5: Create `gentoo_agent/core/disks.py`**

```python
"""Disk identity: what a plan binds to, and what is re-checked before any change."""

from __future__ import annotations

import json
import subprocess

from gentoo_agent.core.errors import (
    CONFIG_INVALID,
    INVENTORY_MISSING,
    SSH_UNREACHABLE,
    TARGET_DRIFT,
    AgentError,
)
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS, run_ssh_readonly


IDENTITY_LABEL = "disk_identity"
IDENTITY_FIELDS = ("path", "size_bytes", "model", "serial", "wwn")


def _text(value: object) -> str:
    return str(value or "").strip()


def parse_disk_identities(stdout: str) -> list[dict]:
    try:
        devices = json.loads(stdout or "{}").get("blockdevices", [])
    except json.JSONDecodeError as exc:
        raise AgentError(INVENTORY_MISSING, "disk_identity output is not valid lsblk JSON", {"label": IDENTITY_LABEL}) from exc
    return [
        {
            "path": _text(device.get("path")),
            "size_bytes": int(device.get("size") or 0),
            "model": _text(device.get("model")),
            "serial": _text(device.get("serial")),
            "wwn": _text(device.get("wwn")),
        }
        for device in devices
        if device.get("type") == "disk"
    ]


def identity_strength(disk: dict) -> str:
    return "strong" if disk.get("serial") or disk.get("wwn") else "weak"


def disk_from_inventory(inventory: dict | None, path: str) -> dict:
    results = {item.get("label"): item for item in (inventory or {}).get("results", [])}
    item = results.get(IDENTITY_LABEL)
    if item is None or item.get("returncode") != 0:
        raise AgentError(INVENTORY_MISSING, "The latest inventory has no disk identity; run inspect again.", {"label": IDENTITY_LABEL})
    for disk in parse_disk_identities(item.get("stdout", "")):
        if disk["path"] == path:
            return disk
    raise AgentError(CONFIG_INVALID, f"Configured disk {path} is not in the latest inventory.", {"disk": path})


def read_live_identity(target: dict, path: str) -> dict:
    try:
        result = run_ssh_readonly(target, IDENTITY_LABEL, READ_ONLY_COMMANDS[IDENTITY_LABEL])
    except subprocess.TimeoutExpired as exc:
        raise AgentError(SSH_UNREACHABLE, f"Timed out reading disk identity from {target['host']}", {"host": target["host"]}) from exc
    if result["returncode"] != 0:
        raise AgentError(
            SSH_UNREACHABLE,
            f"Could not read disk identity from {target['host']}: {result['stderr'].strip()}",
            {"host": target["host"], "returncode": result["returncode"]},
        )
    for disk in parse_disk_identities(result["stdout"]):
        if disk["path"] == path:
            return disk
    raise AgentError(TARGET_DRIFT, f"Disk {path} is no longer present on the target.", {"disk": path})


def same_disk(expected: dict, actual: dict) -> bool:
    return all(expected.get(field) == actual.get(field) for field in IDENTITY_FIELDS)


def check_drift(target: dict, expected_disks: list[dict]) -> None:
    for expected in expected_disks:
        actual = read_live_identity(target, expected["path"])
        if not same_disk(expected, actual):
            raise AgentError(
                TARGET_DRIFT,
                f"Disk {expected['path']} does not match the approved plan.",
                {"expected": expected, "actual": actual},
            )
```

- [ ] **Step 6: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 57 tests PASS. `test_collect_inventory_runs_every_allowlisted_command_and_saves` still passes because it counts `READ_ONLY_COMMANDS` dynamically.

- [ ] **Step 7: Commit**

```bash
git add gentoo_agent/core tests/test_disks.py
git commit -m "Record disk serial and WWN and detect target drift"
```

---

### Task 6: Install plans with `plan_hash`

**Files:**
- Modify: `gentoo_agent/core/errors.py`
- Create: `gentoo_agent/core/install_plan.py`, `tests/test_install_plan.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_install_plan.py`:

```python
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import install_plan, paths, phases
from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.errors import (
    CONFIG_INVALID,
    INVENTORY_MISSING,
    PLAN_HASH_MISMATCH,
    PLAN_NOT_FOUND,
    AgentError,
)
from tests.helpers import temp_home


REPO = Path(__file__).resolve().parents[1]
PUBLIC_KEY = "ssh-ed25519 AAAAPLAN gentoo-agent-plan-test"
TARGET = {
    "name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
    "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True,
}


def install_fixture() -> dict:
    data = json.loads((REPO / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))
    data.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True})
    return data


def project_plan() -> dict:
    return json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8"))


def inventory(serial: str = "QM00001") -> dict:
    stdout = json.dumps({"blockdevices": [
        {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK", "serial": serial, "wwn": None},
    ]})
    return {"results": [{"label": "disk_identity", "returncode": 0, "stdout": stdout}]}


class CreatePlanTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.object(phases, "public_key_for_target", return_value=PUBLIC_KEY)
        self.key.start()

    def tearDown(self):
        self.key.stop()

    def create(self, **changes):
        install = {**install_fixture(), **changes.pop("install", {})}
        return install_plan.create_install_plan(TARGET, install, project_plan(), changes.pop("inventory", inventory()))

    def test_plan_is_deterministic_and_id_is_hash_prefix(self):
        first, second = self.create(), self.create()
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(first["plan_id"], first["plan_hash"][:16])
        self.assertEqual(len(first["plan_hash"]), 64)

    def test_hash_changes_with_disk_serial(self):
        self.assertNotEqual(self.create()["plan_hash"], self.create(inventory=inventory("QM00002"))["plan_hash"])

    def test_hash_changes_with_schema_version(self):
        before = self.create()["plan_hash"]
        with patch.object(install_plan, "PLAN_SCHEMA_VERSION", 999):
            self.assertNotEqual(self.create()["plan_hash"], before)

    def test_plan_pins_generated_commands_and_destructive_flags(self):
        plan = self.create()
        self.assertEqual([phase["id"] for phase in plan["phases"]], phases.PHASES)
        storage = next(phase for phase in plan["phases"] if phase["id"] == "storage")
        self.assertEqual(storage["apply"], phases.command_for("storage", TARGET, install_fixture()))
        self.assertEqual(storage["check"], phases.check_command_for("storage", TARGET, install_fixture()))
        self.assertTrue(storage["destructive"])
        preflight = next(phase for phase in plan["phases"] if phase["id"] == "preflight")
        self.assertFalse(preflight["destructive"])
        self.assertEqual(plan["disks"][0]["serial"], "QM00001")

    def test_missing_inventory_is_reported(self):
        with self.assertRaises(AgentError) as caught:
            install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), None)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_plan_requires_full_erase_authorization(self):
        with self.assertRaises(AgentError) as caught:
            self.create(install={"disk_must_be_erased": False})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        with self.assertRaises(AgentError):
            install_plan.create_install_plan({**TARGET, "disk_erase_authorized": False}, install_fixture(), project_plan(), inventory())


class PlanStorageTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.object(phases, "public_key_for_target", return_value=PUBLIC_KEY)
        self.key.start()

    def tearDown(self):
        self.key.stop()

    def test_save_and_load_round_trip_is_idempotent(self):
        with temp_home():
            plan = install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), inventory())
            saved = install_plan.save_install_plan(plan)
            again = install_plan.save_install_plan(
                install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), inventory())
            )
            self.assertEqual(again["created_at"], saved["created_at"])
            self.assertEqual(install_plan.load_install_plan(plan["plan_id"]), saved)
            with self.assertRaises(AgentError) as caught:
                install_plan.load_install_plan("0" * 16)
            self.assertEqual(caught.exception.code, PLAN_NOT_FOUND)

    def test_modified_plan_file_is_rejected(self):
        with temp_home():
            plan = install_plan.save_install_plan(
                install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), inventory())
            )
            path = paths.plans_dir() / f"{plan['plan_id']}.json"
            tampered = load_json(path)
            tampered["phases"][1]["apply"] = "reboot"
            save_json(path, tampered)
            with self.assertRaises(AgentError) as caught:
                install_plan.load_install_plan(plan["plan_id"])
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_invalid_plan_id_is_rejected_before_touching_files(self):
        with self.assertRaises(AgentError) as caught:
            install_plan.load_install_plan("../../etc/passwd")
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_install_plan -v`
Expected: ERROR, `ImportError: cannot import name 'PLAN_HASH_MISMATCH'`.

- [ ] **Step 3: Add the error codes**

Append to `gentoo_agent/core/errors.py`:

```python
PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
PLAN_HASH_MISMATCH = "PLAN_HASH_MISMATCH"
```

- [ ] **Step 4: Create `gentoo_agent/core/install_plan.py`**

```python
"""Install plans: the frozen, hashed description of exactly what will run.

A plan holds the validated configuration, the SSH target, the target disk
identity, and every generated phase command. plan_hash covers all of it, so an
approval of the hash is an approval of these exact commands on this exact disk.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from gentoo_agent.core import paths
from gentoo_agent.core.config import load_json, now, save_json
from gentoo_agent.core.disks import disk_from_inventory
from gentoo_agent.core.errors import (
    CONFIG_INVALID,
    INVENTORY_MISSING,
    PLAN_HASH_MISMATCH,
    PLAN_NOT_FOUND,
    AgentError,
)
from gentoo_agent.core.phases import PHASES, check_command_for, command_for
from gentoo_agent.core.validate import validated_settings


PLAN_SCHEMA_VERSION = 1
PLAN_ID_LENGTH = 16
PLAN_ID_PATTERN = re.compile(r"[0-9a-f]{16}")
UNHASHED_FIELDS = {"plan_id", "plan_hash", "created_at"}
TARGET_FIELDS = ("name", "host", "port", "user", "identity_file", "known_hosts_file")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_plan_hash(plan: dict) -> str:
    body = {key: value for key, value in plan.items() if key not in UNHASHED_FIELDS}
    return hashlib.sha256(canonical(body)).hexdigest()


def _check_erase_authorized(target: dict, install: dict) -> None:
    flags = {
        "install.disk_must_be_erased": install.get("disk_must_be_erased"),
        "install.target_is_disposable": install.get("target_is_disposable"),
        "install.disk_erase_authorized": install.get("disk_erase_authorized"),
        "target.target_is_disposable": target.get("target_is_disposable"),
        "target.disk_erase_authorized": target.get("disk_erase_authorized"),
    }
    missing = [name for name, value in flags.items() if value is not True]
    if missing:
        raise AgentError(
            CONFIG_INVALID,
            "The storage phase always erases the target disk; the configuration does not fully authorize that.",
            {"missing": missing},
        )


def create_install_plan(target: dict, install: dict, project_plan: dict, inventory: dict | None) -> dict:
    validated_settings(target, install)
    if inventory is None:
        raise AgentError(INVENTORY_MISSING, "No inventory exists; run inspect first.", {})
    _check_erase_authorized(target, install)
    disk = disk_from_inventory(inventory, install["disk"])
    destructive = {item["id"]: bool(item.get("destructive", True)) for item in project_plan.get("phases", [])}
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "target": {field: target.get(field) for field in TARGET_FIELDS},
        "install": install,
        "disks": [disk],
        "inventory_hash": hashlib.sha256(canonical(inventory)).hexdigest(),
        "phases": [
            {
                "id": phase,
                "destructive": destructive.get(phase, True),
                "apply": command_for(phase, target, install),
                "check": check_command_for(phase, target, install),
            }
            for phase in PHASES
        ],
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    plan["plan_id"] = plan["plan_hash"][:PLAN_ID_LENGTH]
    plan["created_at"] = now()
    return plan


def plan_path(plan_id: str) -> Path:
    if not PLAN_ID_PATTERN.fullmatch(str(plan_id or "")):
        raise AgentError(CONFIG_INVALID, f"Invalid plan id: {plan_id!r}", {"plan_id": plan_id})
    return paths.plans_dir() / f"{plan_id}.json"


def load_install_plan(plan_id: str) -> dict:
    path = plan_path(plan_id)
    if not path.exists():
        raise AgentError(PLAN_NOT_FOUND, f"Plan {plan_id} does not exist.", {"plan_id": plan_id})
    plan = load_json(path)
    if plan.get("plan_id") != plan_id or plan.get("plan_hash") != compute_plan_hash(plan):
        raise AgentError(PLAN_HASH_MISMATCH, f"Plan file {path} was modified after it was created.", {"plan_id": plan_id})
    return plan


def save_install_plan(plan: dict) -> dict:
    path = plan_path(plan["plan_id"])
    if path.exists():
        existing = load_install_plan(plan["plan_id"])
        if existing["plan_hash"] != plan["plan_hash"]:
            raise AgentError(PLAN_HASH_MISMATCH, "A different plan already uses this plan id.", {"plan_id": plan["plan_id"]})
        return existing
    save_json(path, plan)
    return plan
```

- [ ] **Step 5: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 66 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add gentoo_agent/core tests/test_install_plan.py
git commit -m "Add install plans bound by plan_hash"
```

---

### Task 7: Approval records

**Files:**
- Modify: `gentoo_agent/core/errors.py`
- Create: `gentoo_agent/core/approval.py`, `tests/test_approval.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_approval.py`:

```python
import datetime as dt
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import approval, install_plan, phases
from gentoo_agent.core.config import save_json
from gentoo_agent.core.errors import (
    APPROVAL_EXPIRED,
    APPROVAL_REQUIRED,
    CONFIG_INVALID,
    PLAN_HASH_MISMATCH,
    AgentError,
)
from tests.helpers import temp_home


REPO = Path(__file__).resolve().parents[1]
TARGET = {
    "name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
    "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True,
}
START = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)


def make_plan(serial: str = "QM00001") -> dict:
    install = json.loads((REPO / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))
    install.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True})
    project = json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8"))
    stdout = json.dumps({"blockdevices": [
        {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK", "serial": serial, "wwn": None},
    ]})
    inventory = {"results": [{"label": "disk_identity", "returncode": 0, "stdout": stdout}]}
    with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAA test"):
        return install_plan.save_install_plan(install_plan.create_install_plan(TARGET, install, project, inventory))


class ApprovalTests(unittest.TestCase):
    def test_summary_is_built_from_the_plan(self):
        with temp_home():
            plan = make_plan()
            summary = approval.approval_summary(plan)
            weak = approval.approval_summary(make_plan(serial=""))
        self.assertIn(plan["plan_hash"], summary)
        self.assertIn("serial=QM00001", summary)
        self.assertIn("root@192.0.2.10:22", summary)
        self.assertIn("Destructive phases: storage, filesystems, base-system", summary)
        self.assertIn("all data on the disk will be destroyed", summary)
        self.assertNotIn("WARNING", summary)
        self.assertIn("WARNING: this disk has no serial number or WWN", weak)

    def test_unapproved_plan_is_refused(self):
        with temp_home():
            plan = make_plan()
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)
        self.assertIn(f"gentoo-agent approve {plan['plan_id']}", caught.exception.message)

    def test_recorded_approval_allows_the_plan_until_expiry(self):
        with temp_home():
            plan = make_plan()
            record = approval.record_approval(plan, "cli", at=START)
            self.assertEqual(record["expires_at"], "2026-10-15T13:00:00+00:00")
            loaded = approval.require_approved_plan(plan["plan_id"], at=START + dt.timedelta(minutes=59))
            self.assertEqual(loaded["plan_hash"], plan["plan_hash"])
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START + dt.timedelta(minutes=60))
        self.assertEqual(caught.exception.code, APPROVAL_EXPIRED)

    def test_approval_for_another_hash_is_rejected(self):
        with temp_home():
            plan = make_plan()
            record = approval.record_approval(plan, "cli", at=START)
            save_json(approval.approval_path(plan["plan_id"]), {**record, "plan_hash": "0" * 64})
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_unknown_channel_is_invalid(self):
        with temp_home():
            plan = make_plan()
            with self.assertRaises(AgentError) as caught:
                approval.record_approval(plan, "model", at=START)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_phrase_names_the_plan(self):
        self.assertEqual(approval.approval_phrase({"plan_id": "abc"}), "APPROVE abc")

    def test_status_reports_both_states(self):
        with temp_home():
            plan = make_plan()
            self.assertEqual(approval.approval_status(plan["plan_id"], at=START)["code"], APPROVAL_REQUIRED)
            approval.record_approval(plan, "elicitation", at=START)
            status = approval.approval_status(plan["plan_id"], at=START)
        self.assertEqual(status, {"approved": True, "channel": "elicitation", "expires_at": "2026-10-15T13:00:00+00:00"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_approval -v`
Expected: ERROR, `ImportError: cannot import name 'APPROVAL_EXPIRED'`.

- [ ] **Step 3: Add the error codes**

Append to `gentoo_agent/core/errors.py`:

```python
APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
APPROVAL_DECLINED = "APPROVAL_DECLINED"
```

- [ ] **Step 4: Create `gentoo_agent/core/approval.py`**

```python
"""Human approval of a stored plan, bound to its plan_hash.

Only a human writes approval records: the TTY-only CLI command now, and MCP
elicitation in plan 3. No function here prompts; front ends do that and then
call record_approval(). require_approved_plan() is the single execution gate.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from gentoo_agent.core import paths
from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.disks import identity_strength
from gentoo_agent.core.errors import (
    APPROVAL_EXPIRED,
    APPROVAL_REQUIRED,
    CONFIG_INVALID,
    PLAN_HASH_MISMATCH,
    AgentError,
)
from gentoo_agent.core.install_plan import load_install_plan, plan_path


APPROVAL_TTL_MINUTES = 60
CHANNELS = {"cli", "elicitation"}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def approval_phrase(plan: dict) -> str:
    return f"APPROVE {plan['plan_id']}"


def approval_summary(plan: dict) -> str:
    target = plan["target"]
    lines = [
        f"Plan: {plan['plan_id']}",
        f"Plan hash: {plan['plan_hash']}",
        f"Target: {target['user']}@{target['host']}:{target.get('port') or 22} ({target['name']})",
    ]
    for disk in plan["disks"]:
        lines.append(
            f"Disk: {disk['path']}  size={disk['size_bytes']} bytes  model={disk['model'] or 'unknown'}  "
            f"serial={disk['serial'] or 'none'}  wwn={disk['wwn'] or 'none'}"
        )
        if identity_strength(disk) == "weak":
            lines.append("WARNING: this disk has no serial number or WWN; drift detection relies on path, size, and model only.")
    lines.append("Disk erase: YES, all data on the disk will be destroyed")
    lines.append("Destructive phases: " + ", ".join(phase["id"] for phase in plan["phases"] if phase["destructive"]))
    return "\n".join(lines)


def approval_path(plan_id: str) -> Path:
    plan_path(plan_id)  # validates the id format
    return paths.approval_dir() / f"{plan_id}.json"


def record_approval(plan: dict, channel: str, *, at: dt.datetime | None = None) -> dict:
    if channel not in CHANNELS:
        raise AgentError(CONFIG_INVALID, f"Unknown approval channel: {channel!r}", {"channel": channel})
    approved_at = at or _utcnow()
    record = {
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "channel": channel,
        "approved_at": approved_at.isoformat(),
        "expires_at": (approved_at + dt.timedelta(minutes=APPROVAL_TTL_MINUTES)).isoformat(),
    }
    save_json(approval_path(plan["plan_id"]), record)
    return record


def require_approved_plan(plan_id: str, *, at: dt.datetime | None = None) -> dict:
    plan = load_install_plan(plan_id)
    path = approval_path(plan_id)
    if not path.exists():
        raise AgentError(
            APPROVAL_REQUIRED,
            f"Plan {plan_id} is not approved. A human must run: gentoo-agent approve {plan_id}",
            {"plan_id": plan_id, "command": f"gentoo-agent approve {plan_id}"},
        )
    record = load_json(path)
    if record.get("plan_hash") != plan["plan_hash"]:
        raise AgentError(PLAN_HASH_MISMATCH, f"The approval for plan {plan_id} is for a different plan hash.", {"plan_id": plan_id})
    if dt.datetime.fromisoformat(record["expires_at"]) <= (at or _utcnow()):
        raise AgentError(
            APPROVAL_EXPIRED,
            f"The approval for plan {plan_id} expired at {record['expires_at']}; approve it again.",
            {"plan_id": plan_id, "expires_at": record["expires_at"]},
        )
    return plan


def approval_status(plan_id: str, *, at: dt.datetime | None = None) -> dict:
    try:
        require_approved_plan(plan_id, at=at)
    except AgentError as error:
        return {"approved": False, "code": error.code, "message": error.message}
    record = load_json(approval_path(plan_id))
    return {"approved": True, "channel": record["channel"], "expires_at": record["expires_at"]}
```

- [ ] **Step 5: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 73 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add gentoo_agent/core tests/test_approval.py
git commit -m "Add plan_hash-bound approval records"
```

---

### Task 8: Run records

**Files:**
- Modify: `gentoo_agent/core/errors.py`
- Create: `gentoo_agent/core/runs.py`, `tests/test_runs.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_runs.py`:

```python
import datetime as dt
import subprocess
import sys
import unittest

from gentoo_agent.core import runs
from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.errors import CONFIG_INVALID, RUN_ALREADY_ACTIVE, RUN_NOT_FOUND, AgentError
from tests.helpers import temp_home


def dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def record(run_id: str, **changes) -> dict:
    return {"run_id": run_id, "plan_id": "a" * 16, "status": "running", "pid": None,
            "started_at": runs.now(), **changes}


class RunRecordTests(unittest.TestCase):
    def test_run_ids_are_validated(self):
        run_id = runs.new_run_id()
        self.assertRegex(run_id, r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
        with self.assertRaises(AgentError) as caught:
            runs.run_dir("../x")
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_write_read_update_and_missing(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id))
            runs.update_run(run_id, status="failed")
            self.assertEqual(runs.read_run(run_id)["status"], "failed")
            with self.assertRaises(AgentError) as caught:
                runs.read_run(runs.new_run_id())
        self.assertEqual(caught.exception.code, RUN_NOT_FOUND)


class LockTests(unittest.TestCase):
    def test_lock_is_exclusive_while_the_holder_is_alive(self):
        import os

        with temp_home():
            first = runs.new_run_id()
            runs.write_run(first, record(first, pid=os.getpid()))
            runs.acquire_lock(first)
            with self.assertRaises(AgentError) as caught:
                runs.acquire_lock(runs.new_run_id())
            self.assertEqual(caught.exception.code, RUN_ALREADY_ACTIVE)
            runs.release_lock(first)
            runs.acquire_lock(runs.new_run_id())

    def test_stale_lock_is_reclaimed(self):
        with temp_home():
            stale = runs.new_run_id()
            runs.write_run(stale, record(stale, pid=dead_pid()))
            runs.acquire_lock(stale)
            fresh = runs.new_run_id()
            runs.acquire_lock(fresh)
            self.assertEqual(runs.lock_path().read_text(encoding="utf-8"), fresh)


class StatusTests(unittest.TestCase):
    def test_cancel_flag(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id))
            self.assertFalse(runs.cancel_requested(run_id))
            runs.request_cancel(run_id)
            self.assertTrue(runs.cancel_requested(run_id))

    def test_dead_worker_is_reported_as_interrupted(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id, pid=dead_pid()))
            runs.acquire_lock(run_id)
            status = runs.run_status(run_id)
            self.assertFalse(runs.lock_path().exists())
        self.assertEqual(status["status"], "interrupted")


class CredentialsAndLogTests(unittest.TestCase):
    def test_credentials_are_private_and_returned_once(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id, status="failed"))
            runs.write_credentials(run_id, {"credentials": {"root": "secret"}, "applied": True})
            path = runs.run_dir(run_id) / "credentials.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(runs.take_credentials(run_id)["credentials"], {"root": "secret"})
            self.assertIsNone(runs.take_credentials(run_id))

    def test_stale_credentials_are_purged(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id, status="failed"))
            runs.write_credentials(run_id, {"credentials": {"root": "secret"}, "applied": False})
            path = runs.run_dir(run_id) / "credentials.json"
            data = load_json(path)
            data["written_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).isoformat()
            save_json(path, data, mode=0o600)
            runs.purge_stale_credentials()
            self.assertFalse(path.exists())

    def test_log_is_read_in_pages(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, record(run_id))
            for index in range(5):
                runs.append_log(run_id, {"index": index})
            page = runs.read_log(run_id, offset=1, limit=2)
            with self.assertRaises(AgentError):
                runs.read_log(run_id, offset=-1, limit=2)
        self.assertEqual([entry["index"] for entry in page["entries"]], [1, 2])
        self.assertEqual(page["next_offset"], 3)
        self.assertEqual(page["total"], 5)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_runs -v`
Expected: ERROR, `ImportError: cannot import name 'RUN_ALREADY_ACTIVE'`.

- [ ] **Step 3: Add the error codes**

Append to `gentoo_agent/core/errors.py`:

```python
RUN_NOT_FOUND = "RUN_NOT_FOUND"
RUN_ALREADY_ACTIVE = "RUN_ALREADY_ACTIVE"
```

- [ ] **Step 4: Create `gentoo_agent/core/runs.py`**

```python
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
```

`tests/test_runs.py` uses `runs.now`, which this module imports from `config`.

- [ ] **Step 5: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 82 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add gentoo_agent/core tests/test_runs.py
git commit -m "Add run records, single-run lock, cancel flag, and one-time credentials"
```

---

### Task 9: `execute_plan`

The executor runs the commands stored in the plan, never regenerated ones. It checks disk drift first, honors cancel between phases, records every outcome (including exceptions and interrupts) in `run.json`, and returns an output tail on failure.

**Files:**
- Modify: `gentoo_agent/core/runner.py`
- Create: `tests/test_execute_plan.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_execute_plan.py`:

```python
import unittest
from unittest.mock import patch

from gentoo_agent.core import runner, runs
from gentoo_agent.core.errors import TARGET_DRIFT, AgentError
from tests.helpers import temp_home


TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root"}
PLAN = {
    "plan_id": "a" * 16,
    "plan_hash": "a" * 64,
    "target": TARGET,
    "disks": [{"path": "/dev/sda", "size_bytes": 1, "model": "m", "serial": "s", "wwn": ""}],
    "phases": [
        {"id": "preflight", "destructive": False, "apply": "apply-preflight", "check": "check-preflight"},
        {"id": "authentication", "destructive": True, "apply": "apply-auth", "check": "check-auth"},
        {"id": "kernel", "destructive": True, "apply": "apply-kernel", "check": "check-kernel"},
    ],
}
RECORDS = "root:pw-root\ngentoo:pw-user\n"


class FakeShell:
    def __init__(self, returncodes=None, raise_on=None):
        self.returncodes = returncodes or {}
        self.raise_on = raise_on or {}
        self.calls = []

    def __call__(self, target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
        self.calls.append((label, command, input_data))
        if label in self.raise_on:
            raise self.raise_on[label]
        stdout = "".join(f"line {number}\n" for number in range(60))
        return {"label": label, "command": command, "returncode": self.returncodes.get(label, 0),
                "stdout": stdout, "stderr": "", "finished_at": "now"}


class ExecutePlanTests(unittest.TestCase):
    def run_plan(self, shell, drift=None):
        run_id = runs.new_run_id()
        runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "starting",
                                "pid": None, "started_at": runs.now()})
        with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift", side_effect=drift):
            result = runner.execute_plan(PLAN, run_id, RECORDS)
        return run_id, result, runs.read_run(run_id)

    def test_completed_checks_skip_apply(self):
        with temp_home():
            shell = FakeShell()
            _, result, record = self.run_plan(shell)
        self.assertEqual(result["status"], "installed_pending_reboot")
        self.assertEqual([call[0] for call in shell.calls], ["preflight-check", "authentication-check", "kernel-check"])
        self.assertEqual(record["status"], "installed_pending_reboot")
        self.assertEqual(record["completed_phases"], ["preflight", "authentication", "kernel"])

    def test_failed_apply_stops_with_output_tail(self):
        with temp_home():
            shell = FakeShell({"kernel-check": 1, "kernel": 4})
            _, result, record = self.run_plan(shell)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_phase"], "kernel")
        self.assertEqual(result["error"]["code"], "PHASE_FAILED")
        self.assertEqual(len(result["output_tail"].splitlines()), 40)
        self.assertTrue(result["output_tail"].endswith("line 59"))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["last_error"], "kernel apply returned 4")

    def test_failed_verify_stops(self):
        with temp_home():
            _, result, record = self.run_plan(FakeShell({"kernel-check": 1, "kernel-verify": 2}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(record["last_error"], "kernel verification returned 2")

    def test_drift_stops_before_any_phase(self):
        drift = AgentError(TARGET_DRIFT, "Disk /dev/sda does not match the approved plan.")
        with temp_home():
            shell = FakeShell()
            _, result, record = self.run_plan(shell, drift=drift)
        self.assertEqual(shell.calls, [])
        self.assertEqual(result["error"]["code"], TARGET_DRIFT)
        self.assertEqual(record["status"], "failed")

    def test_cancel_stops_at_the_next_phase_boundary(self):
        with temp_home():
            shell = FakeShell({"preflight-check": 1})
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "status": "starting", "pid": None, "started_at": runs.now()})
            original = shell.__call__

            def cancel_after_preflight_verify(*args, **kwargs):
                result = original(*args, **kwargs)
                if args[1] == "preflight-verify":
                    runs.request_cancel(run_id)
                return result

            with patch.object(runner, "remote_shell", cancel_after_preflight_verify), patch.object(runner, "check_drift"):
                result = runner.execute_plan(PLAN, run_id, RECORDS)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["completed_phases"], ["preflight"])

    def test_unexpected_exception_is_recorded(self):
        with temp_home():
            _, result, record = self.run_plan(FakeShell(raise_on={"authentication-check": OSError("ssh vanished")}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_phase"], "authentication")
        self.assertIn("ssh vanished", record["last_error"])

    def test_interrupt_is_recorded_and_reraised(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "status": "starting", "pid": None, "started_at": runs.now()})
            shell = FakeShell(raise_on={"kernel-check": KeyboardInterrupt()})
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift"):
                with self.assertRaises(KeyboardInterrupt):
                    runner.execute_plan(PLAN, run_id, RECORDS)
            self.assertEqual(runs.read_run(run_id)["status"], "interrupted")

    def test_passwords_reach_only_the_authentication_apply_and_never_the_log(self):
        with temp_home():
            shell = FakeShell({"authentication-check": 1})
            run_id, result, _ = self.run_plan(shell)
            log_text = (runs.run_dir(run_id) / "log.jsonl").read_text(encoding="utf-8")
        inputs = {label: data for label, _, data in shell.calls}
        self.assertEqual(inputs["authentication"], RECORDS)
        self.assertTrue(all(data is None for label, data in inputs.items() if label != "authentication"))
        self.assertTrue(result["authentication_applied"])
        self.assertNotIn("pw-root", log_text)
        self.assertNotIn("pw-user", log_text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_execute_plan -v`
Expected: ERROR or FAIL on every test with `AttributeError: ... has no attribute 'remote_shell'` / `'execute_plan'`.

- [ ] **Step 3: Add `execute_plan` to `gentoo_agent/core/runner.py`**

Replace the module docstring and imports with:

```python
"""Phase selection, dry-run rendering, and execution of an approved plan.

execute_plan runs the commands stored in the plan (never regenerated ones),
after confirming the live disk still matches the plan. The target is never
rebooted. Every outcome, including exceptions and interrupts, is recorded in
the run record.
"""

from __future__ import annotations

import os
from typing import Callable

from gentoo_agent.core.config import now
from gentoo_agent.core.disks import check_drift
from gentoo_agent.core.errors import CONFIG_INVALID, PHASE_FAILED, AgentError
from gentoo_agent.core.phases import PHASES, command_for
from gentoo_agent.core.runs import append_log, cancel_requested, update_run
from gentoo_agent.core.ssh import remote_shell


EventSink = Callable[[str], None]
OUTPUT_TAIL_LINES = 40
```

Keep `select_phases` and `render_dry_run` unchanged and append:

```python
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

    update_run(run_id, status="running", pid=os.getpid(), phase_count=len(phases), phase_index=0)
    try:
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
                authentication_applied = True
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
        current = None
        return finish("installed_pending_reboot")
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
```

In `test_drift_stops_before_any_phase`, `current` is `None` when drift is detected, so `failed_phase` is `None`; the test does not assert it.

- [ ] **Step 4: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 90 tests PASS. `tests/test_core_purity.py` still passes (`runner` uses `os`, not `sys`).

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/runner.py tests/test_execute_plan.py
git commit -m "Execute approved plans with drift check, cancel, and recorded outcomes"
```

---

### Task 10: Launcher and worker

`jobs/launcher.start_run()` is the only code path that starts work, so both the CLI and the MCP server pass through `require_approved_plan()`. It resolves passwords before any phase runs, then spawns a detached worker and hands it the password records over a pipe.

**Files:**
- Modify: `gentoo_agent/core/credentials.py`, `tests/test_credentials.py`
- Create: `gentoo_agent/jobs/__init__.py`, `gentoo_agent/jobs/launcher.py`, `gentoo_agent/jobs/worker.py`
- Create: `tests/test_launcher.py`, `tests/test_worker.py`

- [ ] **Step 1: Write the failing credentials test** (append to `PasswordInputTests`)

```python
    def test_empty_environment_password_is_generated(self):
        install = {**BASE, "password_source": "environment", "generate_missing_passwords": True}
        with patch.dict(
            credentials.os.environ,
            {"TEST_GENTOO_ROOT_PASSWORD": "", "TEST_GENTOO_USER_PASSWORD": "user-secret"},
            clear=True,
        ), patch("gentoo_agent.core.credentials.secrets.token_urlsafe", return_value="generated-root"):
            records, generated = credentials.password_input(install)
        self.assertEqual(records, "root:generated-root\ngentoo:user-secret\n")
        self.assertEqual(generated, {"root": "generated-root"})
```

Run: `python -m unittest tests.test_credentials -v`
Expected: the new test ERRORS with `AgentError: Password from TEST_GENTOO_ROOT_PASSWORD is empty or contains a newline`.

- [ ] **Step 2: Treat an empty variable as missing**

In `gentoo_agent/core/credentials.py`, inside `obtain`, change `if value is None:` to `if not value:`.

Run: `python -m unittest tests.test_credentials -v`
Expected: 7 tests PASS. (`test_missing_environment_password_without_generation_is_invalid` still raises for a missing variable.)

- [ ] **Step 3: Write the failing launcher tests**

`tests/test_launcher.py`:

```python
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import approval, install_plan, phases, runs
from gentoo_agent.core.errors import APPROVAL_REQUIRED, RUN_ALREADY_ACTIVE, AgentError
from gentoo_agent.jobs import launcher
from tests.helpers import temp_home


REPO = Path(__file__).resolve().parents[1]


def approved_plan(host: str = "192.0.2.10") -> dict:
    target = {"name": "gentoo", "host": host, "port": 22, "user": "root",
              "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True}
    install = json.loads((REPO / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))
    install.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True,
                    "password_source": "generate"})
    project = json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8"))
    stdout = json.dumps({"blockdevices": [
        {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK", "serial": "QM00001", "wwn": None},
    ]})
    inventory = {"results": [{"label": "disk_identity", "returncode": 0, "stdout": stdout}]}
    with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAA test"):
        plan = install_plan.save_install_plan(install_plan.create_install_plan(target, install, project, inventory))
    approval.record_approval(plan, "cli")
    return plan


class FakeProcess:
    pid = 424242

    def __init__(self):
        self.stdin = self
        self.written = b""
        self.closed = False

    def write(self, data):
        self.written += data

    def close(self):
        self.closed = True


class LauncherTests(unittest.TestCase):
    def test_unapproved_plan_never_spawns_a_worker(self):
        with temp_home():
            plan = approved_plan()
            approval.approval_path(plan["plan_id"]).unlink()
            with patch.object(launcher.subprocess, "Popen") as popen:
                with self.assertRaises(AgentError) as caught:
                    launcher.start_run(plan["plan_id"])
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)
        popen.assert_not_called()

    def test_start_resolves_passwords_first_and_hands_them_over_stdin(self):
        process = FakeProcess()
        with temp_home() as home, patch.object(launcher.subprocess, "Popen", return_value=process) as popen:
            plan = approved_plan()
            run_id = launcher.start_run(plan["plan_id"])
            record = runs.read_run(run_id)
            command = popen.call_args.args[0]
            kwargs = popen.call_args.kwargs
        self.assertEqual(command[1:], ["-m", "gentoo_agent.jobs.worker", run_id])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["GENTOO_AGENT_HOME"], str(home))
        self.assertTrue(kwargs["env"]["PYTHONPATH"].startswith(str(REPO)))
        payload = json.loads(process.written)
        self.assertEqual(set(payload["generated_credentials"]), {"root", "gentoo"})
        self.assertTrue(payload["password_records"].startswith("root:"))
        self.assertTrue(process.closed)
        self.assertEqual(record["status"], "starting")
        self.assertEqual(record["pid"], 424242)
        self.assertEqual(record["plan_hash"], plan["plan_hash"])
        self.assertNotIn("password", json.dumps(record))

    def test_second_start_while_active_is_refused(self):
        with temp_home(), patch.object(launcher.subprocess, "Popen", return_value=FakeProcess()):
            plan = approved_plan()
            first = launcher.start_run(plan["plan_id"])
            runs.update_run(first, pid=os.getpid())
            with self.assertRaises(AgentError) as caught:
                launcher.start_run(plan["plan_id"])
        self.assertEqual(caught.exception.code, RUN_ALREADY_ACTIVE)

    def test_resume_starts_a_new_run_for_the_same_plan(self):
        with temp_home(), patch.object(launcher.subprocess, "Popen", return_value=FakeProcess()):
            plan = approved_plan()
            first = launcher.start_run(plan["plan_id"])
            runs.update_run(first, status="failed")
            runs.release_lock(first)
            second = launcher.resume_run(first)
            record = runs.read_run(second)
        self.assertNotEqual(first, second)
        self.assertEqual(record["resumed_from"], first)
        self.assertEqual(record["plan_id"], plan["plan_id"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run them to verify they fail**

Run: `python -m unittest tests.test_launcher -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'gentoo_agent.jobs'`.

- [ ] **Step 5: Create the launcher**

`gentoo_agent/jobs/__init__.py`:

```python
"""Background execution: the launcher (start, resume, cancel) and the worker process."""
```

`gentoo_agent/jobs/launcher.py`:

```python
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
from gentoo_agent.core.errors import RUN_ALREADY_ACTIVE, AgentError
from gentoo_agent.core.runs import (
    ACTIVE_STATUSES,
    acquire_lock,
    new_run_id,
    release_lock,
    request_cancel,
    run_dir,
    run_status,
    update_run,
    write_run,
)


PACKAGE_PARENT = Path(gentoo_agent.__file__).resolve().parents[1]


def worker_command(run_id: str) -> list[str]:
    return [sys.executable, "-m", "gentoo_agent.jobs.worker", run_id]


def worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["GENTOO_AGENT_HOME"] = str(paths.root())
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(PACKAGE_PARENT) + (os.pathsep + existing if existing else "")
    return environment


def start_run(plan_id: str, *, prompt: PasswordPrompt | None = None, resumed_from: str | None = None) -> str:
    plan = require_approved_plan(plan_id)
    password_records, generated = password_input(plan["install"], prompt)
    run_id = new_run_id()
    acquire_lock(run_id)
    try:
        write_run(run_id, {
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
        with (run_dir(run_id) / "worker.err").open("ab") as worker_errors:
            process = subprocess.Popen(
                worker_command(run_id),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=worker_errors,
                cwd=paths.root(),
                env=worker_environment(),
                start_new_session=True,
            )
        update_run(run_id, pid=process.pid)
        payload = {"password_records": password_records, "generated_credentials": generated}
        process.stdin.write(json.dumps(payload).encode("utf-8"))
        process.stdin.close()
    except BaseException as exc:
        try:
            update_run(run_id, status="failed", last_error=f"Could not start the worker: {exc}", finished_at=now())
        except AgentError:
            pass
        release_lock(run_id)
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
```

The worker blocks on reading stdin, which the launcher writes only after recording the pid, so the worker's first `update_run` never races the launcher's.

- [ ] **Step 6: Run the launcher tests**

Run: `python -m unittest tests.test_launcher -v`
Expected: 4 tests PASS.

- [ ] **Step 7: Write the failing worker tests**

`tests/test_worker.py`:

```python
import os
import shutil
import time
import unittest
from unittest.mock import patch

from gentoo_agent.core import approval, runs
from gentoo_agent.jobs import launcher, worker
from tests.helpers import temp_home
from tests.test_launcher import approved_plan


class WorkerTests(unittest.TestCase):
    def new_run(self, plan):
        run_id = runs.new_run_id()
        runs.write_run(run_id, {"run_id": run_id, "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"],
                                "status": "starting", "pid": os.getpid(), "started_at": runs.now()})
        runs.acquire_lock(run_id)
        return run_id

    def test_worker_runs_the_plan_writes_credentials_and_releases_the_lock(self):
        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            outcome = {"status": "failed", "authentication_applied": True}
            with patch.object(worker, "execute_plan", return_value=outcome) as execute:
                result = worker.run(run_id, {"password_records": "root:x\n", "generated_credentials": {"root": "x"}})
            credentials = runs.take_credentials(run_id)
            lock_exists = runs.lock_path().exists()
        self.assertEqual(result, outcome)
        self.assertEqual(execute.call_args.args[2], "root:x\n")
        self.assertEqual(credentials["credentials"], {"root": "x"})
        self.assertTrue(credentials["applied"])
        self.assertFalse(lock_exists)

    def test_worker_refuses_a_plan_whose_approval_is_gone(self):
        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            approval.approval_path(plan["plan_id"]).unlink()
            with patch.object(worker, "execute_plan") as execute:
                result = worker.run(run_id, {"password_records": None, "generated_credentials": {}})
            record = runs.read_run(run_id)
        execute.assert_not_called()
        self.assertEqual(result["error"]["code"], "APPROVAL_REQUIRED")
        self.assertEqual(record["status"], "failed")

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH client not installed")
    def test_real_worker_process_reports_an_unreachable_target(self):
        with temp_home():
            plan = approved_plan(host="gentoo-agent-test.invalid")
            run_id = launcher.start_run(plan["plan_id"])
            deadline = time.monotonic() + 60
            record = runs.run_status(run_id)
            while record["status"] in runs.ACTIVE_STATUSES and time.monotonic() < deadline:
                time.sleep(0.2)
                record = runs.run_status(run_id)
            errors = (runs.run_dir(run_id) / "worker.err").read_text(encoding="utf-8", errors="replace")
            credentials = runs.take_credentials(run_id)
            lock_exists = runs.lock_path().exists()
        self.assertEqual(record["status"], "failed", errors)
        self.assertEqual(record["error"]["code"], "SSH_UNREACHABLE", errors)
        self.assertFalse(credentials["applied"])
        self.assertFalse(lock_exists)


if __name__ == "__main__":
    unittest.main()
```

The third test starts a real detached worker. `.invalid` is a reserved top-level domain, so `ssh` fails at name resolution without contacting any host.

- [ ] **Step 8: Run them to verify they fail**

Run: `python -m unittest tests.test_worker -v`
Expected: ERROR, `ImportError: cannot import name 'worker' from 'gentoo_agent.jobs'`.

- [ ] **Step 9: Create the worker**

`gentoo_agent/jobs/worker.py`:

```python
"""Detached worker process: runs one approved plan and records the outcome.

Started only by jobs.launcher.start_run(). It reads the password records from
stdin, re-checks the approval, runs the plan, writes generated credentials for
one-time pickup, and always releases the run lock.
"""

from __future__ import annotations

import json
import sys

from gentoo_agent.core.approval import require_approved_plan
from gentoo_agent.core.config import now
from gentoo_agent.core.errors import PLAN_HASH_MISMATCH, AgentError
from gentoo_agent.core.runner import execute_plan
from gentoo_agent.core.runs import read_run, release_lock, update_run, write_credentials


def run(run_id: str, payload: dict) -> dict:
    result: dict = {"authentication_applied": False}
    try:
        record = read_run(run_id)
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
```

- [ ] **Step 10: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 98 tests PASS (97 if `ssh` is missing; then the end-to-end test is reported as skipped). If the end-to-end test fails, its assertion message contains `worker.err`.

- [ ] **Step 11: Commit**

```bash
git add gentoo_agent/core/credentials.py gentoo_agent/jobs tests/test_credentials.py tests/test_launcher.py tests/test_worker.py
git commit -m "Add the launcher and detached worker

Passwords are resolved before any phase runs, an empty password variable
now means generate, and every start passes through require_approved_plan."
```

---

### Task 11: CLI for plans, approval, and runs

`approve` reads the confirmation from `/dev/tty` and refuses without an interactive terminal, so a model's non-interactive shell cannot approve. A committed deny rule is the second barrier.

**Files:**
- Modify: `gentoo_agent/cli.py`, `tests/test_cli.py`
- Create: `.claude/settings.json`

- [ ] **Step 1: Write the failing tests** (in `tests/test_cli.py`: put the imports at the top of the file with the existing ones, and the constant, helper class, and test classes after `InstallCommandTests`)

```python
import json
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import approval, paths, phases, runs
from gentoo_agent.core.config import save_json
from tests.helpers import temp_home
from tests.test_launcher import approved_plan


REPO = Path(__file__).resolve().parents[1]


class FakeTty:
    def __init__(self, answer):
        self.answer = answer
        self.written = []

    def write(self, text):
        self.written.append(text)

    def flush(self):
        pass

    def readline(self):
        return self.answer + "\n"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ApproveCommandTests(unittest.TestCase):
    def test_approve_refuses_without_a_terminal(self):
        stderr = io.StringIO()
        with temp_home():
            plan = approved_plan()
            approval.approval_path(plan["plan_id"]).unlink()
            with patch.object(cli, "TTY_PATH", "/nonexistent/tty"), redirect_stderr(stderr):
                code = cli.main(["approve", plan["plan_id"]])
            approved = approval.approval_path(plan["plan_id"]).exists()
        self.assertEqual(code, 1)
        self.assertIn("error [APPROVAL_REQUIRED]: approve needs an interactive terminal", stderr.getvalue())
        self.assertFalse(approved)

    def test_approve_with_the_exact_phrase_records_approval(self):
        with temp_home():
            plan = approved_plan()
            approval.approval_path(plan["plan_id"]).unlink()
            tty = FakeTty(f"APPROVE {plan['plan_id']}")
            with patch.object(cli, "open_tty", return_value=tty), redirect_stdout(io.StringIO()):
                code = cli.main(["approve", plan["plan_id"]])
            status = approval.approval_status(plan["plan_id"])
        self.assertEqual(code, 0)
        self.assertTrue(status["approved"])
        self.assertEqual(status["channel"], "cli")
        self.assertIn(plan["plan_hash"], "".join(tty.written))

    def test_wrong_phrase_records_nothing(self):
        stderr = io.StringIO()
        with temp_home():
            plan = approved_plan()
            approval.approval_path(plan["plan_id"]).unlink()
            with patch.object(cli, "open_tty", return_value=FakeTty("yes")), redirect_stderr(stderr):
                code = cli.main(["approve", plan["plan_id"]])
            approved = approval.approval_path(plan["plan_id"]).exists()
        self.assertEqual(code, 1)
        self.assertIn("APPROVAL_DECLINED", stderr.getvalue())
        self.assertFalse(approved)


class PlanAndRunCommandTests(unittest.TestCase):
    def test_plan_create_saves_a_plan_from_the_config(self):
        with temp_home() as home:
            config = home / "config"
            install = json.loads((REPO / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))
            install.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True})
            save_json(config / "install.json", install)
            save_json(config / "target.json", {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
                                               "identity_file": "~/.ssh/id_test", "target_is_disposable": True,
                                               "disk_erase_authorized": True})
            save_json(config / "plan.json", json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8")))
            stdout = json.dumps({"blockdevices": [{"path": "/dev/sda", "type": "disk", "size": 1, "model": "m",
                                                   "serial": "s1", "wwn": None}]})
            save_json(home / "inventory" / "inventory-20261015T120000Z.json",
                      {"results": [{"label": "disk_identity", "returncode": 0, "stdout": stdout}]})
            output = io.StringIO()
            with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAA test"), redirect_stdout(output):
                code = cli.main(["plan", "create"])
            saved = list(paths.plans_dir().glob("*.json"))
        self.assertEqual(code, 0)
        self.assertEqual(len(saved), 1)
        self.assertIn(f"gentoo-agent approve {saved[0].stem}", output.getvalue())
        self.assertIn("serial=s1", output.getvalue())

    def test_run_status_shows_generated_credentials_once(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": "a" * 16, "status": "failed", "pid": None,
                                    "started_at": runs.now()})
            runs.write_credentials(run_id, {"credentials": {"root": "s3cret-value"}, "applied": True})
            first, second = io.StringIO(), io.StringIO()
            with redirect_stdout(first):
                cli.main(["run", "status", run_id])
            with redirect_stdout(second):
                cli.main(["run", "status", run_id])
        self.assertIn("s3cret-value", first.getvalue())
        self.assertNotIn("s3cret-value", second.getvalue())


class DenyRuleTests(unittest.TestCase):
    def test_agent_may_not_run_approve(self):
        settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        for rule in (
            "Bash(gentoo-agent approve:*)",
            "Bash(python -m gentoo_agent approve:*)",
            "Bash(python3 -m gentoo_agent approve:*)",
            "Bash(python tools/gentoo_agent.py approve:*)",
            "Bash(python3 tools/gentoo_agent.py approve:*)",
        ):
            self.assertIn(rule, deny)
```

Add `from contextlib import redirect_stdout` to the existing `from contextlib import redirect_stderr` import.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_cli -v`
Expected: the six new tests FAIL or ERROR (argparse `invalid choice: 'approve'` / `'plan'` / `'run'` exits with `SystemExit: 2`, missing `.claude/settings.json`).

- [ ] **Step 3: Add the commands to `gentoo_agent/cli.py`**

Add to the imports:

```python
import getpass

from gentoo_agent.core.approval import (
    APPROVAL_TTL_MINUTES,
    approval_phrase,
    approval_status,
    approval_summary,
    record_approval,
)
from gentoo_agent.core.errors import APPROVAL_DECLINED
from gentoo_agent.core.install_plan import create_install_plan, load_install_plan, save_install_plan
from gentoo_agent.core.runs import ACTIVE_STATUSES, read_log, run_status, take_credentials
from gentoo_agent.jobs.launcher import cancel_run, resume_run, start_run
```

(merge `APPROVAL_DECLINED` into the existing `gentoo_agent.core.errors` import line).

Add after `EXECUTE_REPLACED`:

```python
TTY_PATH = "/dev/tty"
NO_TERMINAL = (
    "approve needs an interactive terminal. Run it yourself in a terminal; "
    "it cannot be completed by an AI agent or a script."
)


def open_tty():
    try:
        handle = open(TTY_PATH, "r+", encoding="utf-8")
    except OSError as exc:
        raise AgentError(APPROVAL_REQUIRED, NO_TERMINAL) from exc
    if not handle.isatty():
        handle.close()
        raise AgentError(APPROVAL_REQUIRED, NO_TERMINAL)
    return handle


def ask_password(label: str) -> str:
    first = getpass.getpass(f"New Gentoo {label} password: ")
    second = getpass.getpass(f"Repeat Gentoo {label} password: ")
    if not first or first != second:
        raise AgentError(CONFIG_INVALID, f"Passwords for {label} were empty or did not match.")
    return first


def cmd_plan_create(_: argparse.Namespace) -> int:
    plan = save_install_plan(create_install_plan(target_config(), install_config(), load_plan(), latest_inventory()))
    print(approval_summary(plan))
    print(f"\nPlan saved: {plan['plan_id']}")
    print(f"Approve it yourself in a terminal: gentoo-agent approve {plan['plan_id']}")
    return 0


def cmd_plan_show(args: argparse.Namespace) -> int:
    plan = load_install_plan(args.plan_id)
    print(approval_summary(plan))
    print(json.dumps({"approval": approval_status(args.plan_id)}, indent=2))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    plan = load_install_plan(args.plan_id)
    phrase = approval_phrase(plan)
    with open_tty() as tty:
        tty.write(approval_summary(plan) + "\n\n")
        tty.write(f"Type {phrase} to approve this plan for {APPROVAL_TTL_MINUTES} minutes: ")
        tty.flush()
        answer = tty.readline().strip()
    if answer != phrase:
        raise AgentError(APPROVAL_DECLINED, "Approval not recorded.", {"plan_id": plan["plan_id"]})
    record = record_approval(plan, "cli")
    print(f"Approved until {record['expires_at']}. Start with: gentoo-agent run start {plan['plan_id']}")
    return 0


def cmd_run_start(args: argparse.Namespace) -> int:
    run_id = start_run(args.plan_id, prompt=ask_password)
    print(f"Run started: {run_id}")
    print(f"Follow it with: gentoo-agent run status {run_id}  and  gentoo-agent run log {run_id}")
    return 0


def cmd_run_status(args: argparse.Namespace) -> int:
    record = run_status(args.run_id)
    print(json.dumps(record, indent=2))
    if record["status"] not in ACTIVE_STATUSES:
        credentials = take_credentials(args.run_id)
        if credentials:
            state = "were applied" if credentials.get("applied") else "were generated but the authentication phase did not run"
            print(f"Generated credentials ({state}; shown once, save them now):")
            for username, password in credentials["credentials"].items():
                print(f"  {username}: {password}")
    return 0


def cmd_run_log(args: argparse.Namespace) -> int:
    print(json.dumps(read_log(args.run_id, args.offset, args.limit), indent=2))
    return 0


def cmd_run_cancel(args: argparse.Namespace) -> int:
    print(json.dumps(cancel_run(args.run_id), indent=2))
    return 0


def cmd_run_resume(args: argparse.Namespace) -> int:
    run_id = resume_run(args.run_id, prompt=ask_password)
    print(f"Run started: {run_id} (resuming {args.run_id})")
    return 0
```

In `build_parser`, before `return parser`, add:

```python
    plan = sub.add_parser("plan", help="Create or show an install plan")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    plan_sub.add_parser("create", help="Freeze the current config, disk identity, and commands into a plan").set_defaults(func=cmd_plan_create)
    plan_show = plan_sub.add_parser("show", help="Show a plan and its approval status")
    plan_show.add_argument("plan_id")
    plan_show.set_defaults(func=cmd_plan_show)
    approve = sub.add_parser("approve", help="Approve a plan (interactive terminal only)")
    approve.add_argument("plan_id")
    approve.set_defaults(func=cmd_approve)
    run = sub.add_parser("run", help="Start and follow background runs of approved plans")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    run_start = run_sub.add_parser("start", help="Start an approved plan in the background")
    run_start.add_argument("plan_id")
    run_start.set_defaults(func=cmd_run_start)
    for name, func, help_text in (
        ("status", cmd_run_status, "Show a run's status (and generated credentials once)"),
        ("cancel", cmd_run_cancel, "Stop a run at the next phase boundary"),
        ("resume", cmd_run_resume, "Start a new run of the same plan; completed phases are skipped"),
    ):
        command = run_sub.add_parser(name, help=help_text)
        command.add_argument("run_id")
        command.set_defaults(func=func)
    run_log = run_sub.add_parser("log", help="Print part of a run's log")
    run_log.add_argument("run_id")
    run_log.add_argument("--offset", type=int, default=0)
    run_log.add_argument("--limit", type=int, default=50)
    run_log.set_defaults(func=cmd_run_log)
```

- [ ] **Step 4: Create `.claude/settings.json`**

```json
{
  "permissions": {
    "deny": [
      "Bash(gentoo-agent approve:*)",
      "Bash(python -m gentoo_agent approve:*)",
      "Bash(python3 -m gentoo_agent approve:*)",
      "Bash(python tools/gentoo_agent.py approve:*)",
      "Bash(python3 tools/gentoo_agent.py approve:*)",
      "Bash(tools/gentoo_agent.py approve:*)",
      "Bash(./tools/gentoo_agent.py approve:*)"
    ]
  }
}
```

Check that `.gitignore` ignores only `.claude/worktrees/`, not the whole `.claude/` directory: `git check-ignore -v .claude/settings.json` must print nothing.

- [ ] **Step 5: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: 104 tests PASS (103 with the end-to-end test skipped).

- [ ] **Step 6: Smoke-test the approval refusal from a non-interactive shell**

Run: `python tools/gentoo_agent.py approve 0000000000000000 < /dev/null; echo "exit=$?"`
Expected: `error [PLAN_NOT_FOUND]: Plan 0000000000000000 does not exist.` and `exit=1` (the plan check runs before the terminal is opened).

- [ ] **Step 7: Commit**

```bash
git add gentoo_agent/cli.py tests/test_cli.py .claude/settings.json
git commit -m "Add plan, approve, and run commands; approval needs a real terminal"
```

---

### Task 12: Documentation

**Files:**
- Modify: `AGENTS.md`, `README.md`, `docs/CONFIGURATION.md`, `CLAUDE.md`, `docs/superpowers/specs/2026-10-15-mcp-server-design.md`

Writing rules for every added sentence: no em dashes, no emoji.

- [ ] **Step 1: AGENTS.md safety rules**

Replace these two bullets:

```markdown
- Never partition, format, delete, repair, replace a bootloader, or reboot
  unless the reviewed configuration explicitly authorizes the operation. For
  this disposable VM, disk erasure is authorized by both
  `target_is_disposable: true` and `disk_erase_authorized: true`; execution is
  still separately controlled by `execution_enabled`.
- Do not start execution while `execution_enabled` is `false` or
  `review_status` is not `approved`.
```

with:

```markdown
- Never partition, format, delete, repair, replace a bootloader, or reboot
  unless the reviewed configuration explicitly authorizes the operation. Disk
  erasure requires `disk_must_be_erased`, `target_is_disposable`, and
  `disk_erase_authorized` set to `true` in both `config/target.json` and
  `config/install.json`; `plan create` refuses otherwise.
- Execution happens only through an approved plan: `plan create`, then a human
  runs `approve <plan_id>` in their own terminal, then `run start <plan_id>`.
  Never run `approve` yourself and never ask to have a deny rule removed. An
  approval is bound to the plan hash, expires after 60 minutes, and is void
  when the plan or the disk identity changes.
```

Replace the paragraph under `## Current implementation status` with:

```markdown
The repository provides the read-only inspector, Handbook fetcher, plans bound
by `plan_hash`, human approval records, and a background worker. Code lives in
`gentoo_agent/core/` (phase generators in `phases.py`, plans in
`install_plan.py`, approvals in `approval.py`, runs in `runs.py`, the executor
in `runner.py`) and `gentoo_agent/jobs/` (launcher and worker). `install
--dry-run` prints commands without connecting. A run starts only from an
approved plan, re-checks the disk serial before any change, and never reboots
the target. Authentication installs and configures sudo for the configured
wheel-group user by default. Test on the disposable VM before using physical
hardware.
```

- [ ] **Step 2: README execution flow**

Replace everything from `After reviewing \`config/target.json\`, \`config/install.json\`, the latest` through the code block containing `install --execute --confirm` with:

````markdown
After reviewing `config/target.json`, `config/install.json`, the latest
inventory, and the dry-run output, freeze them into a plan:

```sh
python tools/gentoo_agent.py plan create
```

The plan records the generated commands and the target disk serial, and its
`plan_hash` covers both. Approve it yourself in an interactive terminal; the
command refuses to run without one, so an AI agent cannot approve on your
behalf:

```sh
python tools/gentoo_agent.py approve <plan_id>
```

The approval is valid for 60 minutes. Start the run, which continues in the
background, and follow it:

```sh
python tools/gentoo_agent.py run start <plan_id>
python tools/gentoo_agent.py run status <run_id>
python tools/gentoo_agent.py run log <run_id>
```
````

In the paragraph that follows, replace `A failure stops the workflow and preserves
the local log under \`logs/\`` with `A failure stops the run and preserves its log under \`runs/<run_id>/\``, and replace the sentence starting `If
\`--from-phase\` is omitted` with: `After a failure, \`run resume <run_id>\` starts a new run of the same plan and skips phases whose target state is already complete.`

Replace `Generated
credentials are printed once after successful completion` with `Generated
credentials are shown once by \`run status\``.

In `## Workflow`, replace steps 5 and 6 with:

```markdown
5. Run `install --dry-run` and review the generated commands.
6. Run `plan create`, approve the plan yourself with `approve <plan_id>`, then `run start <plan_id>`.
```

- [ ] **Step 3: CONFIGURATION.md**

- In the `config/target.json` required-fields table, delete the `execution_enabled` row.
- In the optional-fields table, delete the `dangerous_operations_enabled` row.
- In the `config/install.json` table, delete the `execution_enabled` and `review_status` rows.
- In the PowerShell password example, replace `python tools/gentoo_agent.py install --execute --confirm "INSTALL gentoo ON /dev/sda"` with `python tools/gentoo_agent.py run start <plan_id>`, and replace `The generated credentials are printed once after
all phases complete` with `The generated credentials are shown once by \`run status\``.
- Replace the `## Starting the guarded executor` section body with:

````markdown
The default command is read-only and prints generated commands:

```powershell
python tools/gentoo_agent.py install --dry-run
```

Freeze the reviewed configuration into a plan, approve it yourself in an
interactive terminal, and start it:

```powershell
python tools/gentoo_agent.py plan create
python tools/gentoo_agent.py approve <plan_id>
python tools/gentoo_agent.py run start <plan_id>
```

The run continues in the background and writes `runs/<run_id>/run.json` and
`log.jsonl`. It first re-reads the disk serial and WWN and stops with
`TARGET_DRIFT` if they differ from the plan. It stops on failure and never
reboots. `run resume <run_id>` starts a new run of the same plan and skips
phases whose target state is already complete. `run cancel <run_id>` stops at
the next phase boundary.
````

- In the `## Current proposed layout` paragraph, replace `but execution remains disabled. The executor checks
the live boot mode and exact disk model/size again immediately before storage
changes.` with `and execution still requires an approved plan. The worker checks the disk identity again before any change.`

Run: `grep -rnE "execution_enabled|review_status|approve-install|--confirm|dangerous_operations" README.md AGENTS.md docs/CONFIGURATION.md`
Expected: no output.

- [ ] **Step 4: CLAUDE.md**

Replace the paragraph that starts `Commands that touch the target` with:

```markdown
Commands that touch the target (`inspect`, `handbook`, `prepare_install.py`, `run start`, `run resume`) are operator actions; do not run them unless asked. Never run `approve`: it must be a human in an interactive terminal, and `.claude/settings.json` denies it.
```

In `## Architecture`, replace the `core/runner.py` bullet with these bullets:

```markdown
- `core/install_plan.py`: `create_install_plan()` freezes the validated config, SSH target, disk identity (serial/WWN from the `disk_identity` inventory command, `core/disks.py`), and every generated apply/check command into `plans/<plan_id>.json`. `plan_hash` is SHA-256 over all of it; `plan_id` is its first 16 hex characters. `load_install_plan()` rejects modified files.
- `core/approval.py`: `record_approval()` writes `approvals/<plan_id>.json` (60-minute expiry, bound to `plan_hash`); `require_approved_plan()` is the single execution gate. Only humans approve: the TTY-only `approve` CLI command now, MCP elicitation later.
- `jobs/launcher.py`: `start_run()` is the only way to start work. It calls `require_approved_plan()`, resolves passwords before any phase, takes the single-run lock (`core/runs.py`), and spawns `python -m gentoo_agent.jobs.worker <run_id>` detached, passing password records over stdin.
- `core/runner.py`: `execute_plan()` (run by the worker) re-checks the live disk identity (`TARGET_DRIFT`), then per phase runs the stored check (exit 0 means done, skip), apply, and check again as verify, each as one `sh -c` program via `core/ssh.remote_shell()`. It honors `runs/<run_id>/cancel` between phases, records every outcome in `runs/<run_id>/run.json` and `log.jsonl`, and never reboots. `render_dry_run()` backs `install --dry-run`.
- `core/paths.py`: functions resolved on every call (`GENTOO_AGENT_HOME` overrides the root); tests use `tests/helpers.temp_home()`.
```

In `## Configuration files`, remove any mention of `execution_enabled` or `approve-install`, and add `plans/`, `approvals/`, and `runs/` to the list of private generated directories.

- [ ] **Step 5: Spec error codes**

In `docs/superpowers/specs/2026-10-15-mcp-server-design.md`, replace the error-code paragraph under `### Error codes` with:

```markdown
`HOST_KEY_UNKNOWN`, `SSH_UNREACHABLE`, `CONFIG_INVALID`, `CONFIG_CONFLICT`,
`DISK_AMBIGUOUS`, `INVENTORY_MISSING`, `HANDBOOK_UNAVAILABLE`,
`PLAN_NOT_FOUND`, `PLAN_HASH_MISMATCH`, `APPROVAL_REQUIRED`,
`APPROVAL_EXPIRED`, `APPROVAL_DECLINED`, `TARGET_DRIFT`, `RUN_NOT_FOUND`,
`RUN_ALREADY_ACTIVE`, `PHASE_PRECONDITION_FAILED`, `PHASE_FAILED`.
```

and in the `## Approval` section replace `The human runs it in their own terminal or with
\`! gentoo-agent approve <plan_id>\` in Claude Code.` with `The human runs it in their own terminal. Commands run through Claude Code's \`!\` prefix may not have an interactive terminal, so a separate terminal is the reliable choice.`

- [ ] **Step 6: Gitignore the new private directories**

Append to `.gitignore`:

```
plans/*.json
approvals/*.json
runs/
```

(`approvals/*.json` is already present; add only the missing lines.)

- [ ] **Step 7: Run all tests and commit**

Run: `python -m unittest discover -s tests -v`
Expected: 104 tests PASS.

```bash
git add AGENTS.md README.md docs/CONFIGURATION.md CLAUDE.md docs/superpowers/specs/2026-10-15-mcp-server-design.md .gitignore
git commit -m "Document approved plans, the worker, and the new CLI commands"
```
