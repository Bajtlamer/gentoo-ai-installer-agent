# Core Package Refactor Implementation Plan (Plan 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the existing installer from `tools/*.py` into a `gentoo_agent/` package whose `core/` never prints, prompts, or exits, while generating byte-identical remote commands.

**Architecture:** Golden snapshots of today's generated shell programs are captured first and guard every later step. The code then moves into `gentoo_agent/core/` (pure logic, typed `AgentError` with stable codes) plus `gentoo_agent/cli.py` and `gentoo_agent/prepare.py` (front ends that print and prompt). `tools/*.py` become thin compatibility shims so every documented command keeps working.

**Tech Stack:** Python 3.12+ standard library, `unittest`, setuptools `pyproject.toml` (no runtime dependencies yet).

**Spec:** `docs/superpowers/specs/2026-10-15-mcp-server-design.md`, build order steps 1 to 3.

**Plan series:**
1. This plan: golden snapshots, package move, pure core.
2. Plan, `plan_hash`, approval records, background worker, TTY-only CLI approval.
3. MCP server (tools, resources, prompt, elicitation).
4. Hardware profile, value inference, kernel plan.

**Known issues fixed along the way** (each called out in its task):
- `install --execute` exits 0 even when a phase fails (CLI discards the return code).
- Generated passwords are lost when a run fails after the authentication phase.
- `handbook` fails when `inventory/` does not exist yet (writes HTML before creating the directory).
- README claims Python 3.10; `chroot()` uses a backslash inside an f-string, which needs 3.12.

All test commands run from the repository root.

---

## File structure after this plan

```
pyproject.toml                    package metadata, console script gentoo-agent
gentoo_agent/
  __init__.py                     __version__
  __main__.py                     python -m gentoo_agent
  cli.py                          argparse front end (prints, prompts)
  prepare.py                      interactive beginner flow (was tools/prepare_install.py)
  init_config.py                  config bootstrap (was tools/init_config.py)
  core/
    __init__.py
    errors.py                     AgentError and error codes
    paths.py                      ROOT and directory constants
    config.py                     JSON io, target/install/plan loading
    ssh.py                        ssh_base, read-only allowlist, remote_shell
    state.py                      state/state.json load and save
    inventory.py                  read-only inventory collection
    handbook.py                   Handbook snapshot
    validate.py                   validated_settings, validate_installation
    phases.py                     PHASES, MOUNT_ROOT, apply/check generators
    credentials.py                chpasswd records from env, generation, or prompt callback
    runner.py                     execution gate, check/apply/verify loop
    approval.py                   approve-install flag changes
tools/gentoo_agent.py             shim -> gentoo_agent.cli
tools/prepare_install.py          shim -> gentoo_agent.prepare
tools/init_config.py              shim -> gentoo_agent.init_config
tests/
  golden/configs/*.json           four install configurations
  golden/*.txt                    generated commands per configuration
  test_golden_commands.py
  test_authentication.py          (existing, imports updated)
  test_errors.py, test_phases.py, test_state_inventory.py, test_handbook.py,
  test_validate.py, test_credentials.py, test_runner.py, test_approval.py,
  test_core_purity.py, test_prepare.py
```

---

### Task 1: Golden snapshots of today's generated commands

**Files:**
- Create: `tests/golden/configs/bios_mbr_user_sudo.json`
- Create: `tests/golden/configs/uefi_gpt_user_sudo.json`
- Create: `tests/golden/configs/uefi_gpt_root_only_static.json`
- Create: `tests/golden/configs/bios_mbr_user_no_sudo.json`
- Create: `tests/test_golden_commands.py`
- Create (generated): `tests/golden/*.txt`

- [ ] **Step 1: Create the four fixture configurations**

`tests/golden/configs/bios_mbr_user_sudo.json`:

```json
{
  "disk": "/dev/sda",
  "boot_mode": "bios",
  "partition_table": "mbr",
  "root_filesystem": "ext4",
  "partition_layout": [
    {"device": "/dev/sda1", "size_mib": 2048, "type": "linux-swap", "mount": null},
    {"device": "/dev/sda2", "size": "remaining", "type": "linux-root", "filesystem": "ext4", "mount": "/"}
  ],
  "init_system": "openrc",
  "hostname": "gentoo",
  "timezone": "Europe/Prague",
  "network_method": "dhcp",
  "create_user": true,
  "username": "gentoo",
  "install_user_ssh_key": true,
  "user_groups": ["wheel"],
  "configure_sudo": true,
  "user_shell": "/bin/bash",
  "set_root_password": true,
  "allow_root_password_ssh": false,
  "password_source": "environment",
  "generate_missing_passwords": true,
  "root_password_env": "GENTOO_ROOT_PASSWORD",
  "user_password_env": "GENTOO_USER_PASSWORD",
  "expected_disk_model": "QEMU HARDDISK",
  "expected_disk_size_gib": 40
}
```

`tests/golden/configs/uefi_gpt_user_sudo.json`:

```json
{
  "disk": "/dev/nvme0n1",
  "boot_mode": "uefi",
  "partition_table": "gpt",
  "root_filesystem": "ext4",
  "partition_layout": [
    {"device": "/dev/nvme0n1p1", "size_mib": 512, "type": "efi-system", "filesystem": "vfat", "mount": "/boot"},
    {"device": "/dev/nvme0n1p2", "size_mib": 4096, "type": "linux-swap", "mount": null},
    {"device": "/dev/nvme0n1p3", "size": "remaining", "type": "linux-root", "filesystem": "ext4", "mount": "/"}
  ],
  "init_system": "openrc",
  "hostname": "server",
  "timezone": "Europe/Prague",
  "network_method": "dhcp",
  "create_user": true,
  "username": "alex",
  "install_user_ssh_key": true,
  "user_groups": ["wheel"],
  "configure_sudo": true,
  "user_shell": "/bin/bash",
  "set_root_password": true,
  "allow_root_password_ssh": true,
  "password_source": "environment",
  "generate_missing_passwords": true,
  "root_password_env": "GENTOO_ROOT_PASSWORD",
  "user_password_env": "ALEX_PW"
}
```

`tests/golden/configs/uefi_gpt_root_only_static.json`:

```json
{
  "disk": "/dev/vda",
  "boot_mode": "uefi",
  "partition_table": "gpt",
  "root_filesystem": "ext4",
  "partition_layout": [
    {"device": "/dev/vda1", "size_mib": 512, "type": "efi-system", "filesystem": "vfat", "mount": "/boot"},
    {"device": "/dev/vda2", "size": "remaining", "type": "linux-root", "filesystem": "ext4", "mount": "/"}
  ],
  "init_system": "openrc",
  "hostname": "edge01",
  "timezone": "UTC",
  "network_method": "static",
  "network_static": {"address": "192.168.10.20/24", "gateway": "192.168.10.1", "dns": ["1.1.1.1", "9.9.9.9"]},
  "create_user": false,
  "install_user_ssh_key": false,
  "configure_sudo": false,
  "set_root_password": true,
  "allow_root_password_ssh": false,
  "password_source": "generate"
}
```

`tests/golden/configs/bios_mbr_user_no_sudo.json`:

```json
{
  "disk": "/dev/sdb",
  "boot_mode": "bios",
  "partition_table": "mbr",
  "root_filesystem": "ext4",
  "partition_layout": [
    {"device": "/dev/sdb1", "size_mib": 1024, "type": "linux-swap", "mount": null},
    {"device": "/dev/sdb2", "size": "remaining", "type": "linux-root", "filesystem": "ext4", "mount": "/"}
  ],
  "init_system": "openrc",
  "hostname": "lab",
  "timezone": "Europe/Prague",
  "network_method": "dhcp",
  "create_user": true,
  "username": "alex",
  "install_user_ssh_key": false,
  "user_groups": ["wheel", "audio"],
  "configure_sudo": false,
  "user_shell": "/bin/bash",
  "set_root_password": false,
  "allow_root_password_ssh": false,
  "password_source": "environment",
  "generate_missing_passwords": true,
  "root_password_env": "GENTOO_ROOT_PASSWORD",
  "user_password_env": "GENTOO_USER_PASSWORD"
}
```

- [ ] **Step 2: Write the golden test against the current `tools/install_phases.py`**

`tests/test_golden_commands.py`:

```python
"""Generated remote commands must stay byte-identical across refactors.

Regenerate only on an intentional command change:
    GOLDEN_UPDATE=1 python -m unittest tests.test_golden_commands
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
TARGET = {
    "name": "gentoo",
    "host": "192.0.2.10",
    "port": 22,
    "user": "root",
    "identity_file": "~/.ssh/id_test",
}
PUBLIC_KEY = "ssh-ed25519 AAAAGOLDEN gentoo-agent-golden"


def render(install: dict) -> str:
    sys.path.insert(0, str(ROOT / "tools"))
    import install_phases

    with patch("install_phases.target_config", return_value=TARGET), patch(
        "install_phases.public_key_for_target", return_value=PUBLIC_KEY
    ):
        parts = []
        for phase in install_phases.PHASES:
            parts.append(f"===== {phase} apply =====")
            parts.append(install_phases.command_for(phase, TARGET, install))
            parts.append(f"===== {phase} check =====")
            parts.append(install_phases.check_command_for(phase, TARGET, install))
        return "\n".join(parts) + "\n"


class GoldenCommandTests(unittest.TestCase):
    def test_generated_commands_match_golden_files(self):
        configs = sorted((GOLDEN / "configs").glob("*.json"))
        self.assertEqual(len(configs), 4)
        for config_path in configs:
            with self.subTest(config=config_path.stem):
                install = json.loads(config_path.read_text(encoding="utf-8"))
                rendered = render(install)
                golden_path = GOLDEN / f"{config_path.stem}.txt"
                if os.environ.get("GOLDEN_UPDATE") == "1":
                    golden_path.write_text(rendered, encoding="utf-8", newline="\n")
                self.assertEqual(golden_path.read_text(encoding="utf-8"), rendered)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it to verify it fails (no golden files yet)**

Run: `python -m unittest tests.test_golden_commands -v`
Expected: FAIL / ERROR with `FileNotFoundError` for `tests/golden/bios_mbr_user_no_sudo.txt`.

- [ ] **Step 4: Generate the golden files from the current code**

Run: `GOLDEN_UPDATE=1 python -m unittest tests.test_golden_commands -v`
Expected: PASS, and four files `tests/golden/*.txt` exist.

- [ ] **Step 5: Inspect one golden file**

Run: `grep -c "=====" tests/golden/uefi_gpt_user_sudo.txt && grep -n "grub-install" tests/golden/uefi_gpt_user_sudo.txt`
Expected: `18` and one line containing `grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=Gentoo`.

- [ ] **Step 6: Run the test again without the update flag**

Run: `python -m unittest discover -s tests -v`
Expected: all 10 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/golden tests/test_golden_commands.py
git commit -m "Add golden snapshots of generated phase commands"
```

---

### Task 2: Move the code into the `gentoo_agent` package

This task changes no behavior except the exit-code fix in Step 7. The golden test is the proof.

**Files:**
- Create: `pyproject.toml`, `gentoo_agent/__init__.py`, `gentoo_agent/__main__.py`, `gentoo_agent/core/__init__.py`, `gentoo_agent/core/paths.py`, `gentoo_agent/core/config.py`, `gentoo_agent/core/ssh.py`
- Move: `tools/install_phases.py` -> `gentoo_agent/core/phases.py`
- Move: `tools/gentoo_agent.py` -> `gentoo_agent/cli.py`
- Move: `tools/prepare_install.py` -> `gentoo_agent/prepare.py`
- Move: `tools/init_config.py` -> `gentoo_agent/init_config.py`
- Create: shims `tools/gentoo_agent.py`, `tools/prepare_install.py`, `tools/init_config.py`
- Modify: `tests/test_authentication.py`, `tests/test_golden_commands.py`

- [ ] **Step 1: Create package metadata and skeleton**

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "gentoo-agent"
version = "0.2.0"
description = "Supervised Gentoo installation over SSH"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
gentoo-agent = "gentoo_agent.cli:main"

[tool.setuptools.packages.find]
include = ["gentoo_agent*"]
```

`gentoo_agent/__init__.py`:

```python
"""Supervised Gentoo installation over SSH."""

__version__ = "0.2.0"
```

`gentoo_agent/__main__.py`:

```python
from gentoo_agent.cli import main

raise SystemExit(main())
```

`gentoo_agent/core/__init__.py`:

```python
"""Pure installer logic: no printing, prompting, or process exits."""
```

- [ ] **Step 2: Create `gentoo_agent/core/paths.py`**

```python
"""Filesystem locations used by the agent.

GENTOO_AGENT_HOME overrides the repository root, which keeps an editable
install (pip install -e .) and the tools/ shims pointing at the same files.
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(os.environ.get("GENTOO_AGENT_HOME") or Path(__file__).resolve().parents[2])
CONFIG = ROOT / "config"
STATE_DIR = ROOT / "state"
INVENTORY_DIR = ROOT / "inventory"
LOG_DIR = ROOT / "logs"
APPROVAL_DIR = ROOT / "approvals"
```

- [ ] **Step 3: Create `gentoo_agent/core/config.py`** (bodies copied from `tools/gentoo_agent.py`, plus `load_plan` from `tools/install_phases.py`)

```python
"""Local JSON configuration: loading, atomic saving, timestamps."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from gentoo_agent.core.paths import CONFIG, ROOT


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    temporary.replace(path)


def target_config() -> dict:
    path = CONFIG / "target.json"
    if not path.exists():
        raise SystemExit("Missing config/target.json; copy target.example.json first.")
    return load_json(path)


def install_config() -> dict:
    target = target_config()
    configured_path = ROOT / target.get("install_config", "config/install.json")
    if not configured_path.exists():
        raise SystemExit(f"Missing installation configuration: {configured_path}")
    return load_json(configured_path)


def load_plan() -> dict:
    return load_json(CONFIG / "plan.json")
```

- [ ] **Step 4: Create `gentoo_agent/core/ssh.py`** (copied from `tools/gentoo_agent.py`; `remote_shell` joins it in Task 6)

```python
"""The only module that talks to the target, over OpenSSH."""

from __future__ import annotations

import os
import shlex
import subprocess

from gentoo_agent.core.paths import ROOT


def ssh_base(target: dict) -> list[str]:
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    known_hosts = target.get("known_hosts_file", "")
    if known_hosts:
        command += ["-o", f"UserKnownHostsFile={os.path.expanduser(known_hosts)}"]
    if target.get("identity_file"):
        command += ["-i", os.path.expanduser(target["identity_file"])]
    if target.get("port"):
        command += ["-p", str(target["port"])]
    command.append(f"{target['user']}@{target['host']}")
    return command


READ_ONLY_COMMANDS = {
    "identity": "printf 'hostname='; hostname; printf '\\narch='; uname -m; printf '\\nkernel='; uname -r",
    "boot_mode": "if [ -d /sys/firmware/efi ]; then printf 'uefi'; else printf 'bios-or-unknown'; fi",
    "storage": "lsblk -e7 -o NAME,PATH,SIZE,TYPE,FSTYPE,FSVER,LABEL,UUID,MOUNTPOINTS,ROTA,MODEL",
    "storage_json": "lsblk -J -e7 -o NAME,PATH,SIZE,TYPE,FSTYPE,MODEL,MOUNTPOINTS",
    "block_ids": "blkid || true",
    "mounts": "findmnt -D || mount",
    "network": "ip -brief address; printf '\\n'; ip route",
    "errors": "dmesg --level=err,warn 2>/dev/null | tail -n 200 || true",
}


def run_ssh_readonly(target: dict, label: str, command: str) -> dict:
    # OpenSSH joins all arguments after the host into one remote command. Quote
    # the shell program explicitly so pipes, semicolons, and conditionals are
    # interpreted on the target instead of being split by the SSH invocation.
    remote_command = "sh -c " + shlex.quote(command)
    completed = subprocess.run(
        ssh_base(target) + ["--", remote_command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    return {
        "label": label,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
```

- [ ] **Step 5: Move `install_phases.py` into the package**

Run: `git mv tools/install_phases.py gentoo_agent/core/phases.py`

In `gentoo_agent/core/phases.py`, delete the first line `#!/usr/bin/env python3` and replace this block:

```python
from gentoo_agent import (
    CONFIG,
    LOG_DIR,
    ROOT,
    STATE_DIR,
    install_config,
    load_json,
    now,
    run_ssh_readonly,
    save_json,
    ssh_base,
    target_config,
)
```

with:

```python
from gentoo_agent.core.config import install_config, load_json, now, save_json, target_config
from gentoo_agent.core.paths import CONFIG, LOG_DIR, ROOT, STATE_DIR
from gentoo_agent.core.ssh import ssh_base
```

- [ ] **Step 6: Move the CLI into the package**

Run: `git mv tools/gentoo_agent.py gentoo_agent/cli.py`

In `gentoo_agent/cli.py`:

1. Delete the first line `#!/usr/bin/env python3`.
2. Delete everything from `ROOT = Path(__file__).resolve().parents[1]` through the end of the `run_ssh_readonly` function (the path constants, `now`, `load_json`, `save_json`, `target_config`, `install_config`, `ssh_base`, `READ_ONLY_COMMANDS`, `run_ssh_readonly`) and put this in their place:

```python
from gentoo_agent.core.config import install_config, load_json, now, save_json, target_config
from gentoo_agent.core.paths import APPROVAL_DIR, CONFIG, INVENTORY_DIR, ROOT, STATE_DIR
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS, run_ssh_readonly
```

3. Replace each of the three lazy imports:
   - `from install_phases import cmd_install` (in `cmd_phase` and `cmd_install_command`) with `from gentoo_agent.core.phases import cmd_install`
   - `from install_phases import approve_execution` with `from gentoo_agent.core.phases import approve_execution`

- [ ] **Step 7: Give the CLI a `main()` and fix the discarded exit code**

In `gentoo_agent/cli.py` replace:

```python
def cmd_install_command(args: argparse.Namespace) -> None:
    from gentoo_agent.core.phases import cmd_install
    cmd_install(args)
```

with:

```python
def cmd_install_command(args: argparse.Namespace) -> int:
    from gentoo_agent.core.phases import cmd_install
    return cmd_install(args)
```

and replace the final block:

```python
if __name__ == "__main__":
    try:
        parsed = build_parser().parse_args()
        parsed.func(parsed)
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
```

with:

```python
def main(argv: list[str] | None = None) -> int:
    parsed = build_parser().parse_args(argv)
    try:
        result = parsed.func(parsed)
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Before this change a failed `install --execute` exited 0 because `cmd_install`'s return code was dropped.

- [ ] **Step 8: Move the two front-end scripts**

Run:

```bash
git mv tools/prepare_install.py gentoo_agent/prepare.py
git mv tools/init_config.py gentoo_agent/init_config.py
```

In `gentoo_agent/prepare.py`, delete `#!/usr/bin/env python3` and replace:

```python
from gentoo_agent import (
    CONFIG,
    INVENTORY_DIR,
    ROOT,
    READ_ONLY_COMMANDS,
    load_json,
    now,
    run_ssh_readonly,
    save_json,
)
```

with:

```python
from gentoo_agent.core.config import load_json, now, save_json
from gentoo_agent.core.paths import CONFIG, INVENTORY_DIR, ROOT
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS, run_ssh_readonly
```

In `gentoo_agent/init_config.py`, delete `#!/usr/bin/env python3` and replace:

```python
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
```

with:

```python
from gentoo_agent.core.paths import CONFIG
```

- [ ] **Step 9: Create the three compatibility shims**

`tools/gentoo_agent.py`:

```python
#!/usr/bin/env python3
"""Compatibility entry point; the implementation lives in gentoo_agent.cli."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gentoo_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
```

`tools/prepare_install.py`:

```python
#!/usr/bin/env python3
"""Compatibility entry point; the implementation lives in gentoo_agent.prepare."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gentoo_agent.prepare import main  # noqa: E402

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
```

`tools/init_config.py`:

```python
#!/usr/bin/env python3
"""Compatibility entry point; the implementation lives in gentoo_agent.init_config."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gentoo_agent.init_config import main  # noqa: E402

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
```

Run: `chmod +x tools/gentoo_agent.py tools/prepare_install.py tools/init_config.py`

- [ ] **Step 10: Point the tests at the package**

In `tests/test_authentication.py` replace:

```python
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import install_phases  # noqa: E402
```

with:

```python
from gentoo_agent.core import phases as install_phases
```

remove the now unused `import sys` and `from pathlib import Path`, and replace the two patch target strings:
- `"install_phases.public_key_for_target"` -> `"gentoo_agent.core.phases.public_key_for_target"`
- `"install_phases.secrets.token_urlsafe"` -> `"gentoo_agent.core.phases.secrets.token_urlsafe"`

In `tests/test_golden_commands.py` replace the `render` function with:

```python
def render(install: dict) -> str:
    from gentoo_agent.core import phases

    with patch("gentoo_agent.core.phases.target_config", return_value=TARGET), patch(
        "gentoo_agent.core.phases.public_key_for_target", return_value=PUBLIC_KEY
    ):
        parts = []
        for phase in phases.PHASES:
            parts.append(f"===== {phase} apply =====")
            parts.append(phases.command_for(phase, TARGET, install))
            parts.append(f"===== {phase} check =====")
            parts.append(phases.check_command_for(phase, TARGET, install))
        return "\n".join(parts) + "\n"
```

and remove the now unused `import sys`.

- [ ] **Step 11: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: all 10 tests PASS, including `test_generated_commands_match_golden_files` without `GOLDEN_UPDATE`.

- [ ] **Step 12: Smoke-test every entry point**

Run:

```bash
python tools/gentoo_agent.py --help
python -m gentoo_agent --help
python tools/init_config.py --help
python -c "import gentoo_agent.prepare"
```

Expected: the first three print usage text listing their subcommands/options; the last prints nothing and exits 0.

- [ ] **Step 13: Commit**

```bash
git add -A pyproject.toml gentoo_agent tools tests
git commit -m "Move installer into the gentoo_agent package with tools/ shims

Also return the install exit code from the CLI; a failed --execute
previously exited 0."
```

---

### Task 3: Typed errors with stable codes

**Files:**
- Create: `gentoo_agent/core/errors.py`
- Create: `tests/test_errors.py`
- Modify: `gentoo_agent/cli.py` (`main`)

- [ ] **Step 1: Write the failing test**

`tests/test_errors.py`:

```python
import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError


class AgentErrorTests(unittest.TestCase):
    def test_error_carries_code_message_and_details(self):
        error = AgentError(CONFIG_INVALID, "disk path is unsafe", {"disk": "/dev/x;reboot"})
        self.assertEqual(error.code, "CONFIG_INVALID")
        self.assertEqual(str(error), "disk path is unsafe")
        self.assertEqual(
            error.to_dict(),
            {"code": "CONFIG_INVALID", "message": "disk path is unsafe", "details": {"disk": "/dev/x;reboot"}},
        )

    def test_details_default_to_empty_dict(self):
        self.assertEqual(AgentError(CONFIG_INVALID, "x").details, {})

    def test_cli_reports_agent_error_with_code_and_exit_1(self):
        from gentoo_agent import cli

        def failing(_):
            raise AgentError(CONFIG_INVALID, "broken config")

        stderr = io.StringIO()
        with patch.object(cli, "cmd_status", failing), redirect_stderr(stderr):
            code = cli.main(["status"])
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "error [CONFIG_INVALID]: broken config\n")
```

`build_parser()` binds `cmd_status` at parse time through `set_defaults(func=cmd_status)`, so `patch.object` on the module attribute works because `main()` calls `build_parser()` after the patch is active.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_errors -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'gentoo_agent.core.errors'`.

- [ ] **Step 3: Create `gentoo_agent/core/errors.py`**

```python
"""Typed errors with stable codes; front ends turn them into text or MCP results."""

from __future__ import annotations


CONFIG_INVALID = "CONFIG_INVALID"
CONFIG_CONFLICT = "CONFIG_CONFLICT"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
PHASE_FAILED = "PHASE_FAILED"


class AgentError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}
```

- [ ] **Step 4: Catch `AgentError` in the CLI**

In `gentoo_agent/cli.py` add `from gentoo_agent.core.errors import AgentError` to the imports and replace `main` with:

```python
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
```

- [ ] **Step 5: Run the tests**

Run: `python -m unittest discover -s tests -v`
Expected: all 13 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add gentoo_agent/core/errors.py gentoo_agent/cli.py tests/test_errors.py
git commit -m "Add AgentError with stable error codes"
```

---

### Task 4: Pure phase generators and config loading

Generators stop reading `config/target.json` behind the caller's back and raise `AgentError` instead of `SystemExit`.

**Files:**
- Modify: `gentoo_agent/core/phases.py`
- Modify: `gentoo_agent/core/config.py`
- Create: `tests/test_phases.py`
- Modify: `tests/test_golden_commands.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_phases.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import config, phases
from gentoo_agent.core.errors import CONFIG_CONFLICT, CONFIG_INVALID, AgentError


ROOT = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "user": "root", "identity_file": "~/.ssh/id_test"}


def fixture(name: str) -> dict:
    return json.loads((ROOT / "tests" / "golden" / "configs" / f"{name}.json").read_text(encoding="utf-8"))


class PhaseGeneratorTests(unittest.TestCase):
    def test_phases_module_does_not_load_target_config_itself(self):
        self.assertFalse(hasattr(phases, "target_config"))

    def test_storage_command_uses_the_given_target(self):
        command = phases.storage_command(TARGET, fixture("bios_mbr_user_sudo"))
        self.assertIn("sfdisk", command)
        self.assertIn("disk=/dev/sda", command)

    def test_uefi_with_mbr_is_a_conflict(self):
        install = fixture("uefi_gpt_user_sudo")
        install["partition_table"] = "mbr"
        with self.assertRaises(AgentError) as caught:
            phases.validated_settings(TARGET, install)
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)

    def test_unsafe_disk_path_is_invalid(self):
        install = fixture("bios_mbr_user_sudo")
        install["disk"] = "/dev/sda;reboot"
        with self.assertRaises(AgentError) as caught:
            phases.validated_settings(TARGET, install)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_missing_identity_file_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            phases.public_key_for_target({"user": "root"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_unknown_phase_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            phases.command_for("reboot", TARGET, fixture("bios_mbr_user_sudo"))
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


class ConfigLoadingTests(unittest.TestCase):
    def test_missing_target_config_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "CONFIG", Path(tmp)):
            with self.assertRaises(AgentError) as caught:
                config.target_config()
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_phases -v`
Expected: all 7 FAIL or ERROR (for example `test_storage_command_uses_the_given_target` fails with `TypeError: storage_command() takes 1 positional argument but 2 were given`, and the error-code tests get `SystemExit` instead of `AgentError`).

- [ ] **Step 3: Convert `config.py` errors**

In `gentoo_agent/core/config.py` add `from gentoo_agent.core.errors import CONFIG_INVALID, AgentError` and replace `target_config` and `install_config` with:

```python
def target_config() -> dict:
    path = CONFIG / "target.json"
    if not path.exists():
        raise AgentError(CONFIG_INVALID, "Missing config/target.json; copy target.example.json first.", {"path": str(path)})
    return load_json(path)


def install_config() -> dict:
    target = target_config()
    configured_path = ROOT / target.get("install_config", "config/install.json")
    if not configured_path.exists():
        raise AgentError(CONFIG_INVALID, f"Missing installation configuration: {configured_path}", {"path": str(configured_path)})
    return load_json(configured_path)
```

- [ ] **Step 4: Update the imports of `phases.py`**

In `gentoo_agent/core/phases.py` replace:

```python
from gentoo_agent.core.config import install_config, load_json, now, save_json, target_config
```

with:

```python
from gentoo_agent.core.config import install_config, load_json, now, save_json
from gentoo_agent.core.errors import CONFIG_CONFLICT, CONFIG_INVALID, AgentError
```

`install_config` stays imported because `cmd_install` and `approve_execution` still live here until Task 6. `target_config` disappears from the module. `cmd_install` and `approve_execution` call `target_config()` and must import it locally until Task 6: in both functions, add `from gentoo_agent.core.config import target_config` as the first line of the function body.

- [ ] **Step 5: Replace `validated_settings` with the `AgentError` version**

```python
def validated_settings(target: dict, install: dict) -> tuple[str, list[dict]]:
    disk = install.get("disk", "")
    if not re.fullmatch(r"/dev/[A-Za-z0-9._+/-]+", disk):
        raise AgentError(CONFIG_INVALID, f"Unsafe disk path in configuration: {disk!r}", {"field": "disk"})
    layout = install.get("partition_layout")
    if not isinstance(layout, list) or not layout:
        raise AgentError(CONFIG_INVALID, "partition_layout is empty", {"field": "partition_layout"})
    for item in layout:
        device = item.get("device", "")
        if not re.fullmatch(r"/dev/[A-Za-z0-9._+/-]+", device):
            raise AgentError(CONFIG_INVALID, f"Unsafe partition path in configuration: {device!r}", {"field": "partition_layout"})
    if target.get("user") != "root":
        raise AgentError(CONFIG_INVALID, "The installation executor currently requires SSH user root.", {"field": "user"})
    if install.get("boot_mode") == "uefi" and install.get("partition_table") != "gpt":
        raise AgentError(CONFIG_CONFLICT, "UEFI requires GPT.", {"fields": ["boot_mode", "partition_table"]})
    if install.get("root_filesystem") != "ext4":
        raise AgentError(CONFIG_INVALID, "The executor currently supports ext4 root filesystems only.", {"field": "root_filesystem"})
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", str(install.get("hostname", ""))):
        raise AgentError(CONFIG_INVALID, "hostname must be a simple DNS-compatible hostname", {"field": "hostname"})
    if not re.fullmatch(r"[A-Za-z0-9._+/-]+", str(install.get("timezone", ""))):
        raise AgentError(CONFIG_INVALID, "timezone contains unsafe characters", {"field": "timezone"})
    create_user = install.get("create_user", True)
    set_root_password = install.get("set_root_password", True)
    install_user_ssh_key = install.get("install_user_ssh_key", True)
    configure_sudo = install.get("configure_sudo", True)
    allow_root_password_ssh = install.get("allow_root_password_ssh", False)
    generate_missing_passwords = install.get("generate_missing_passwords", True)
    for field, value in (
        ("create_user", create_user),
        ("set_root_password", set_root_password),
        ("install_user_ssh_key", install_user_ssh_key),
        ("configure_sudo", configure_sudo),
        ("allow_root_password_ssh", allow_root_password_ssh),
        ("generate_missing_passwords", generate_missing_passwords),
    ):
        if not isinstance(value, bool):
            raise AgentError(CONFIG_INVALID, f"{field} must be boolean", {"field": field})
    if install_user_ssh_key and not create_user:
        raise AgentError(CONFIG_INVALID, "install_user_ssh_key requires create_user=true", {"field": "install_user_ssh_key"})
    password_source = install.get("password_source", "environment")
    if password_source not in {"prompt", "environment", "generate"}:
        raise AgentError(CONFIG_INVALID, "password_source must be 'prompt', 'environment', or 'generate'", {"field": "password_source"})
    if password_source == "environment":
        for field in ("root_password_env", "user_password_env"):
            env_name = str(install.get(field, ""))
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", env_name):
                raise AgentError(CONFIG_INVALID, f"{field} must contain a valid uppercase environment-variable name", {"field": field})
    if not create_user and not set_root_password:
        raise AgentError(CONFIG_INVALID, "at least one installed password must be configured", {"fields": ["create_user", "set_root_password"]})
    if create_user:
        username = str(install.get("username", ""))
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) or username == "root":
            raise AgentError(CONFIG_INVALID, "username must be a non-root Linux account name", {"field": "username"})
        groups = install.get("user_groups", ["wheel"])
        if not isinstance(groups, list) or any(
            not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(group)) for group in groups
        ):
            raise AgentError(CONFIG_INVALID, "user_groups must contain simple Linux group names", {"field": "user_groups"})
        if configure_sudo and "wheel" not in [str(group) for group in groups]:
            raise AgentError(CONFIG_INVALID, "configure_sudo requires wheel in user_groups", {"field": "configure_sudo"})
        shell = str(install.get("user_shell", "/bin/bash"))
        if shell != "/bin/bash":
            raise AgentError(CONFIG_INVALID, "user_shell must be /bin/bash; other shells are not installed by this workflow", {"field": "user_shell"})
    return disk, layout
```

The check order and messages match the old function; the six boolean checks are folded into one loop in the same order.

- [ ] **Step 6: Replace `public_key_for_target`**

```python
def public_key_for_target(target: dict) -> str:
    identity = target.get("identity_file")
    if not identity:
        raise AgentError(CONFIG_INVALID, "target.identity_file is required for installed SSH access", {"field": "identity_file"})
    private_path = Path(identity).expanduser()
    candidates = [Path(str(private_path) + ".pub")]
    if private_path.suffix:
        candidates.append(private_path.with_suffix(".pub"))
    public_path = next((path for path in candidates if path.exists()), None)
    if public_path is None:
        raise AgentError(
            CONFIG_INVALID,
            f"Missing SSH public key next to {private_path}; expected {candidates[0]}",
            {"field": "identity_file"},
        )
    public_key = public_path.read_text(encoding="utf-8").strip()
    if not public_key or "\n" in public_key:
        raise AgentError(CONFIG_INVALID, f"SSH public key must contain exactly one line: {public_path}", {"field": "identity_file"})
    return public_key
```

- [ ] **Step 7: Pass `target` explicitly to the seven generators**

Change each signature and its `validated_settings(target_config(), install)` call to `validated_settings(target, install)`:

| Old | New |
|---|---|
| `def partition_script(install: dict) -> str:` | `def partition_script(target: dict, install: dict) -> str:` |
| `def storage_command(install: dict) -> str:` | `def storage_command(target: dict, install: dict) -> str:` |
| `def filesystem_command(install: dict) -> str:` | `def filesystem_command(target: dict, install: dict) -> str:` |
| `def bootloader_command(install: dict) -> str:` | `def bootloader_command(target: dict, install: dict) -> str:` |
| `def validation_command(install: dict) -> str:` | `def validation_command(target: dict, install: dict) -> str:` |
| `def storage_check_command(install: dict) -> str:` | `def storage_check_command(target: dict, install: dict) -> str:` |
| `def filesystem_check_command(install: dict) -> str:` | `def filesystem_check_command(target: dict, install: dict) -> str:` |

Inside `storage_command`, change `script = partition_script(install)` to `script = partition_script(target, install)`. `storage_command` calls `validated_settings` twice; change both calls.

Then replace both dispatchers:

```python
def check_command_for(phase: str, target: dict, install: dict) -> str:
    if phase == "preflight":
        return "\n".join(preflight_commands(target, install))
    if phase == "storage":
        return storage_check_command(target, install)
    if phase == "filesystems":
        return filesystem_check_command(target, install)
    if phase == "base-system":
        return base_system_check_command()
    if phase == "configuration":
        return configuration_check_command(target, install)
    if phase == "authentication":
        return authentication_check_command(target, install)
    if phase == "kernel":
        return kernel_check_command()
    if phase == "bootloader":
        return bootloader_check_command(install)
    if phase == "validation":
        return validation_command(target, install)
    raise AgentError(CONFIG_INVALID, f"Unknown installation phase: {phase}", {"phase": phase})


def command_for(phase: str, target: dict, install: dict) -> str:
    if phase == "preflight":
        return "\n".join(preflight_commands(target, install))
    if phase == "storage":
        return storage_command(target, install)
    if phase == "filesystems":
        return filesystem_command(target, install)
    if phase == "base-system":
        return base_system_command(install)
    if phase == "configuration":
        return configuration_command(target, install)
    if phase == "authentication":
        return authentication_command(target, install)
    if phase == "kernel":
        return kernel_command(install)
    if phase == "bootloader":
        return bootloader_command(target, install)
    if phase == "validation":
        return validation_command(target, install)
    raise AgentError(CONFIG_INVALID, f"Unknown installation phase: {phase}", {"phase": phase})
```

- [ ] **Step 8: Drop the obsolete patch from the golden test**

In `tests/test_golden_commands.py` replace the `with` statement inside `render` with:

```python
    with patch("gentoo_agent.core.phases.public_key_for_target", return_value=PUBLIC_KEY):
```

- [ ] **Step 9: Verify no `target_config` reference remains in generators**

Run: `grep -n "target_config" gentoo_agent/core/phases.py`
Expected: exactly four lines, all inside `cmd_install` and `approve_execution` (one local import and one call in each).

- [ ] **Step 10: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: all 20 tests PASS, golden files unchanged (`git status --short tests/golden` prints nothing).

- [ ] **Step 11: Commit**

```bash
git add gentoo_agent/core tests/test_phases.py tests/test_golden_commands.py
git commit -m "Make phase generators take target explicitly and raise AgentError"
```

---

### Task 5: State, inventory, Handbook, and validation in core

**Files:**
- Create: `gentoo_agent/core/state.py`, `gentoo_agent/core/inventory.py`, `gentoo_agent/core/handbook.py`, `gentoo_agent/core/validate.py`
- Modify: `gentoo_agent/core/phases.py` (move `validated_settings` out)
- Create: `tests/test_state_inventory.py`, `tests/test_handbook.py`, `tests/test_validate.py`
- Modify: `tests/test_phases.py`

- [ ] **Step 1: Write the failing state and inventory tests**

`tests/test_state_inventory.py`:

```python
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import inventory, state
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS


TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root"}


class StateTests(unittest.TestCase):
    def test_missing_state_has_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(state, "STATE_DIR", Path(tmp)):
            self.assertEqual(state.load_state(), {"schema_version": 1, "completed_phases": []})

    def test_save_state_stamps_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(state, "STATE_DIR", Path(tmp)):
            state.save_state({"status": "running"})
            loaded = state.load_state()
        self.assertEqual(loaded["status"], "running")
        self.assertIn("updated_at", loaded)


class InventoryTests(unittest.TestCase):
    def fake_ssh(self, target, label, command):
        if label == "errors":
            raise subprocess.TimeoutExpired(command, 45)
        return {"label": label, "command": command, "returncode": 0, "stdout": f"{label}-out", "stderr": ""}

    def test_collect_inventory_runs_every_allowlisted_command_and_saves(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(inventory, "INVENTORY_DIR", Path(tmp)), patch.object(
            inventory, "run_ssh_readonly", self.fake_ssh
        ):
            data, path = inventory.collect_inventory(TARGET, on_progress=seen.append)
            self.assertTrue(path.exists())
        self.assertEqual(seen, list(READ_ONLY_COMMANDS))
        self.assertEqual(len(data["results"]), len(READ_ONLY_COMMANDS))
        self.assertTrue(data["read_only"])
        self.assertEqual(data["target_address"], "root@192.0.2.10:22")
        errors = next(item for item in data["results"] if item["label"] == "errors")
        self.assertIn("timed out", errors["error"])

    def test_latest_inventory_returns_newest_or_none(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(inventory, "INVENTORY_DIR", Path(tmp)):
            self.assertIsNone(inventory.latest_inventory())
            with patch.object(inventory, "run_ssh_readonly", self.fake_ssh):
                data, _ = inventory.collect_inventory(TARGET)
            self.assertEqual(inventory.latest_inventory(), data)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_state_inventory -v`
Expected: ERROR, `ImportError: cannot import name 'inventory' from 'gentoo_agent.core'`.

- [ ] **Step 3: Create `gentoo_agent/core/state.py`**

```python
"""Resumable local workflow state in state/state.json."""

from __future__ import annotations

from pathlib import Path

from gentoo_agent.core.config import load_json, now, save_json
from gentoo_agent.core.paths import STATE_DIR


def state_path() -> Path:
    return STATE_DIR / "state.json"


def load_state() -> dict:
    path = state_path()
    return load_json(path) if path.exists() else {
        "schema_version": 1,
        "completed_phases": [],
    }


def save_state(state: dict) -> None:
    state["updated_at"] = now()
    save_json(state_path(), state)
```

- [ ] **Step 4: Create `gentoo_agent/core/inventory.py`**

```python
"""Read-only target inventory collected with the SSH allowlist."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Callable

from gentoo_agent.core.config import load_json, now, save_json
from gentoo_agent.core.paths import INVENTORY_DIR, ROOT
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS, run_ssh_readonly
from gentoo_agent.core.state import load_state, save_state


def collect_inventory(target: dict, on_progress: Callable[[str], None] | None = None) -> tuple[dict, Path]:
    results = []
    for label, command in READ_ONLY_COMMANDS.items():
        if on_progress is not None:
            on_progress(label)
        try:
            results.append(run_ssh_readonly(target, label, command))
        except subprocess.TimeoutExpired as exc:
            results.append({"label": label, "command": command, "error": str(exc)})
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data = {
        "target": target["name"],
        "target_address": f"{target['user']}@{target['host']}:{target.get('port', 22)}",
        "collected_at": now(),
        "read_only": True,
        "results": results,
    }
    path = INVENTORY_DIR / f"inventory-{stamp}.json"
    save_json(path, data)
    return data, path


def record_inventory(target: dict, path: Path) -> None:
    state = load_state()
    state.update({"target": target["name"], "last_inventory": str(path.relative_to(ROOT))})
    save_state(state)


def latest_inventory() -> dict | None:
    paths = sorted(INVENTORY_DIR.glob("inventory-*.json"), key=lambda item: item.stat().st_mtime)
    return load_json(paths[-1]) if paths else None
```

- [ ] **Step 5: Run the state and inventory tests**

Run: `python -m unittest tests.test_state_inventory -v`
Expected: 4 tests PASS.

- [ ] **Step 6: Write the failing Handbook test**

`tests/test_handbook.py`:

```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import handbook


class FakeResponse:
    headers = {"Content-Type": "text/html; charset=UTF-8"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"<html><head><title>Handbook:AMD64 - Gentoo Wiki</title></head></html>"


class HandbookTests(unittest.TestCase):
    def test_fetch_records_url_title_and_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_dir = root / "inventory"
            with patch.object(handbook, "ROOT", root), patch.object(handbook, "INVENTORY_DIR", inventory_dir), patch.object(
                handbook.urllib.request, "urlopen", return_value=FakeResponse()
            ):
                metadata = handbook.fetch_handbook({"handbook_url": "https://wiki.gentoo.org/wiki/Handbook:AMD64"})
            self.assertTrue((root / metadata["saved_html"]).exists())
        self.assertEqual(metadata["url"], "https://wiki.gentoo.org/wiki/Handbook:AMD64")
        self.assertEqual(metadata["title"], "Handbook:AMD64 - Gentoo Wiki")
        self.assertEqual(metadata["content_type"], "text/html; charset=UTF-8")
        self.assertIn("retrieved_at", metadata)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 7: Run it to verify it fails**

Run: `python -m unittest tests.test_handbook -v`
Expected: ERROR, `ImportError: cannot import name 'handbook'`.

- [ ] **Step 8: Create `gentoo_agent/core/handbook.py`**

The old `cmd_handbook` wrote the HTML before any directory existed; this version creates `inventory/` first.

```python
"""Handbook snapshot: the procedure source, recorded with URL and retrieval time."""

from __future__ import annotations

import datetime as dt
import html.parser
import urllib.request

from gentoo_agent.core.config import now, save_json
from gentoo_agent.core.paths import INVENTORY_DIR, ROOT


class TitleParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def fetch_handbook(target: dict) -> dict:
    url = target["handbook_url"]
    request = urllib.request.Request(url, headers={"User-Agent": "gentoo-agent/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    html_path = INVENTORY_DIR / f"handbook-{stamp}.html"
    html_path.write_bytes(raw)
    parser = TitleParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    metadata = {
        "url": url,
        "retrieved_at": now(),
        "content_type": content_type,
        "bytes": len(raw),
        "title": "".join(parser.title_parts).strip(),
        "saved_html": str(html_path.relative_to(ROOT)),
    }
    save_json(INVENTORY_DIR / f"handbook-{stamp}.json", metadata)
    return metadata
```

- [ ] **Step 9: Run the Handbook test**

Run: `python -m unittest tests.test_handbook -v`
Expected: PASS.

- [ ] **Step 10: Write the failing validation tests**

`tests/test_validate.py`:

```python
import json
import unittest
from pathlib import Path

from gentoo_agent.core.errors import CONFIG_CONFLICT, AgentError
from gentoo_agent.core.validate import validate_installation, validated_settings


ROOT = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "user": "root"}


def fixture(name: str) -> dict:
    data = json.loads((ROOT / "tests" / "golden" / "configs" / f"{name}.json").read_text(encoding="utf-8"))
    data.update({
        "profile": "gentoo-amd64-openrc",
        "swap_mb": 2048,
        "bootloader": "grub",
        "review_status": "pending_operator_review",
    })
    return data


def inventory_with(storage: str, boot_mode: str) -> dict:
    return {"results": [
        {"label": "storage", "stdout": storage},
        {"label": "boot_mode", "stdout": boot_mode},
    ]}


class ValidatedSettingsTests(unittest.TestCase):
    def test_valid_configuration_returns_disk_and_layout(self):
        disk, layout = validated_settings(TARGET, fixture("uefi_gpt_user_sudo"))
        self.assertEqual(disk, "/dev/nvme0n1")
        self.assertEqual(len(layout), 3)

    def test_uefi_with_mbr_is_a_conflict(self):
        install = fixture("uefi_gpt_user_sudo")
        install["partition_table"] = "mbr"
        with self.assertRaises(AgentError) as caught:
            validated_settings(TARGET, install)
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)


class ValidateInstallationTests(unittest.TestCase):
    def test_without_inventory_is_valid_with_warning(self):
        result = validate_installation(TARGET, fixture("bios_mbr_user_sudo"), {}, None)
        self.assertTrue(result["valid_for_execution"])
        self.assertIn("no inventory exists; run inspect before review", result["warnings"])

    def test_disk_missing_from_inventory_is_an_error(self):
        inventory = inventory_with("sdb 40G disk", "bios-or-unknown")
        result = validate_installation(TARGET, fixture("bios_mbr_user_sudo"), {}, inventory)
        self.assertFalse(result["valid_for_execution"])
        self.assertIn("configured disk /dev/sda was not found in the latest inventory", result["errors"])

    def test_uefi_plan_on_bios_live_boot_is_an_error(self):
        inventory = inventory_with("/dev/nvme0n1 512G disk", "bios-or-unknown")
        result = validate_installation(TARGET, fixture("uefi_gpt_user_sudo"), {}, inventory)
        self.assertTrue(any("conflicts with the latest live boot mode" in error for error in result["errors"]))

    def test_mismatched_execution_flags_are_an_error(self):
        result = validate_installation({**TARGET, "execution_enabled": True}, fixture("bios_mbr_user_sudo"), {}, None)
        self.assertIn("target, install, and plan execution_enabled flags must match", result["errors"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 11: Run it to verify it fails**

Run: `python -m unittest tests.test_validate -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'gentoo_agent.core.validate'`.

- [ ] **Step 12: Create `gentoo_agent/core/validate.py`**

Move `validated_settings` (the `AgentError` version from Task 4, Step 5) out of `gentoo_agent/core/phases.py` into this file unchanged, and add `validate_installation`, which is the body of the old `cmd_validate` returning its result instead of printing it:

```python
"""Configuration validation: the single gate before commands are generated."""

from __future__ import annotations

import re

from gentoo_agent.core.errors import CONFIG_CONFLICT, CONFIG_INVALID, AgentError


# validated_settings(target, install) goes here, moved verbatim from phases.py.


def validate_installation(target: dict, install: dict, plan: dict, inventory: dict | None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    required = [
        "profile", "boot_mode", "partition_table", "disk", "partition_layout",
        "root_filesystem", "swap_mb", "bootloader", "init_system", "hostname",
        "timezone", "network_method",
    ]
    for key in required:
        if key not in install:
            errors.append(f"missing install setting: {key}")

    if install.get("boot_mode") not in {"uefi", "bios"}:
        errors.append("boot_mode must be 'uefi' or 'bios'")
    if install.get("boot_mode") == "uefi" and install.get("partition_table") != "gpt":
        errors.append("UEFI installation requires a GPT partition table")
    if not isinstance(install.get("partition_layout"), list) or not install.get("partition_layout"):
        errors.append("partition_layout must contain the proposed partitions")
    if install.get("root_filesystem") != "ext4":
        warnings.append("this starter configuration has only been reviewed for ext4")
    if not isinstance(install.get("swap_mb"), int) or install.get("swap_mb", -1) < 0:
        errors.append("swap_mb must be a non-negative integer")
    create_user = install.get("create_user", True)
    set_root_password = install.get("set_root_password", True)
    if not isinstance(create_user, bool):
        errors.append("create_user must be boolean")
    if not isinstance(set_root_password, bool):
        errors.append("set_root_password must be boolean")
    install_user_ssh_key = install.get("install_user_ssh_key", True)
    if not isinstance(install_user_ssh_key, bool):
        errors.append("install_user_ssh_key must be boolean")
    if install_user_ssh_key and not create_user:
        errors.append("install_user_ssh_key requires create_user=true")
    configure_sudo = install.get("configure_sudo", True)
    if not isinstance(configure_sudo, bool):
        errors.append("configure_sudo must be boolean")
    if configure_sudo and not create_user:
        errors.append("configure_sudo requires create_user=true")
    if not isinstance(install.get("allow_root_password_ssh", False), bool):
        errors.append("allow_root_password_ssh must be boolean")
    password_source = install.get("password_source", "environment")
    if password_source not in {"prompt", "environment", "generate"}:
        errors.append("password_source must be 'prompt', 'environment', or 'generate'")
    if password_source == "environment":
        for field in ("root_password_env", "user_password_env"):
            env_name = str(install.get(field, ""))
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", env_name):
                errors.append(f"{field} must contain a valid uppercase environment-variable name")
    if not isinstance(install.get("generate_missing_passwords", True), bool):
        errors.append("generate_missing_passwords must be boolean")
    if create_user:
        username = str(install.get("username", ""))
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) or username == "root":
            errors.append("username must be a non-root Linux account name")
        groups = install.get("user_groups", ["wheel"])
        if not isinstance(groups, list) or any(
            not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(group)) for group in groups
        ):
            errors.append("user_groups must contain simple Linux group names")
        if configure_sudo and "wheel" not in [str(group) for group in groups]:
            errors.append("configure_sudo requires wheel in user_groups")
    if not create_user and not set_root_password:
        errors.append("at least one installed password must be configured")
    execution_flags = [
        target.get("execution_enabled"),
        install.get("execution_enabled"),
        plan.get("execution_enabled"),
    ]
    if len(set(execution_flags)) != 1:
        errors.append("target, install, and plan execution_enabled flags must match")
    if any(execution_flags) and install.get("review_status") != "approved_for_execution":
        errors.append("execution_enabled=true requires approved_for_execution review status")
    if install.get("disk_must_be_erased") and not (
        install.get("target_is_disposable") and install.get("disk_erase_authorized")
    ):
        errors.append("disk erasure requires explicit disposable-target authorization")
    if install.get("review_status") not in {
        "pending_operator_review", "reviewed_execution_disabled", "approved_for_execution"
    }:
        warnings.append("unrecognized review_status")

    if inventory is not None:
        results = {item.get("label"): item for item in inventory.get("results", [])}
        storage = results.get("storage", {}).get("stdout", "")
        boot_mode = results.get("boot_mode", {}).get("stdout", "").strip()
        disk = install.get("disk", "")
        if disk and disk not in storage:
            errors.append(f"configured disk {disk} was not found in the latest inventory")
        if install.get("boot_mode") == "uefi" and boot_mode != "uefi":
            errors.append(
                "desired UEFI install conflicts with the latest live boot mode "
                f"({boot_mode or 'unknown'}); boot the VM with EFI enabled and inspect again"
            )
        if install.get("boot_mode") == "bios" and boot_mode == "uefi":
            errors.append("desired BIOS install conflicts with the latest UEFI live boot")
    else:
        warnings.append("no inventory exists; run inspect before review")

    return {
        "valid_for_execution": not errors,
        "execution_enabled": all(execution_flags),
        "errors": errors,
        "warnings": warnings,
        "target": target.get("name"),
        "installation": install,
        "plan_review_status": plan.get("review_status"),
    }
```

Replace the comment line with the full `validated_settings` function. Then in `gentoo_agent/core/phases.py`:
- delete `validated_settings`,
- add `from gentoo_agent.core.validate import validated_settings`,
- change the errors import to `from gentoo_agent.core.errors import CONFIG_INVALID, AgentError` (`CONFIG_CONFLICT` is no longer used there).

In `tests/test_phases.py`, `phases.validated_settings` still resolves through the import, so the tests keep working.

- [ ] **Step 13: Switch the CLI commands to the core functions**

In `gentoo_agent/cli.py` replace `TitleParser`, `cmd_handbook`, `cmd_inspect`, and `cmd_validate` with:

```python
def cmd_handbook(_: argparse.Namespace) -> int:
    print(json.dumps(fetch_handbook(target_config()), indent=2))
    return 0


def cmd_inspect(_: argparse.Namespace) -> int:
    target = target_config()
    _, path = collect_inventory(target, on_progress=lambda label: print(f"Inspecting {label}...", file=sys.stderr))
    record_inventory(target, path)
    print(path)
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    result = validate_installation(target_config(), install_config(), load_plan(), latest_inventory())
    print(json.dumps(result, indent=2))
    return 2 if result["errors"] else 0
```

and add these imports:

```python
from gentoo_agent.core.config import load_plan
from gentoo_agent.core.handbook import fetch_handbook
from gentoo_agent.core.inventory import collect_inventory, latest_inventory, record_inventory
from gentoo_agent.core.validate import validate_installation
```

- [ ] **Step 14: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: all 31 tests PASS, `git status --short tests/golden` prints nothing.

- [ ] **Step 15: Commit**

```bash
git add gentoo_agent tests
git commit -m "Move state, inventory, Handbook, and validation into core

fetch_handbook now creates inventory/ before writing the snapshot."
```

---

### Task 6: Credentials, runner, and approval in core

**Files:**
- Create: `gentoo_agent/core/credentials.py`, `gentoo_agent/core/runner.py`, `gentoo_agent/core/approval.py`
- Modify: `gentoo_agent/core/ssh.py` (add `remote_shell`)
- Modify: `gentoo_agent/core/phases.py` (remove everything that is not a generator)
- Replace: `gentoo_agent/cli.py`
- Create: `tests/test_credentials.py`, `tests/test_runner.py`, `tests/test_approval.py`
- Modify: `tests/test_authentication.py`

- [ ] **Step 1: Write the failing credentials tests**

`tests/test_credentials.py` (the two environment tests move here from `test_authentication.py`):

```python
import unittest
from unittest.mock import patch

from gentoo_agent.core import credentials
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError


BASE = {
    "create_user": True,
    "username": "gentoo",
    "set_root_password": True,
    "root_password_env": "TEST_GENTOO_ROOT_PASSWORD",
    "user_password_env": "TEST_GENTOO_USER_PASSWORD",
}


class PasswordInputTests(unittest.TestCase):
    def test_environment_password_source_reads_configured_names(self):
        install = {**BASE, "password_source": "environment"}
        with patch.dict(
            credentials.os.environ,
            {"TEST_GENTOO_ROOT_PASSWORD": "root-secret", "TEST_GENTOO_USER_PASSWORD": "user-secret"},
            clear=False,
        ):
            records, generated = credentials.password_input(install)
        self.assertEqual(records, "root:root-secret\ngentoo:user-secret\n")
        self.assertEqual(generated, {})

    def test_missing_environment_passwords_are_generated(self):
        install = {**BASE, "password_source": "environment", "generate_missing_passwords": True}
        with patch.dict(credentials.os.environ, {}, clear=True), patch(
            "gentoo_agent.core.credentials.secrets.token_urlsafe",
            side_effect=["generated-root", "generated-user"],
        ):
            records, generated = credentials.password_input(install)
        self.assertEqual(records, "root:generated-root\ngentoo:generated-user\n")
        self.assertEqual(generated, {"root": "generated-root", "gentoo": "generated-user"})

    def test_missing_environment_password_without_generation_is_invalid(self):
        install = {**BASE, "password_source": "environment", "generate_missing_passwords": False}
        with patch.dict(credentials.os.environ, {}, clear=True):
            with self.assertRaises(AgentError) as caught:
                credentials.password_input(install)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(caught.exception.details, {"variable": "TEST_GENTOO_ROOT_PASSWORD"})

    def test_prompt_source_uses_the_callback(self):
        install = {**BASE, "password_source": "prompt"}
        asked = []

        def prompt(label):
            asked.append(label)
            return f"{label}-typed"

        records, generated = credentials.password_input(install, prompt)
        self.assertEqual(asked, ["root", "gentoo"])
        self.assertEqual(records, "root:root-typed\ngentoo:gentoo-typed\n")
        self.assertEqual(generated, {})

    def test_prompt_source_without_callback_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            credentials.password_input({**BASE, "password_source": "prompt"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_no_accounts_means_no_records(self):
        records, generated = credentials.password_input({"create_user": False, "set_root_password": False})
        self.assertIsNone(records)
        self.assertEqual(generated, {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_credentials -v`
Expected: ERROR, `ImportError: cannot import name 'credentials'`.

- [ ] **Step 3: Create `gentoo_agent/core/credentials.py`**

```python
"""chpasswd records for the authentication phase.

Passwords never appear in generated commands, logs, or configuration. The
runner sends these records to the target over SSH stdin.
"""

from __future__ import annotations

import os
import secrets
from typing import Callable

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError


PasswordPrompt = Callable[[str], str]


def _checked(value: str, label: str, source: str) -> str:
    if not value or "\r" in value or "\n" in value:
        raise AgentError(CONFIG_INVALID, f"Password from {source} is empty or contains a newline", {"label": label})
    return value


def password_input(install: dict, prompt: PasswordPrompt | None = None) -> tuple[str | None, dict[str, str]]:
    password_source = install.get("password_source", "environment")
    generated: dict[str, str] = {}

    def obtain(label: str, env_field: str) -> str:
        if password_source == "environment":
            env_name = str(install.get(env_field, ""))
            value = os.environ.get(env_name)
            if value is None:
                if not install.get("generate_missing_passwords", True):
                    raise AgentError(
                        CONFIG_INVALID,
                        f"Missing required password environment variable: {env_name}",
                        {"variable": env_name},
                    )
                value = secrets.token_urlsafe(24)
                generated[label] = value
            return _checked(value, label, env_name)
        if password_source == "generate":
            value = secrets.token_urlsafe(24)
            generated[label] = value
            return value
        if prompt is None:
            raise AgentError(CONFIG_INVALID, "password_source 'prompt' needs an interactive front end", {"label": label})
        return _checked(prompt(label), label, "prompt")

    records: list[str] = []
    if install.get("set_root_password", True):
        records.append(f"root:{obtain('root', 'root_password_env')}")
    if install.get("create_user", True):
        username = install.get("username", "gentoo")
        records.append(f"{username}:{obtain(str(username), 'user_password_env')}")
    return ("\n".join(records) + "\n" if records else None), generated
```

- [ ] **Step 4: Run the credentials tests**

Run: `python -m unittest tests.test_credentials -v`
Expected: 6 tests PASS.

- [ ] **Step 5: Write the failing runner tests**

`tests/test_runner.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import runner, state
from gentoo_agent.core.errors import APPROVAL_REQUIRED, CONFIG_INVALID, AgentError
from gentoo_agent.core.phases import PHASES


ROOT = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root", "identity_file": "~/.ssh/id_test"}


def fixture() -> dict:
    return json.loads((ROOT / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))


def approved(install: dict) -> tuple[dict, dict, dict]:
    target = {**TARGET, "execution_enabled": True, "target_is_disposable": True, "disk_erase_authorized": True}
    install = {**install, "execution_enabled": True, "review_status": "approved_for_execution"}
    plan = {"execution_enabled": True}
    return target, install, plan


class SelectPhasesTests(unittest.TestCase):
    def test_default_is_every_phase(self):
        self.assertEqual(runner.select_phases(None), PHASES)

    def test_start_phase_slices_the_list(self):
        self.assertEqual(runner.select_phases("kernel"), ["kernel", "bootloader", "validation"])

    def test_unknown_start_phase_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            runner.select_phases("reboot")
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


class ExecutionGateTests(unittest.TestCase):
    def test_wrong_phrase_is_refused(self):
        target, install, plan = approved(fixture())
        with self.assertRaises(AgentError) as caught:
            runner.execution_gate(target, install, plan, "INSTALL gentoo ON /dev/sdb")
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_unapproved_review_is_refused(self):
        target, install, plan = approved(fixture())
        install["review_status"] = "pending_operator_review"
        with self.assertRaises(AgentError) as caught:
            runner.execution_gate(target, install, plan, "INSTALL gentoo ON /dev/sda")
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_disabled_plan_is_refused(self):
        target, install, plan = approved(fixture())
        plan["execution_enabled"] = False
        with self.assertRaises(AgentError):
            runner.execution_gate(target, install, plan, "INSTALL gentoo ON /dev/sda")

    def test_erase_without_authorization_is_refused(self):
        target, install, plan = approved(fixture())
        install["disk_must_be_erased"] = True
        with self.assertRaises(AgentError):
            runner.execution_gate(target, install, plan, "INSTALL gentoo ON /dev/sda")

    def test_fully_approved_passes(self):
        target, install, plan = approved(fixture())
        install.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True})
        runner.execution_gate(target, install, plan, "INSTALL gentoo ON /dev/sda")


class DryRunTests(unittest.TestCase):
    def test_render_lists_header_and_each_phase(self):
        with patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA test"):
            text = runner.render_dry_run(TARGET, fixture(), ["preflight", "storage"])
        self.assertTrue(text.startswith("Target: root@192.0.2.10:22\nDisk:   /dev/sda (bios/mbr)\n"))
        self.assertIn("\n\n===== preflight =====\n", text)
        self.assertIn("\n\n===== storage =====\n", text)
        self.assertNotIn("===== filesystems =====", text)


class ExecuteTests(unittest.TestCase):
    def run_execute(self, results_by_label: dict[str, int], phases: list[str]) -> tuple[dict, dict, list[str]]:
        sent = []

        def fake_remote_shell(target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
            sent.append(label)
            return {"label": label, "command": command, "returncode": results_by_label.get(label, 0),
                    "stdout": "", "stderr": f"{label}-stderr", "finished_at": "now"}

        with tempfile.TemporaryDirectory() as tmp, patch.object(state, "STATE_DIR", Path(tmp) / "state"), patch.object(
            runner, "LOG_DIR", Path(tmp) / "logs"
        ), patch.object(runner, "remote_shell", fake_remote_shell):
            result = runner.execute(TARGET, fixture(), phases)
            saved_state = state.load_state()
        return result, saved_state, sent

    def test_completed_checks_skip_apply(self):
        result, saved, sent = self.run_execute({}, ["preflight", "storage"])
        self.assertEqual(result["status"], "installed_pending_reboot")
        self.assertEqual(sent, ["preflight-check", "storage-check"])
        self.assertEqual(saved["completed_phases"], ["preflight", "storage"])

    def test_failed_apply_stops_and_records_state(self):
        result, saved, sent = self.run_execute({"storage-check": 1, "storage": 3}, ["preflight", "storage", "filesystems"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_phase"], "storage")
        self.assertEqual(result["returncode"], 3)
        self.assertEqual(result["stderr"], "storage-stderr")
        self.assertEqual(sent, ["preflight-check", "storage-check", "storage"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["last_error"], "storage apply returned 3")

    def test_failed_verify_stops(self):
        result, saved, _ = self.run_execute({"storage-check": 1, "storage-verify": 1}, ["storage"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(saved["last_error"], "storage verification returned 1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6: Run it to verify it fails**

Run: `python -m unittest tests.test_runner -v`
Expected: ERROR, `ImportError: cannot import name 'runner'`.

- [ ] **Step 7: Move `remote_shell` into `ssh.py` with an output callback**

Append to `gentoo_agent/core/ssh.py` (add `import queue`, `import threading`, `import time`, `from typing import Callable`, and `from gentoo_agent.core.config import now` to its imports). The only change from the original is `on_output` replacing the direct `print`:

```python
def remote_shell(
    target: dict,
    label: str,
    command: str,
    timeout: int = 3600,
    stream: bool = False,
    input_data: str | None = None,
    on_output: Callable[[str], None] | None = None,
) -> dict:
    """Run one generated shell program and return an auditable result."""
    remote_command = "sh -c " + shlex.quote(command)
    if stream:
        process = subprocess.Popen(
            ssh_base(target) + ["--", remote_command],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if input_data is not None else None,
            text=True,
            bufsize=1,
        )
        if input_data is not None:
            assert process.stdin is not None
            process.stdin.write(input_data)
            process.stdin.close()
        output: list[str] = []
        lines: queue.Queue[str] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            process.stdout.close()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            while reader.is_alive() or not lines.empty():
                if time.monotonic() - started > timeout:
                    process.kill()
                    raise subprocess.TimeoutExpired(remote_command, timeout)
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                output.append(line)
                if on_output is not None:
                    on_output(line)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        reader.join(timeout=1)
        returncode = process.wait(timeout=5)
        return {
            "label": label,
            "command": command,
            "returncode": returncode,
            "stdout": "".join(output),
            "stderr": "",
            "finished_at": now(),
        }
    completed = subprocess.run(
        ssh_base(target) + ["--", remote_command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        input=input_data,
        timeout=timeout,
        check=False,
    )
    return {
        "label": label,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "finished_at": now(),
    }
```

- [ ] **Step 8: Create `gentoo_agent/core/runner.py`**

Behavior matches the old `execute`, `execution_gate`, `dry_run`, and `cmd_install`, with two differences: output goes through callbacks, and a failed run also returns any passwords generated before the failure (previously they were lost even though the target already used them).

```python
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
```

- [ ] **Step 9: Run the runner tests**

Run: `python -m unittest tests.test_runner -v`
Expected: 12 tests PASS.

- [ ] **Step 10: Write the failing approval test**

`tests/test_approval.py`:

```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import approval
from gentoo_agent.core.config import load_json


class ApprovalTests(unittest.TestCase):
    def test_phrase_names_target_and_disk(self):
        self.assertEqual(
            approval.approval_phrase({"name": "gentoo"}, {"disk": "/dev/sda"}),
            "APPROVE INSTALL gentoo ON /dev/sda",
        )

    def test_enable_execution_sets_all_three_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(approval, "CONFIG", Path(tmp)):
            approval.enable_execution({"name": "gentoo"}, {"disk": "/dev/sda"}, {"phases": []})
            target = load_json(Path(tmp) / "target.json")
            install = load_json(Path(tmp) / "install.json")
            plan = load_json(Path(tmp) / "plan.json")
        self.assertTrue(target["execution_enabled"])
        self.assertTrue(install["execution_enabled"])
        self.assertEqual(install["review_status"], "approved_for_execution")
        self.assertTrue(plan["execution_enabled"])
        self.assertEqual(plan["review_status"], "approved_for_execution")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 11: Run it to verify it fails**

Run: `python -m unittest tests.test_approval -v`
Expected: ERROR, `ImportError: cannot import name 'approval'`.

- [ ] **Step 12: Create `gentoo_agent/core/approval.py`**

```python
"""Operator approval of the reviewed configuration (approve-install).

Plan 2 replaces this with plan_hash-bound approval records.
"""

from __future__ import annotations

from gentoo_agent.core.config import save_json
from gentoo_agent.core.paths import CONFIG


def approval_phrase(target: dict, install: dict) -> str:
    return f"APPROVE INSTALL {target['name']} ON {install['disk']}"


def enable_execution(target: dict, install: dict, plan: dict) -> None:
    install["review_status"] = "approved_for_execution"
    install["execution_enabled"] = True
    target["execution_enabled"] = True
    plan["execution_enabled"] = True
    plan["review_status"] = "approved_for_execution"
    save_json(CONFIG / "install.json", install)
    save_json(CONFIG / "target.json", target)
    save_json(CONFIG / "plan.json", plan)
```

- [ ] **Step 13: Strip `phases.py` down to generators**

In `gentoo_agent/core/phases.py` delete these functions: `remote_shell`, `load_plan`, `state_path`, `load_state`, `save_state`, `prompt_authentication_input`, `dry_run`, `execution_gate`, `execute`, `cmd_install`, `approve_execution`.

Replace the module docstring and the whole import block (everything above `MOUNT_ROOT = "/mnt/gentoo"`) with:

```python
"""Per-phase shell program generators: apply programs and read-only checks.

Programs are built only from validated configuration values and quoted with
shlex; there is no free-form remote command input. The runner executes them.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.validate import validated_settings
```

Run: `grep -nE "print\(|input\(|SystemExit|getpass|target_config|install_config" gentoo_agent/core/phases.py`
Expected: no output.

- [ ] **Step 14: Update `tests/test_authentication.py`**

Delete `test_environment_password_source_reads_configured_names` and `test_missing_environment_passwords_are_generated` (now in `tests/test_credentials.py`). Keep `from unittest.mock import patch`; it is still used for `public_key_for_target`.

- [ ] **Step 15: Replace `gentoo_agent/cli.py` with the version built on core**

```python
"""Supervised Gentoo-over-SSH agent workflow tools."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from gentoo_agent.core.approval import approval_phrase, enable_execution
from gentoo_agent.core.config import install_config, load_plan, now, save_json, target_config
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.handbook import fetch_handbook
from gentoo_agent.core.inventory import collect_inventory, latest_inventory, record_inventory
from gentoo_agent.core.paths import APPROVAL_DIR
from gentoo_agent.core.phases import PHASES
from gentoo_agent.core.runner import execute, execution_gate, install_phrase, render_dry_run, select_phases
from gentoo_agent.core.state import load_state, state_path
from gentoo_agent.core.validate import validate_installation, validated_settings


def ask_password(label: str) -> str:
    first = getpass.getpass(f"New Gentoo {label} password: ")
    second = getpass.getpass(f"Repeat Gentoo {label} password: ")
    if not first or first != second:
        raise AgentError(CONFIG_INVALID, f"Passwords for {label} were empty or did not match.")
    return first


def print_credentials(credentials: dict[str, str]) -> None:
    if not credentials:
        return
    print("Generated credentials (save them now; they are not stored by the agent):")
    for username, password in credentials.items():
        print(f"  {username}: {password}")


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


def cmd_approve(args: argparse.Namespace) -> int:
    if args.confirm != "I_CONFIRM_TARGET_AND_PHASE":
        raise SystemExit("Refusing approval: use the exact confirmation phrase.")
    target = target_config()
    approval = {
        "target": target["name"],
        "phase": args.phase,
        "disk": args.disk,
        "approved_at": now(),
        "confirmation": args.confirm,
        "note": args.note,
    }
    path = APPROVAL_DIR / f"{target['name']}-{args.phase}.json"
    save_json(path, approval)
    print(path)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    target = target_config()
    install = install_config()
    plan = load_plan()
    validated_settings(target, install)
    if args.execute and args.dry_run:
        raise AgentError(CONFIG_INVALID, "Choose either --execute or --dry-run, not both.")
    if args.from_phase is None:
        print("Starting from the beginning; completed phases will be checked and skipped.")
    phases = select_phases(args.from_phase)
    if not args.execute:
        print(render_dry_run(target, install, phases))
        return 0
    execution_gate(target, install, plan, args.confirm)
    result = execute(
        target,
        install,
        phases,
        prompt=ask_password,
        on_event=lambda message: print(message, flush=True),
        on_output=lambda line: print(f"    {line}", end="", flush=True),
    )
    if result["status"] != "installed_pending_reboot":
        print(result["stderr"], file=sys.stderr, end="")
        print_credentials(result["generated_credentials"])
        return result["returncode"]
    print("Installation phases completed. The target was not rebooted.")
    print_credentials(result["generated_credentials"])
    print("Review the target through the console before rebooting it.")
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
    args.dry_run = True
    args.confirm = ""
    return cmd_install(args)


def cmd_approve_install(_: argparse.Namespace) -> int:
    target = target_config()
    install = install_config()
    plan = load_plan()
    expected = approval_phrase(target, install)
    print("Review target.json, install.json, the latest inventory, and the dry-run output first.")
    if input(f"Type {expected}: ").strip() != expected:
        raise SystemExit("Approval not recorded.")
    enable_execution(target, install, plan)
    print("Execution enabled in all three local configuration files.")
    print(f"Start with: python tools/gentoo_agent.py install --execute --confirm {install_phrase(target, install)!r}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("handbook", help="Fetch and record the configured Handbook URL").set_defaults(func=cmd_handbook)
    sub.add_parser("inspect", help="Run the allowlisted read-only SSH inventory").set_defaults(func=cmd_inspect)
    sub.add_parser("status", help="Print current local workflow state").set_defaults(func=cmd_status)
    sub.add_parser("validate", help="Validate installation settings without touching the target").set_defaults(func=cmd_validate)
    approve = sub.add_parser("approve", help="Create an explicit approval record")
    approve.add_argument("phase")
    approve.add_argument("--disk", required=True)
    approve.add_argument("--note", required=True)
    approve.add_argument("--confirm", required=True)
    approve.set_defaults(func=cmd_approve)
    phase = sub.add_parser("phase", help="Run an implemented phase")
    phase.add_argument("phase")
    phase.set_defaults(func=cmd_phase)
    install = sub.add_parser("install", help="Print or execute the guarded installation phases")
    install.add_argument("--execute", action="store_true", help="Execute after explicit approval; dry-run is the default")
    install.add_argument("--dry-run", action="store_true", help="Print generated phases without connecting (default)")
    install.add_argument("--confirm", default="", help="Exact confirmation phrase printed by approve-install")
    install.add_argument("--from-phase", choices=PHASES, default=None,
                         help="Explicit restart point; omitted means resume from the first incomplete phase")
    install.set_defaults(func=cmd_install)
    approve_install = sub.add_parser("approve-install", help="Approve the reviewed installation for execution")
    approve_install.set_defaults(func=cmd_approve_install)
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

- [ ] **Step 16: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: all 49 tests PASS, `git status --short tests/golden` prints nothing.

- [ ] **Step 17: Check the dry run end to end with a temporary home**

Run:

```bash
tmp=$(mktemp -d) && mkdir -p "$tmp/config" && \
cp config/plan.json "$tmp/config/plan.json" && \
cp tests/golden/configs/bios_mbr_user_sudo.json "$tmp/config/install.json" && \
ssh-keygen -q -t ed25519 -N "" -f "$tmp/id_test" && \
printf '{"name":"gentoo","host":"192.0.2.10","port":22,"user":"root","identity_file":"%s"}\n' "$tmp/id_test" > "$tmp/config/target.json" && \
GENTOO_AGENT_HOME="$tmp" python -m gentoo_agent install --dry-run | head -5; \
GENTOO_AGENT_HOME="$tmp" python -m gentoo_agent install --dry-run --from-phase reboot; echo "exit=$?"; \
rm -rf "$tmp"
```

Expected: the first command prints `Starting from the beginning; ...`, then `Target: root@192.0.2.10:22`, `Disk:   /dev/sda (bios/mbr)`, `Mode:   DRY RUN - no SSH connection and no target changes`, an empty line. The second prints an argparse `invalid choice: 'reboot'` error and `exit=2`.

- [ ] **Step 18: Commit**

```bash
git add gentoo_agent tests
git commit -m "Move credentials, runner, and approval into core; CLI on top

A failed run now also reports passwords generated before the failure;
previously they were set on the target but never shown."
```

---

### Task 7: Guard core purity

**Files:**
- Create: `tests/test_core_purity.py`

- [ ] **Step 1: Write the guard test**

`tests/test_core_purity.py`:

```python
"""core/ must stay usable from the MCP server: no printing, prompting, or exiting."""

import ast
import unittest
from pathlib import Path


CORE = Path(__file__).resolve().parents[1] / "gentoo_agent" / "core"
FORBIDDEN_CALLS = {"print", "input", "exit", "quit"}
FORBIDDEN_NAMES = {"SystemExit"}
FORBIDDEN_MODULES = {"getpass", "argparse"}


def violations_in(path: Path) -> list[str]:
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            found.append(f"{path.name}:{line} calls {node.func.id}()")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{path.name}:{line} uses {node.id}")
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "exit"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        ):
            found.append(f"{path.name}:{line} calls sys.exit")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES:
                    found.append(f"{path.name}:{line} imports {alias.name}")
        if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_MODULES:
            found.append(f"{path.name}:{line} imports from {node.module}")
    return found


class CorePurityTests(unittest.TestCase):
    def test_core_modules_do_not_print_prompt_or_exit(self):
        violations = []
        for path in sorted(CORE.glob("*.py")):
            violations += violations_in(path)
        self.assertEqual(violations, [])

    def test_guard_detects_a_violation(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.py"
            sample.write_text("import getpass\nprint('x')\nraise SystemExit(1)\n", encoding="utf-8")
            self.assertEqual(len(violations_in(sample)), 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it**

Run: `python -m unittest tests.test_core_purity -v`
Expected: 2 tests PASS. If the first fails, the message lists `file:line` of each violation; move that code to `cli.py` or `prepare.py` and rerun.

- [ ] **Step 3: Commit**

```bash
git add tests/test_core_purity.py
git commit -m "Guard core against printing, prompting, and exiting"
```

---

### Task 8: `prepare.py` reuses core

`gentoo_agent/prepare.py` duplicates inventory collection, the Handbook snapshot, and state writing, and runs validation through the `tools/` script path.

**Files:**
- Modify: `gentoo_agent/prepare.py`
- Create: `tests/test_prepare.py`

- [ ] **Step 1: Write the failing test**

`tests/test_prepare.py`:

```python
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gentoo_agent import prepare


class PrepareInventoryTests(unittest.TestCase):
    def test_uses_core_inventory_and_warns_on_nonzero_results(self):
        inventory = {"results": [
            {"label": "identity", "returncode": 0},
            {"label": "storage", "returncode": 2},
            {"label": "errors", "error": "timed out"},
        ]}

        def fake_collect(target, on_progress=None):
            on_progress("identity")
            return inventory, Path("/tmp/inventory.json")

        output = io.StringIO()
        with patch.object(prepare, "collect_inventory", fake_collect), redirect_stdout(output):
            result, path = prepare.collect_inventory_interactive({"name": "gentoo"})
        self.assertIs(result, inventory)
        self.assertEqual(path, Path("/tmp/inventory.json"))
        self.assertIn("  Read-only check: identity", output.getvalue())
        self.assertIn("    warning: storage returned 2", output.getvalue())
        self.assertNotIn("identity returned", output.getvalue())

    def test_missing_ssh_client_is_reported(self):
        def fake_collect(target, on_progress=None):
            raise FileNotFoundError("ssh")

        with patch.object(prepare, "collect_inventory", fake_collect):
            with self.assertRaises(SystemExit) as caught:
                prepare.collect_inventory_interactive({"name": "gentoo"})
        self.assertEqual(str(caught.exception), "OpenSSH client was not found. Install ssh and retry.")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_prepare -v`
Expected: ERROR, `AttributeError: module 'gentoo_agent.prepare' has no attribute 'collect_inventory_interactive'`.

- [ ] **Step 3: Replace the imports of `gentoo_agent/prepare.py`**

Replace everything between the module docstring and `def partition_device` with:

```python
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys

from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.handbook import fetch_handbook
from gentoo_agent.core.inventory import collect_inventory
from gentoo_agent.core.paths import CONFIG, ROOT
from gentoo_agent.core.state import load_state, save_state
```

- [ ] **Step 4: Replace the local `collect_inventory` and `fetch_handbook`**

Delete the local `collect_inventory` and `fetch_handbook` functions and add:

```python
def collect_inventory_interactive(target: dict) -> tuple[dict, object]:
    try:
        inventory, path = collect_inventory(target, on_progress=lambda label: print(f"  Read-only check: {label}"))
    except FileNotFoundError:
        raise SystemExit("OpenSSH client was not found. Install ssh and retry.")
    for result in inventory.get("results", []):
        if result.get("returncode") not in (0, None):
            print(f"    warning: {result['label']} returned {result.get('returncode')}")
    return inventory, path


def snapshot_handbook(target: dict) -> str | None:
    try:
        metadata = fetch_handbook(target)
    except Exception as exc:
        print(f"Handbook snapshot warning: {exc}")
        print(f"The agent must still use the configured Handbook URL: {target['handbook_url']}")
        return None
    print(f"Saved Handbook snapshot: {ROOT / metadata['saved_html']}")
    return metadata["saved_html"]
```

- [ ] **Step 5: Update `main()` in `gentoo_agent/prepare.py`**

Replace:

```python
    inventory, inventory_path = collect_inventory(target)
    handbook_path = fetch_handbook(target)
```

with:

```python
    inventory, inventory_path = collect_inventory_interactive(target)
    handbook_path = snapshot_handbook(target)
```

Replace:

```python
    state_path = ROOT / "state" / "state.json"
    state = load_json(state_path) if state_path.exists() else {}
    state.update({
        "target": target["name"],
        "status": "review_required",
        "current_phase": "discovery",
        "last_inventory": str(inventory_path.relative_to(ROOT)),
        "last_handbook_fetch": handbook_path,
        "updated_at": now(),
    })
    save_json(state_path, state)
```

with:

```python
    state = load_state()
    state.update({
        "target": target["name"],
        "status": "review_required",
        "current_phase": "discovery",
        "last_inventory": str(inventory_path.relative_to(ROOT)),
        "last_handbook_fetch": handbook_path,
    })
    save_state(state)
```

Replace:

```python
    subprocess.run([sys.executable, str(ROOT / "tools" / "gentoo_agent.py"), "validate"], cwd=ROOT, check=False)
```

with:

```python
    subprocess.run([sys.executable, "-m", "gentoo_agent", "validate"], cwd=ROOT, check=False)
```

Replace:

```python
        state["status"] = "reviewed_execution_disabled"
        save_json(state_path, state)
```

with:

```python
        state["status"] = "reviewed_execution_disabled"
        save_state(state)
```

- [ ] **Step 6: Check no stale names remain**

Run: `grep -nE "state_path|urllib|INVENTORY_DIR|READ_ONLY_COMMANDS|run_ssh_readonly|now\(" gentoo_agent/prepare.py`
Expected: no output.

- [ ] **Step 7: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: all 53 tests PASS.

- [ ] **Step 8: Commit**

```bash
git add gentoo_agent/prepare.py tests/test_prepare.py
git commit -m "Reuse core inventory, Handbook, and state in prepare"
```

---

### Task 9: Documentation

**Files:**
- Modify: `README.md`, `AGENTS.md`, `docs/CONFIGURATION.md`, `CLAUDE.md`

- [ ] **Step 1: README requirements**

In `README.md` replace `- Python 3.10 or newer on the operator machine.` with:

```markdown
- Python 3.12 or newer on the operator machine.
- Optional: `pip install -e .` provides the `gentoo-agent` command; every
  `python tools/...` command below keeps working without installation.
```

- [ ] **Step 2: AGENTS.md implementation status**

In `AGENTS.md` replace `approval records, and guarded executors in \`tools/install_phases.py\`. The` with:

```markdown
approval records, and guarded executors in `gentoo_agent/core/` (phase
generators in `phases.py`, the check/apply/verify loop in `runner.py`). The
```

- [ ] **Step 3: CONFIGURATION.md file table**

In `docs/CONFIGURATION.md` replace the row:

```markdown
| `tools/gentoo_agent.py` | Yes | Read-only inspection and validation | Commit |
```

with:

```markdown
| `gentoo_agent/` | Yes | Installer package: `core/` logic, `cli.py`, `prepare.py` | Commit |
| `tools/*.py` | Yes | Compatibility entry points for the package | Commit |
```

- [ ] **Step 4: CLAUDE.md commands and architecture**

In `CLAUDE.md` replace the line `Python 3.10+ standard library only; there is no build step, dependency file, or linter configuration.` with:

```markdown
Python 3.12+ standard library only (`pyproject.toml`, no runtime dependencies yet); no linter configuration. `pip install -e .` adds a `gentoo-agent` command; `python -m gentoo_agent` and `python tools/gentoo_agent.py` are equivalent.
```

Replace the line `# Tests (unittest, no network or SSH; install_phases is imported directly)` with:

```markdown
# Tests (unittest, no network or SSH)
```

Add after the single-test example line:

```markdown
GOLDEN_UPDATE=1 python -m unittest tests.test_golden_commands   # only after an intentional command change
```

Replace the whole `## Architecture` section, up to but not including `## Configuration files`, with:

```markdown
## Architecture

`gentoo_agent/core/` holds all logic and must never print, prompt, or exit (`tests/test_core_purity.py` enforces this); it returns data and raises `AgentError(code, message, details)` from `core/errors.py`. Front ends are `gentoo_agent/cli.py` (argparse subcommands), `gentoo_agent/prepare.py` (interactive beginner flow), and `gentoo_agent/init_config.py`. `tools/*.py` are shims that put the repo root on `sys.path` and call those front ends. `core/paths.py` derives every directory from the repo root, overridable with `GENTOO_AGENT_HOME` (useful for tests and dry runs against a temporary config).

- `core/phases.py`: `PHASES` order (preflight, storage, filesystems, base-system, configuration, authentication, kernel, bootloader, validation) and, per phase, an apply generator (`command_for`) and a read-only check generator (`check_command_for`). Generators take `target` and `install` explicitly and quote every value with `shlex.quote`. `config/plan.json` mirrors the phase list plus the read-only `discovery` phase; keep them in sync. `tests/test_golden_commands.py` pins the generated programs for four configurations in `tests/golden/`.
- `core/validate.py`: `validated_settings()` is the single gate for values that reach remote commands; `validate_installation()` is the broader review used by `validate`.
- `core/runner.py`: `execute()` runs check (exit 0 means done, skip), apply, then the same check as verify, each as one `sh -c` program via `core/ssh.remote_shell()`. Output goes through `on_event`/`on_output` callbacks; progress is saved in `state/state.json` (`core/state.py`) so reruns resume; logs go to `logs/install-<target>-<stamp>.json`. The target is never rebooted. `execution_gate()` requires the exact phrase `INSTALL <target.name> ON <disk>`, `review_status == "approved_for_execution"`, `execution_enabled: true` in `target.json`, `install.json`, and `plan.json`, and both disposable and erase flags when `disk_must_be_erased`. `core/approval.enable_execution()` (behind `approve-install`) is the only code that sets these flags, and it rewrites the committed `config/plan.json`.
- `core/credentials.py`: passwords travel only over SSH stdin as `chpasswd` records; they never appear in generated commands, logs, or config. The `prompt` source takes a callback from the front end.
- `core/ssh.py` is the only module that contacts the target: `READ_ONLY_COMMANDS` allowlist for `inspect`, `remote_shell` for generated programs, strict host-key checking.

`roles/*.md` hold the Supervisor, Inspector, Executor, Verifier, and Recovery prompts used when coordinating the workflow as an agent. The MCP server design that builds on this package is in `docs/superpowers/specs/2026-10-15-mcp-server-design.md`.
```

- [ ] **Step 5: Run all tests one final time**

Run: `python -m unittest discover -s tests -v`
Expected: all 53 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add README.md AGENTS.md docs/CONFIGURATION.md CLAUDE.md
git commit -m "Document the gentoo_agent package layout and Python 3.12 requirement"
```
