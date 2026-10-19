# Provision Plans and the NVIDIA Driver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second plan kind, `provision`, that changes an installed and running Gentoo through the same approved-plan safety model as the install plan, and use it first to tune Portage for all CPU threads and to install the NVIDIA driver (open kernel modules) for an RTX 5070.

**Architecture:** A provision plan is a normal plan file (`kind: "provision"`) with the same hash, approval, worker, and logs, but it carries a host identity (SSH host key fingerprint, machine id, hostname) instead of disks, and its phases run as root directly on the installed system. The runner guards by kind and skips phases marked `run: false` (checked only by `verify_install`). New read-only inventory commands collect the host identity, CPU, memory, GPU, and the user's own Portage settings. Spec: `docs/superpowers/specs/2026-10-19-provision-plans-nvidia-design.md`.

**Tech Stack:** Python 3.12 standard library for `core/`, `unittest`, the optional `mcp` extra for the MCP server, POSIX `sh` programs sent over SSH.

**Conventions that apply to every task**

- Run the tests with `.venv/bin/python -m unittest ...` from the repository root. MCP tests need the `mcp` extra in `.venv` and skip themselves without it.
- `core/` never prints, prompts, or exits (`tests/test_core_purity.py`).
- No em dashes and no emoji in code, comments, or documentation.
- Commit messages carry no `Co-Authored-By`, `Claude-Session`, or "Generated with" lines.
- `GOLDEN_UPDATE=1` is not needed: this plan adds no install-plan command changes, so the install golden files must not change. If one changes, something is wrong.

## Decisions that differ from the spec (Task 1 updates the spec)

1. The accepted licenses are an explicit setting (`accept_licenses`, required), not parsed from `emerge` output. The agent reads `emerge --pretend` output, the human approves the list in the dialog, and an incomplete list makes `emerge` fail loudly. This avoids a fragile parser of Portage's text.
2. The GRUB parameter goes into a block that the plan owns in `/etc/default/grub` (markers `# gentoo-agent nvidia begin` and `end`), not into a drop-in directory, because the installer's GRUB does not create `/etc/default/grub.d/`.
3. A new first phase, `portage-tuning`, writes `MAKEOPTS` and `EMERGE_DEFAULT_OPTS` into a block it owns in `/etc/portage/make.conf`. The values come from the CPU thread count and the RAM (rule of thumb from the Handbook: the smaller of the thread count and the RAM in GiB divided by 2, to be confirmed against the Handbook in Task 13). Reason: the installer never wrote any compile setting, so the installed system uses the stage3 defaults.
4. The post-reboot check is a separate phase, `gpu-online`, with `run: false`, so the run ends in `installed_pending_reboot` and `verify_install` proves the GPU after the reboot.

## Amendment 1 (found while reviewing Task 1): GPU detection reads sysfs

`lspci` comes from `sys-apps/pciutils`, which a stage3 installation does not include, so an `lspci`-based
`gpu` command would fail on the server before anything starts. The `gpu` command reads `/sys/bus/pci/devices`
instead (always present). Its output is one line per PCI device: `<address> <class> <vendor> <device>` with
the hex values as sysfs prints them (`0000:01:00.0 0x030000 0x10de 0x2f04`). A display device is a class whose
first byte is `03`. The GPU name is built by the code from the vendor id, never taken from the target, so no
text from the target reaches the approval dialog for it. The unused `timeout` argument of `run_ssh_readonly` is
removed (nothing uses it). Task 1b applies the code change; Tasks 3, 6, 8, and 13 below already use the new
format.

## File structure

Create:

- `gentoo_agent/core/identity.py`: parse and compare the host identity; `check_identity`.
- `gentoo_agent/core/provision_facts.py`: strict parsers for GPU, CPU, memory, kernel, and `make.conf` facts; `summarize_provision_inventory`.
- `gentoo_agent/core/provision.py`: provision settings validation and the Portage tuning rule.
- `gentoo_agent/core/provision_phases.py`: the phase programs and their checks.
- `gentoo_agent/core/provision_plan.py`: `create_provision_plan`.
- `tests/provision_helpers.py`: shared fixtures.
- `tests/test_identity.py`, `tests/test_provision_inventory.py`, `tests/test_provision_facts.py`, `tests/test_provision_settings.py`, `tests/test_provision_phases.py`, `tests/test_provision_plan.py`, `tests/test_provision_approval.py`.
- `docs/guides/provision.md`.

Modify:

- `gentoo_agent/core/ssh.py`: `PROVISION_READ_ONLY_COMMANDS`, a `timeout` argument.
- `gentoo_agent/core/inventory.py`: shared `_collect`, `collect_provision_inventory`, `latest_provision_inventory`.
- `gentoo_agent/core/install_plan.py`: `plan_kind`.
- `gentoo_agent/core/runner.py`: guard by kind, skip `run: false` phases.
- `gentoo_agent/core/approval.py`: provision summary.
- `gentoo_agent/jobs/launcher.py`, `gentoo_agent/jobs/worker.py`: no passwords for provision plans, count only run phases.
- `gentoo_agent/mcp/server.py`: two tools, `plan_view`, `verify_install`, `_refuse_prompt`, descriptions.
- `gentoo_agent/cli.py`: `provision inspect` and `provision plan`.
- `tests/test_execute_plan.py`, `tests/test_mcp_server.py`, `tests/test_cli.py`, `tests/test_launcher.py` or `tests/test_worker.py` (new test cases only).
- Docs: `README.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md`, `docs/TROUBLESHOOTING.md`, `docs/ROADMAP.md`, the spec.

---

### Task 1: Read-only provision inventory (ssh allowlist and collection)

**Files:**
- Modify: `gentoo_agent/core/ssh.py`
- Modify: `gentoo_agent/core/inventory.py`
- Create: `tests/provision_helpers.py`
- Create: `tests/test_provision_inventory.py`

- [ ] **Step 1: Write the shared fixtures**

Create `tests/provision_helpers.py`:

```python
"""Shared fixtures for provision plan tests. The lspci text is a sample, not a claim about real device ids."""

from gentoo_agent.core.config import now

HOSTKEY = "SHA256:" + "A" * 43
MACHINE_ID = "0123456789abcdef0123456789abcdef"
PROVISION_TARGET = {"name": "gentoo-server2", "host": "192.0.2.10", "port": 22, "user": "root",
                    "identity_file": "~/.ssh/id_test", "known_hosts_file": ""}

LSPCI = (
    "01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GB205 [GeForce RTX 5070] [10de:2f04] (rev a1)\n"
    "\tSubsystem: Example Device [1043:89f3]\n"
    "\tKernel driver in use: nouveau\n"
    "\tKernel modules: nouveau\n"
    "01:00.1 Audio device [0403]: NVIDIA Corporation Device [10de:22eb] (rev a1)\n"
    "\tKernel driver in use: snd_hda_intel\n"
    "00:00.0 Host bridge [0600]: Example Corp Root Complex [1022:14d8]\n"
)


def result(label: str, stdout: str, returncode: int = 0) -> dict:
    return {"label": label, "command": "", "returncode": returncode, "stdout": stdout, "stderr": ""}


def host_identity_result(hostname: str = "gentoo", machine_id: str = MACHINE_ID, hostkey: str = HOSTKEY) -> dict:
    return result("host_identity",
                  f"hostname={hostname}\nmachine_id={machine_id}\nhostkey=256 {hostkey} root@{hostname} (ED25519)\n")


def cpu_result(threads: int = 24) -> dict:
    return result("cpu", f"threads={threads}\nArchitecture:        x86_64\nModel name:          Example CPU 12-Core Processor\n")


def memory_result(kib: int = 32768000) -> dict:
    return result("memory", f"MemTotal:       {kib} kB\nSwapTotal:      16777212 kB\n")


def gpu_result(text: str = LSPCI) -> dict:
    return result("gpu", text)


def portage_conf_result(text: str = "") -> dict:
    return result("portage_conf", text)


def kernel_result(release: str = "6.18.57-gentoo-dist-bin") -> dict:
    return result("kernel", release + "\n")


def provision_inventory(**replace: dict) -> dict:
    """A provision inventory collected now. replace maps a label to the result that replaces the default."""
    results = {
        "host_identity": host_identity_result(), "cpu": cpu_result(), "memory": memory_result(),
        "gpu": gpu_result(), "portage_conf": portage_conf_result(), "kernel": kernel_result(),
    }
    results.update(replace)
    return {"target": "gentoo-server2", "collected_at": now(), "results": list(results.values())}


def provision_settings(**replace) -> dict:
    settings = {"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2"]}
    settings.update(replace)
    return settings


def save_provision_plan(**inventory_replace: dict) -> dict:
    """Create and save a provision plan. Call inside tests.helpers.temp_home()."""
    from gentoo_agent.core.install_plan import save_install_plan
    from gentoo_agent.core.provision_plan import create_provision_plan

    return save_install_plan(
        create_provision_plan(PROVISION_TARGET, provision_settings(), provision_inventory(**inventory_replace)))
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_provision_inventory.py`:

```python
import re
import subprocess
import unittest
from unittest.mock import patch

from gentoo_agent.core import inventory, ssh
from gentoo_agent.core.errors import HOST_KEY_UNKNOWN, SSH_UNREACHABLE, AgentError
from tests.helpers import temp_home
from tests.provision_helpers import PROVISION_TARGET


def fake(target, label, command):
    return {"label": label, "command": command, "returncode": 0, "stdout": f"{label}\n", "stderr": ""}


class ProvisionCommandsTests(unittest.TestCase):
    def test_the_allowlist_has_the_expected_labels(self):
        self.assertEqual(list(ssh.PROVISION_READ_ONLY_COMMANDS),
                         ["host_identity", "cpu", "memory", "gpu", "portage_conf", "kernel"])

    def test_no_command_changes_anything(self):
        forbidden = re.compile(r">|\b(rm|mv|cp|tee|dd|emerge|rc-service|rc-update|mkdir|touch|chmod|chown|"
                               r"reboot|shutdown|kill|modprobe|sed -i)\b")
        for label, command in ssh.PROVISION_READ_ONLY_COMMANDS.items():
            with self.subTest(label=label):
                self.assertIsNone(forbidden.search(command.replace("2>/dev/null", "")), command)

    def test_the_timeout_can_be_raised_and_defaults_to_45(self):
        calls = []

        def run(*args, **kwargs):
            calls.append(kwargs["timeout"])
            return subprocess.CompletedProcess(args[0], 0, "", "")

        with patch.object(ssh.subprocess, "run", run):
            ssh.run_ssh_readonly(PROVISION_TARGET, "kernel", "uname -r")
            ssh.run_ssh_readonly(PROVISION_TARGET, "kernel", "uname -r", timeout=120)
        self.assertEqual(calls, [45, 120])


class CollectProvisionInventoryTests(unittest.TestCase):
    def test_the_inventory_is_saved_apart_from_the_install_inventory(self):
        with temp_home(), patch.object(inventory, "run_ssh_readonly", side_effect=fake):
            data, path = inventory.collect_provision_inventory(PROVISION_TARGET)
            latest = inventory.latest_provision_inventory()
            install_latest = inventory.latest_inventory()
        self.assertEqual([item["label"] for item in data["results"]], list(ssh.PROVISION_READ_ONLY_COMMANDS))
        self.assertTrue(path.name.startswith("provision-inventory-"))
        self.assertEqual(latest, data)
        self.assertIsNone(install_latest)

    def test_install_inventory_collection_is_unchanged(self):
        with temp_home(), patch.object(inventory, "run_ssh_readonly", side_effect=fake):
            data, path = inventory.collect_inventory(PROVISION_TARGET)
        self.assertEqual([item["label"] for item in data["results"]], list(ssh.READ_ONLY_COMMANDS))
        self.assertTrue(path.name.startswith("inventory-"))

    def test_nothing_is_saved_when_the_target_cannot_be_reached(self):
        down = {"label": "x", "command": "", "returncode": 255, "stdout": "", "stderr": "ssh: connect failed"}
        with temp_home(), patch.object(inventory, "run_ssh_readonly", return_value=down):
            with self.assertRaises(AgentError) as caught:
                inventory.collect_provision_inventory(PROVISION_TARGET)
            self.assertIsNone(inventory.latest_provision_inventory())
        self.assertEqual(caught.exception.code, SSH_UNREACHABLE)

    def test_a_changed_host_key_is_reported(self):
        refused = {"label": "x", "command": "", "returncode": 255, "stdout": "",
                   "stderr": "Host key verification failed.\n"}
        with temp_home(), patch.object(inventory, "run_ssh_readonly", return_value=refused):
            with self.assertRaises(AgentError) as caught:
                inventory.collect_provision_inventory(PROVISION_TARGET)
        self.assertEqual(caught.exception.code, HOST_KEY_UNKNOWN)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_provision_inventory -v`
Expected: errors such as `AttributeError: module 'gentoo_agent.core.ssh' has no attribute 'PROVISION_READ_ONLY_COMMANDS'`.

- [ ] **Step 4: Implement the allowlist and the timeout argument**

In `gentoo_agent/core/ssh.py`, add after the `READ_ONLY_COMMANDS` dict:

```python
# Read-only facts of a running, installed system, collected for provision plans (core/provision_plan.py).
PROVISION_READ_ONLY_COMMANDS = {
    "host_identity": (
        "printf 'hostname='; hostname; printf 'machine_id='; cat /etc/machine-id; "
        "printf 'hostkey='; ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub"
    ),
    "cpu": "printf 'threads='; nproc --all; lscpu",
    "memory": "grep -E '^(MemTotal|SwapTotal):' /proc/meminfo",
    "gpu": "lspci -nnk",
    # The user's own MAKEOPTS and EMERGE_DEFAULT_OPTS, outside the block that a plan writes.
    "portage_conf": (
        "sed -n -e '/^# gentoo-agent portage-tuning begin$/,/^# gentoo-agent portage-tuning end$/d' "
        "-e '/^\\(MAKEOPTS\\|EMERGE_DEFAULT_OPTS\\)=/p' /etc/portage/make.conf 2>/dev/null || true"
    ),
    "kernel": "uname -r",
}
```

and change the signature and the call inside `run_ssh_readonly`:

```python
def run_ssh_readonly(target: dict, label: str, command: str, timeout: int = 45) -> dict:
```

with `timeout=timeout,` in the `subprocess.run(...)` call instead of `timeout=45,`.

- [ ] **Step 5: Implement the shared collection**

In `gentoo_agent/core/inventory.py`, change the import line to
`from gentoo_agent.core.ssh import HOST_KEY_MESSAGE, PROVISION_READ_ONLY_COMMANDS, READ_ONLY_COMMANDS, host_key_failed, run_ssh_readonly`
and replace the `collect_inventory` function with:

```python
def _collect(target: dict, commands: dict, prefix: str,
             on_progress: Callable[[str], None] | None) -> tuple[dict, Path]:
    results = []
    for label, command in commands.items():
        if on_progress is not None:
            on_progress(label)
        try:
            results.append(run_ssh_readonly(target, label, command))
        except subprocess.TimeoutExpired as exc:
            results.append({"label": label, "command": command, "error": str(exc)})
        except OSError as exc:
            raise AgentError(SSH_UNREACHABLE, f"Could not run ssh to inspect {target.get('host')}: {exc}",
                             {"host": target.get("host"), "label": label}) from exc
    if all(_unreachable(item) for item in results):
        # An inventory without a single answer from the target is not evidence: never save it.
        if any(host_key_failed(item) for item in results):
            raise AgentError(HOST_KEY_UNKNOWN, HOST_KEY_MESSAGE, {"host": target["host"]})
        raise AgentError(SSH_UNREACHABLE, "Could not reach the target over SSH; check the address, key, and host key.",
                         {"host": target["host"]})
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data = {
        "target": target["name"],
        "target_address": f"{target['user']}@{target['host']}:{target.get('port', 22)}",
        "collected_at": now(),
        "read_only": True,
        "results": results,
    }
    path = paths.inventory_dir() / f"{prefix}-{stamp}.json"
    save_json(path, data)
    return data, path


def collect_inventory(target: dict, on_progress: Callable[[str], None] | None = None) -> tuple[dict, Path]:
    return _collect(target, READ_ONLY_COMMANDS, "inventory", on_progress)


def collect_provision_inventory(target: dict, on_progress: Callable[[str], None] | None = None) -> tuple[dict, Path]:
    """The facts of an installed, running system, saved apart from the install inventory."""
    return _collect(target, PROVISION_READ_ONLY_COMMANDS, "provision-inventory", on_progress)
```

and append at the end of the file:

```python
def latest_provision_inventory() -> dict | None:
    files = sorted(paths.inventory_dir().glob("provision-inventory-*.json"), key=lambda item: item.stat().st_mtime)
    return load_json(files[-1]) if files else None
```

- [ ] **Step 6: Run the tests and the existing inventory tests**

Run: `.venv/bin/python -m unittest tests.test_provision_inventory tests.test_state_inventory -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add gentoo_agent/core/ssh.py gentoo_agent/core/inventory.py tests/provision_helpers.py tests/test_provision_inventory.py
git commit -m "Collect a read-only provision inventory of a running system"
```

---

### Task 2: Host identity

**Files:**
- Create: `gentoo_agent/core/identity.py`
- Create: `tests/test_identity.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_identity.py`:

```python
import subprocess
import unittest
from unittest.mock import patch

from gentoo_agent.core import identity
from gentoo_agent.core.errors import HOST_KEY_UNKNOWN, INVENTORY_MISSING, SSH_UNREACHABLE, TARGET_DRIFT, AgentError
from tests.provision_helpers import HOSTKEY, MACHINE_ID, PROVISION_TARGET, host_identity_result

EXPECTED = {"hostname": "gentoo", "machine_id": MACHINE_ID, "hostkey": HOSTKEY}


class ParseIdentityTests(unittest.TestCase):
    def test_a_complete_result_is_parsed(self):
        self.assertEqual(identity.parse_identity(host_identity_result()["stdout"]), EXPECTED)

    def test_anything_else_is_unusable(self):
        good = host_identity_result()["stdout"]
        cases = {
            "not text": None,
            "empty": "",
            "hostname with shell text": good.replace("hostname=gentoo", "hostname=gentoo;reboot"),
            "hostname with a control sequence": good.replace("hostname=gentoo\n", "hostname=gentoo\x1b[2K\n"),
            "short machine id": good.replace(MACHINE_ID, MACHINE_ID[:-1]),
            "uppercase machine id": good.replace(MACHINE_ID, MACHINE_ID.upper()),
            "md5 fingerprint": good.replace(HOSTKEY, "MD5:aa:bb"),
            "no host key line": "\n".join(line for line in good.splitlines() if not line.startswith("hostkey=")),
        }
        for name, stdout in cases.items():
            with self.subTest(name), self.assertRaises(AgentError) as caught:
                identity.parse_identity(stdout)
            self.assertEqual(caught.exception.code, INVENTORY_MISSING)


class CheckIdentityTests(unittest.TestCase):
    def check(self, *, result=None, error=None, expected=EXPECTED):
        with patch.object(identity, "run_ssh_readonly", return_value=result, side_effect=error):
            identity.check_identity(PROVISION_TARGET, expected)

    def test_the_same_host_passes(self):
        self.check(result=host_identity_result())

    def test_a_different_machine_is_drift(self):
        with self.assertRaises(AgentError) as caught:
            self.check(result=host_identity_result(machine_id="f" * 32))
        self.assertEqual(caught.exception.code, TARGET_DRIFT)
        self.assertEqual(caught.exception.details["expected"], EXPECTED)
        self.assertEqual(caught.exception.details["actual"]["machine_id"], "f" * 32)

    def test_a_different_host_key_is_drift(self):
        with self.assertRaises(AgentError) as caught:
            self.check(result=host_identity_result(hostkey="SHA256:" + "B" * 43))
        self.assertEqual(caught.exception.code, TARGET_DRIFT)

    def test_a_timeout_or_launch_failure_is_unreachable(self):
        for error in (subprocess.TimeoutExpired("ssh", 45), FileNotFoundError("ssh")):
            with self.subTest(error=type(error).__name__), self.assertRaises(AgentError) as caught:
                self.check(error=error)
            self.assertEqual(caught.exception.code, SSH_UNREACHABLE)

    def test_a_host_key_failure_is_reported_as_such(self):
        refused = {"label": "host_identity", "command": "", "returncode": 255, "stdout": "",
                   "stderr": "Host key verification failed.\n"}
        with self.assertRaises(AgentError) as caught:
            self.check(result=refused)
        self.assertEqual(caught.exception.code, HOST_KEY_UNKNOWN)

    def test_a_failed_read_is_unreachable(self):
        failed = {"label": "host_identity", "command": "", "returncode": 1, "stdout": "", "stderr": "boom"}
        with self.assertRaises(AgentError) as caught:
            self.check(result=failed)
        self.assertEqual(caught.exception.code, SSH_UNREACHABLE)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_identity -v`
Expected: `ImportError: cannot import name 'identity'`.

- [ ] **Step 3: Implement**

Create `gentoo_agent/core/identity.py`:

```python
"""The identity of a running system: what a provision plan is bound to.

A plan stores the SSH host key fingerprint, the machine id, and the hostname. The runner reads them
again before the run and before every phase that changes the system, so a plan cannot run on another
server and a reinstalled server (new host key and machine id) invalidates it. Every value comes from
the target and is untrusted: it must match a strict format.
"""

from __future__ import annotations

import re
import subprocess

from gentoo_agent.core.errors import HOST_KEY_UNKNOWN, INVENTORY_MISSING, SSH_UNREACHABLE, TARGET_DRIFT, AgentError
from gentoo_agent.core.ssh import HOST_KEY_MESSAGE, PROVISION_READ_ONLY_COMMANDS, host_key_failed, run_ssh_readonly


IDENTITY_LABEL = "host_identity"
HOSTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
MACHINE_ID = re.compile(r"[0-9a-f]{32}")
HOSTKEY = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


def parse_identity(stdout: object) -> dict:
    """hostname, machine_id, and hostkey (the SHA256 fingerprint) from the host_identity result."""
    unusable = AgentError(INVENTORY_MISSING, "The host identity result is missing or malformed; collect it again.",
                          {"label": IDENTITY_LABEL})
    if not isinstance(stdout, str):
        raise unusable
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("hostname", "machine_id", "hostkey"):
            values[key] = value.strip()
    fields = values.get("hostkey", "").split()
    found = {"hostname": values.get("hostname", ""), "machine_id": values.get("machine_id", ""),
             "hostkey": fields[1] if len(fields) >= 2 else ""}
    if not (HOSTNAME.fullmatch(found["hostname"]) and MACHINE_ID.fullmatch(found["machine_id"])
            and HOSTKEY.fullmatch(found["hostkey"])):
        raise unusable
    return found


def read_live_identity(target: dict) -> dict:
    try:
        result = run_ssh_readonly(target, IDENTITY_LABEL, PROVISION_READ_ONLY_COMMANDS[IDENTITY_LABEL])
    except subprocess.TimeoutExpired as exc:
        raise AgentError(SSH_UNREACHABLE, f"Timed out reading the host identity from {target['host']}",
                         {"host": target["host"]}) from exc
    except OSError as exc:
        raise AgentError(SSH_UNREACHABLE, f"Could not run ssh to read the host identity from {target['host']}: {exc}",
                         {"host": target["host"]}) from exc
    if host_key_failed(result):
        raise AgentError(HOST_KEY_UNKNOWN, HOST_KEY_MESSAGE, {"host": target["host"]})
    if result["returncode"] != 0:
        raise AgentError(SSH_UNREACHABLE,
                         f"Could not read the host identity from {target['host']}: {result['stderr'].strip()}",
                         {"host": target["host"], "returncode": result["returncode"]})
    return parse_identity(result["stdout"])


def check_identity(target: dict, expected: dict) -> None:
    actual = read_live_identity(target)
    if actual != expected:
        raise AgentError(TARGET_DRIFT, "The target does not match the approved plan: it is another host or was reinstalled.",
                         {"expected": expected, "actual": actual})
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m unittest tests.test_identity tests.test_core_purity -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/identity.py tests/test_identity.py
git commit -m "Add the host identity that binds a provision plan to its server"
```

---

### Task 3: Fact parsers

**Files:**
- Create: `gentoo_agent/core/provision_facts.py`
- Create: `tests/test_provision_facts.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provision_facts.py`:

```python
import unittest

from gentoo_agent.core import provision_facts as facts
from gentoo_agent.core.errors import INVENTORY_MISSING, AgentError
from tests.provision_helpers import (
    GPU_SYSFS, cpu_result, gpu_result, host_identity_result, kernel_result, memory_result, portage_conf_result,
    provision_inventory, result,
)


class ParseGpuTests(unittest.TestCase):
    def test_only_display_devices_are_listed(self):
        self.assertEqual(facts.parse_gpus(GPU_SYSFS), [
            {"vendor": "nvidia", "pci_id": "10de:2f04", "name": "NVIDIA display controller"},
        ])

    def test_other_vendors_and_3d_controllers(self):
        text = ("0000:03:00.0 0x030200 0x10de 0x1234\n"
                "0000:00:02.0 0x038000 0x8086 0x46a6\n")
        self.assertEqual([(gpu["vendor"], gpu["pci_id"], gpu["name"]) for gpu in facts.parse_gpus(text)],
                         [("nvidia", "10de:1234", "NVIDIA display controller"),
                          ("other", "8086:46a6", "Unknown vendor display controller")])

    def test_no_gpu_is_an_empty_list(self):
        self.assertEqual(facts.parse_gpus("0000:00:00.0 0x060000 0x1022 0x14d8\n"), [])
        self.assertEqual(facts.parse_gpus(""), [])

    def test_unusable_text(self):
        for value in (None, 5):
            with self.subTest(value=value), self.assertRaises(AgentError) as caught:
                facts.parse_gpus(value)
            self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_lines_that_do_not_match_the_format_are_ignored(self):
        text = ("garbage\n0000:01:00.0 0x030000 0x10DE 0x2f04\n0000:01:00.0 030000 10de 2f04\n"
                "0000:01:00.0 0x030000 0x10de 0x2f04; reboot\n")
        self.assertEqual(facts.parse_gpus(text), [])


class ParseCpuAndMemoryTests(unittest.TestCase):
    def test_cpu(self):
        self.assertEqual(facts.parse_cpu(cpu_result(24)["stdout"]),
                         {"threads": 24, "model": "Example CPU 12-Core Processor"})

    def test_cpu_without_a_model_name(self):
        self.assertEqual(facts.parse_cpu("threads=8\n"), {"threads": 8, "model": "unknown"})

    def test_cpu_without_threads_is_unusable(self):
        for value in (None, "", "threads=0\n", "threads=x\n", "Model name: A\n"):
            with self.subTest(value=value), self.assertRaises(AgentError):
                facts.parse_cpu(value)

    def test_memory_in_kib(self):
        self.assertEqual(facts.parse_memory(memory_result(32768000)["stdout"]), 32768000)

    def test_memory_without_a_total_is_unusable(self):
        for value in (None, "", "SwapTotal: 5 kB\n", "MemTotal: lots kB\n"):
            with self.subTest(value=value), self.assertRaises(AgentError):
                facts.parse_memory(value)


class MakeConfTests(unittest.TestCase):
    def test_variables_the_user_already_sets(self):
        text = 'MAKEOPTS="-j4"\nEMERGE_DEFAULT_OPTS="--quiet-build"\n'
        self.assertEqual(facts.make_conf_overrides(text), ["EMERGE_DEFAULT_OPTS", "MAKEOPTS"])

    def test_nothing_set(self):
        self.assertEqual(facts.make_conf_overrides(""), [])
        self.assertEqual(facts.make_conf_overrides(None), [])


class SummaryTests(unittest.TestCase):
    def test_a_complete_inventory(self):
        summary = facts.summarize_provision_inventory(provision_inventory())
        self.assertEqual(summary["identity"]["hostname"], "gentoo")
        self.assertEqual(summary["cpu"]["threads"], 24)
        self.assertEqual(summary["memory_kib"], 32768000)
        self.assertEqual(summary["memory_gib"], 31)
        self.assertEqual(summary["kernel"], "6.18.57-gentoo-dist-bin")
        self.assertEqual(summary["gpus"][0]["pci_id"], "10de:2f04")
        self.assertEqual(summary["portage_overrides"], [])

    def test_a_missing_or_failed_required_result_is_refused(self):
        for label in ("host_identity", "cpu", "memory", "gpu", "kernel"):
            with self.subTest(label=label), self.assertRaises(AgentError) as caught:
                facts.summarize_provision_inventory(provision_inventory(**{label: result(label, "", 1)}))
            self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_a_failed_make_conf_read_only_means_no_overrides_are_known(self):
        summary = facts.summarize_provision_inventory(provision_inventory(portage_conf=result("portage_conf", "", 1)))
        self.assertEqual(summary["portage_overrides"], [])

    def test_a_hostile_kernel_release_is_unusable(self):
        with self.assertRaises(AgentError):
            facts.summarize_provision_inventory(provision_inventory(kernel=kernel_result("6.1; reboot")))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_provision_facts -v`
Expected: `ImportError: cannot import name 'provision_facts'`.

- [ ] **Step 3: Implement**

Create `gentoo_agent/core/provision_facts.py`:

```python
"""Parsers for the provision inventory: the GPU, CPU, memory, kernel, and Portage facts of a running system.

Pure functions. Every value comes from the target and is untrusted: it must match a strict format or the
inventory counts as unusable (INVENTORY_MISSING). Nothing unchecked reaches a program or the approval dialog.
"""

from __future__ import annotations

import re

from gentoo_agent.core.disks import inventory_results
from gentoo_agent.core.errors import INVENTORY_MISSING, AgentError
from gentoo_agent.core.identity import IDENTITY_LABEL, parse_identity


VENDORS = {"10de": "nvidia"}
VENDOR_NAMES = {"10de": "NVIDIA"}
# One line per PCI device from the gpu command: address, class, vendor, device (hex as sysfs prints it).
SYSFS_DEVICE = re.compile(
    r"(?P<address>[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]) 0x(?P<class>[0-9a-f]{6}) "
    r"0x(?P<vendor>[0-9a-f]{4}) 0x(?P<device>[0-9a-f]{4})"
)
DISPLAY_BASE_CLASS = "03"
KERNEL_RELEASE = re.compile(r"[0-9A-Za-z._+-]{1,64}")
TEXT_LIMIT = 120


def _unusable(label: str) -> AgentError:
    return AgentError(INVENTORY_MISSING, f"The provision inventory has no usable {label} result; collect it again.",
                      {"label": label})


def _text(value: str, label: str) -> str:
    if not value.isprintable() or len(value) > TEXT_LIMIT:
        raise _unusable(label)
    return value


def parse_gpus(stdout: object) -> list[dict]:
    """Display devices (PCI base class 03) with the vendor (nvidia or other), PCI id, and a name built here."""
    if not isinstance(stdout, str):
        raise _unusable("gpu")
    gpus = []
    for line in stdout.splitlines():
        match = SYSFS_DEVICE.fullmatch(line)
        if match is None or match["class"][:2] != DISPLAY_BASE_CLASS:
            continue
        vendor = match["vendor"]
        gpus.append({"vendor": VENDORS.get(vendor, "other"),
                     "pci_id": f"{vendor}:{match['device']}",
                     "name": f"{VENDOR_NAMES.get(vendor, 'Unknown vendor')} display controller"})
    return gpus


def parse_cpu(stdout: object) -> dict:
    if not isinstance(stdout, str):
        raise _unusable("cpu")
    threads = re.search(r"^threads=(\d{1,4})$", stdout, re.MULTILINE)
    if threads is None or int(threads[1]) < 1:
        raise _unusable("cpu")
    model = re.search(r"^Model name:\s+(.+)$", stdout, re.MULTILINE)
    return {"threads": int(threads[1]), "model": _text(model[1].strip(), "cpu") if model else "unknown"}


def parse_memory(stdout: object) -> int:
    """MemTotal in KiB."""
    if not isinstance(stdout, str):
        raise _unusable("memory")
    total = re.search(r"^MemTotal:\s+(\d{1,12}) kB$", stdout, re.MULTILINE)
    if total is None or int(total[1]) < 1:
        raise _unusable("memory")
    return int(total[1])


def make_conf_overrides(stdout: object) -> list[str]:
    """Names of the Portage variables the user already sets outside the plan's own block."""
    if not isinstance(stdout, str):
        return []
    return sorted(set(re.findall(r"^(MAKEOPTS|EMERGE_DEFAULT_OPTS)=", stdout, re.MULTILINE)))


def summarize_provision_inventory(inventory: dict) -> dict:
    results = inventory_results(inventory)

    def stdout(label: str) -> object:
        item = results.get(label)
        if item is None or item.get("returncode") != 0:
            raise _unusable(label)
        return item.get("stdout")

    kernel = str(stdout("kernel")).strip()
    if not KERNEL_RELEASE.fullmatch(kernel):
        raise _unusable("kernel")
    memory_kib = parse_memory(stdout("memory"))
    conf = results.get("portage_conf") or {}
    return {
        "collected_at": inventory.get("collected_at"),
        "identity": parse_identity(stdout(IDENTITY_LABEL)),
        "kernel": kernel,
        "cpu": parse_cpu(stdout("cpu")),
        "memory_kib": memory_kib,
        "memory_gib": memory_kib // (1024 * 1024),
        "gpus": parse_gpus(stdout("gpu")),
        "portage_overrides": make_conf_overrides(conf.get("stdout")) if conf.get("returncode") == 0 else [],
    }
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m unittest tests.test_provision_facts tests.test_core_purity -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/provision_facts.py tests/test_provision_facts.py
git commit -m "Parse the GPU, CPU, memory, and Portage facts of a running system strictly"
```

---

### Task 4: Provision settings and the Portage tuning rule

**Files:**
- Create: `gentoo_agent/core/provision.py`
- Create: `tests/test_provision_settings.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provision_settings.py`:

```python
import unittest

from gentoo_agent.core import provision
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError

GIB = 1024 * 1024


class ValidatedProvisionTests(unittest.TestCase):
    def test_a_valid_setting_is_returned_without_extra_keys(self):
        settings = {"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2", "Apache-2.0"], "extra": 1}
        self.assertEqual(provision.validated_provision(settings),
                         {"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2", "Apache-2.0"]})

    def test_invalid_settings_are_refused(self):
        cases = {
            "not a dict": None,
            "unknown profile": {"profile": "cuda", "accept_licenses": ["X"]},
            "no licenses": {"profile": "nvidia-driver", "accept_licenses": []},
            "licenses not a list": {"profile": "nvidia-driver", "accept_licenses": "NVIDIA-r2"},
            "a group instead of a name": {"profile": "nvidia-driver", "accept_licenses": ["@EULA"]},
            "shell text": {"profile": "nvidia-driver", "accept_licenses": ["X; reboot"]},
            "duplicate": {"profile": "nvidia-driver", "accept_licenses": ["X", "X"]},
            "too many": {"profile": "nvidia-driver", "accept_licenses": [f"L{i}" for i in range(9)]},
            "non-text item": {"profile": "nvidia-driver", "accept_licenses": [5]},
        }
        for name, settings in cases.items():
            with self.subTest(name), self.assertRaises(AgentError) as caught:
                provision.validated_provision(settings)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)


class TuningTests(unittest.TestCase):
    def test_the_smaller_of_threads_and_half_the_memory_in_gib(self):
        self.assertEqual(provision.derive_tuning(24, 32768000),
                         {"jobs": 15, "emerge_jobs": 3, "load_average": 24})
        self.assertEqual(provision.derive_tuning(8, 64 * GIB), {"jobs": 8, "emerge_jobs": 1, "load_average": 8})
        self.assertEqual(provision.derive_tuning(32, 128 * GIB), {"jobs": 32, "emerge_jobs": 4, "load_average": 32})

    def test_a_small_machine_still_gets_one_job(self):
        self.assertEqual(provision.derive_tuning(4, 1 * GIB), {"jobs": 1, "emerge_jobs": 1, "load_average": 4})

    def test_validated_tuning(self):
        good = {"jobs": 15, "emerge_jobs": 3, "load_average": 24, "extra": 1}
        self.assertEqual(provision.validated_tuning(good), {"jobs": 15, "emerge_jobs": 3, "load_average": 24})
        for bad in (None, {}, {"jobs": 0, "emerge_jobs": 1, "load_average": 1},
                    {"jobs": True, "emerge_jobs": 1, "load_average": 1},
                    {"jobs": "4", "emerge_jobs": 1, "load_average": 1},
                    {"jobs": 5000, "emerge_jobs": 1, "load_average": 1}):
            with self.subTest(bad=bad), self.assertRaises(AgentError) as caught:
                provision.validated_tuning(bad)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_provision_settings -v`
Expected: `ImportError: cannot import name 'provision'`.

- [ ] **Step 3: Implement**

Create `gentoo_agent/core/provision.py`:

```python
"""Validated settings of a provision plan and the Portage tuning rule."""

from __future__ import annotations

import re

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError


PROFILE_NVIDIA = "nvidia-driver"
LICENSE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,63}")
MAX_LICENSES = 8
TUNING_KEYS = ("jobs", "emerge_jobs", "load_average")
GIB_IN_KIB = 1024 * 1024


def validated_provision(provision: object) -> dict:
    """The profile and the accepted licenses; anything else in the input is dropped."""
    if not isinstance(provision, dict):
        raise AgentError(CONFIG_INVALID, "The provision settings must be an object.", {"field": "provision"})
    if provision.get("profile") != PROFILE_NVIDIA:
        raise AgentError(CONFIG_INVALID, f"Unknown provision profile: {provision.get('profile')!r}.", {"field": "profile"})
    licenses = provision.get("accept_licenses")
    if (
        not isinstance(licenses, list)
        or not 1 <= len(licenses) <= MAX_LICENSES
        or not all(isinstance(item, str) and LICENSE.fullmatch(item) for item in licenses)
        or len(set(licenses)) != len(licenses)
    ):
        raise AgentError(
            CONFIG_INVALID,
            f"accept_licenses must be 1 to {MAX_LICENSES} distinct license names (letters, digits, +, _, ., -).",
            {"field": "accept_licenses"},
        )
    return {"profile": provision["profile"], "accept_licenses": list(licenses)}


def derive_tuning(threads: int, mem_kib: int) -> dict:
    """Portage parallelism from the CPU threads and the memory.

    MAKEOPTS jobs: the smaller of the thread count and the RAM in GiB divided by 2 (the rule of thumb of the
    Gentoo Handbook for MAKEOPTS; confirmed against the Handbook in Task 13 of the plan). Parallel emerge jobs
    stay small because every one of them runs its own make; the load average keeps the machine busy without
    swamping it.
    """
    mem_gib = mem_kib // GIB_IN_KIB
    return {
        "jobs": max(1, min(threads, mem_gib // 2)),
        "emerge_jobs": max(1, min(4, threads // 8)),
        "load_average": threads,
    }


def validated_tuning(tuning: object) -> dict:
    if not isinstance(tuning, dict) or not all(
        isinstance(tuning.get(key), int) and not isinstance(tuning.get(key), bool) and 1 <= tuning[key] <= 4096
        for key in TUNING_KEYS
    ):
        raise AgentError(CONFIG_INVALID, "The Portage tuning values must be integers from 1 to 4096.", {"field": "tuning"})
    return {key: tuning[key] for key in TUNING_KEYS}
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m unittest tests.test_provision_settings tests.test_core_purity -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/provision.py tests/test_provision_settings.py
git commit -m "Validate provision settings and derive the Portage tuning from the hardware"
```

---

### Task 5: Phase programs

**Files:**
- Create: `gentoo_agent/core/provision_phases.py`
- Create: `tests/test_provision_phases.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provision_phases.py`:

```python
import re
import unittest

from gentoo_agent.core import provision_phases as phases
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError

TARGET = {"name": "gentoo-server2", "host": "192.0.2.10", "port": 22, "user": "root"}
PROVISION = {"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2", "Apache-2.0"],
             "tuning": {"jobs": 15, "emerge_jobs": 3, "load_average": 24}}
FORBIDDEN = re.compile(r"\b(wipefs|sfdisk|sgdisk|parted|mkfs\S*|mkswap|blkdiscard|dd|umount|swapoff|reboot|"
                       r"shutdown|poweroff|halt)\b")


def apply(phase):
    return phases.command_for(phase, TARGET, PROVISION)


def check(phase):
    return phases.check_command_for(phase, TARGET, PROVISION)


class PhaseListTests(unittest.TestCase):
    def test_order_and_flags(self):
        self.assertEqual(phases.PHASES, ("preflight", "portage-tuning", "portage-sync", "nvidia-config",
                                         "nvidia-drivers", "nvidia-verify", "gpu-online"))
        self.assertEqual(phases.PHASE_FLAGS["preflight"], (False, True))
        self.assertEqual(phases.PHASE_FLAGS["nvidia-drivers"], (True, True))
        self.assertEqual(phases.PHASE_FLAGS["gpu-online"], (False, False))
        self.assertEqual(set(phases.PHASE_FLAGS), set(phases.PHASES))

    def test_an_unknown_phase_is_invalid(self):
        for function in (phases.command_for, phases.check_command_for):
            with self.assertRaises(AgentError) as caught:
                function("storage", TARGET, PROVISION)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)


class SafetyTests(unittest.TestCase):
    def test_no_program_touches_a_disk_mounts_or_reboots(self):
        for phase in phases.PHASES:
            for kind, program in (("apply", apply(phase)), ("check", check(phase))):
                with self.subTest(phase=phase, kind=kind):
                    self.assertIsNone(FORBIDDEN.search(program), program)

    def test_every_program_starts_with_set_eu_and_ends_in_a_marker_or_exit(self):
        for phase in phases.PHASES:
            for program in (apply(phase), check(phase)):
                self.assertTrue(program.startswith("set -eu\n"), phase)


class TuningPhaseTests(unittest.TestCase):
    def test_the_block_is_replaced_not_appended_twice(self):
        program = apply("portage-tuning")
        self.assertIn("sed -i '/^# gentoo-agent portage-tuning begin$/,/^# gentoo-agent portage-tuning end$/d' /etc/portage/make.conf", program)
        self.assertIn("'MAKEOPTS=\"-j15\"'", program)
        self.assertIn("'EMERGE_DEFAULT_OPTS=\"--jobs=3 --load-average=24\"'", program)

    def test_the_check_asks_portage_for_the_effective_values(self):
        program = check("portage-tuning")
        self.assertIn("test \"$(portageq envvar MAKEOPTS)\" = -j15", program)
        self.assertIn("test \"$(portageq envvar EMERGE_DEFAULT_OPTS)\" = '--jobs=3 --load-average=24'", program)
        self.assertTrue(program.endswith("printf 'portage-tuning-ok\\n'"))


class NvidiaConfigPhaseTests(unittest.TestCase):
    def test_license_and_use_files_are_rewritten(self):
        program = apply("nvidia-config")
        self.assertIn("mkdir -p /etc/portage/package.license /etc/portage/package.use", program)
        self.assertIn("'*/* NVIDIA-r2 Apache-2.0' > /etc/portage/package.license/gentoo-agent-nvidia", program)
        self.assertIn("'x11-drivers/nvidia-drivers kernel-open dist-kernel' > /etc/portage/package.use/gentoo-agent-nvidia", program)

    def test_the_grub_parameter_is_a_block_the_plan_owns(self):
        program = apply("nvidia-config")
        self.assertIn("/^# gentoo-agent nvidia begin$/,/^# gentoo-agent nvidia end$/d", program)
        self.assertIn("nvidia-drm.modeset=1", program)
        self.assertIn("/etc/default/grub", program)

    def test_the_kernel_sources_link_follows_the_running_kernel(self):
        self.assertIn('eselect kernel set "linux-$(uname -r)"', apply("nvidia-config"))
        self.assertIn('test "$(readlink -f /usr/src/linux)" = "/usr/src/linux-$(uname -r)"', check("nvidia-config"))

    def test_hostile_licenses_never_reach_a_program(self):
        hostile = {**PROVISION, "accept_licenses": ["X; reboot"]}
        with self.assertRaises(AgentError):
            phases.command_for("nvidia-config", TARGET, hostile)


class DriverPhaseTests(unittest.TestCase):
    def test_apply_installs_the_package_and_rebuilds_the_grub_configuration(self):
        program = apply("nvidia-drivers")
        self.assertIn("emerge --noreplace --verbose x11-drivers/nvidia-drivers", program)
        self.assertLess(program.index("emerge"), program.index("grub-mkconfig -o /boot/grub/grub.cfg"))

    def test_check_needs_the_package_the_module_and_the_boot_parameter(self):
        program = check("nvidia-drivers")
        self.assertIn("ls -d /var/db/pkg/x11-drivers/nvidia-drivers-*", program)
        self.assertIn("-name 'nvidia*.ko*'", program)
        self.assertIn("grep -q 'nvidia-drm.modeset=1' /boot/grub/grub.cfg", program)


class VerifyPhasesTests(unittest.TestCase):
    def test_verify_checks_the_module_and_the_nouveau_blacklist(self):
        program = check("nvidia-verify")
        self.assertIn('modinfo -k "$(uname -r)" nvidia', program)
        self.assertIn("modprobe -c | grep -Eq '^blacklist nouveau$'", program)

    def test_a_verify_apply_only_reports_that_the_check_failed(self):
        self.assertIn("exit 1", apply("nvidia-verify"))

    def test_gpu_online_needs_the_driver_loaded_and_the_card_listed(self):
        program = check("gpu-online")
        self.assertIn("lsmod | grep -q '^nvidia '", program)
        self.assertIn("nvidia-smi -L | grep -q NVIDIA", program)
        self.assertIn("/sys/module/nvidia_drm/parameters/modeset", program)
        self.assertIn("/opt/bin", program)


class PreflightPhaseTests(unittest.TestCase):
    def test_preflight_is_its_own_check(self):
        self.assertEqual(apply("preflight"), check("preflight"))
        program = check("preflight")
        for fragment in ('test "$(id -u)" -eq 0', "ip route | grep -q default", "command -v emerge",
                         'test -d "/usr/src/linux-$(uname -r)"', "8388608"):
            self.assertIn(fragment, program)
        self.assertTrue(program.endswith("printf 'preflight-ok\\n'"))


class SyncPhaseTests(unittest.TestCase):
    def test_sync_refreshes_the_tree_and_the_check_wants_a_recent_timestamp(self):
        self.assertIn("emerge-webrsync", apply("portage-sync"))
        self.assertIn("-name timestamp.chk -mtime -1", check("portage-sync"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_provision_phases -v`
Expected: `ImportError: cannot import name 'provision_phases'`.

- [ ] **Step 3: Implement**

Create `gentoo_agent/core/provision_phases.py`:

```python
"""Phase programs for provision plans. They run as root on an installed, running system.

Every apply program starts with set -eu and is idempotent: files are rewritten, blocks are replaced, emerge
uses --noreplace. Nothing here touches a disk, mounts or unmounts anything, or reboots (a test scans every
program for such commands). Values are quoted with shlex.quote and come from validated settings.
"""

from __future__ import annotations

import shlex

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.provision import validated_provision, validated_tuning


q = shlex.quote

PHASES = ("preflight", "portage-tuning", "portage-sync", "nvidia-config", "nvidia-drivers", "nvidia-verify",
          "gpu-online")
# phase -> (destructive: changes the system, run: executed by the worker; False means verify_install only)
PHASE_FLAGS = {
    "preflight": (False, True),
    "portage-tuning": (True, True),
    "portage-sync": (True, True),
    "nvidia-config": (True, True),
    "nvidia-drivers": (True, True),
    "nvidia-verify": (False, True),
    "gpu-online": (False, False),
}

MAKE_CONF = "/etc/portage/make.conf"
GRUB_DEFAULTS = "/etc/default/grub"
LICENSE_FILE = "/etc/portage/package.license/gentoo-agent-nvidia"
USE_FILE = "/etc/portage/package.use/gentoo-agent-nvidia"
NVIDIA_PACKAGE = "x11-drivers/nvidia-drivers"
NVIDIA_USE = "kernel-open dist-kernel"
TUNING_BEGIN = "# gentoo-agent portage-tuning begin"
TUNING_END = "# gentoo-agent portage-tuning end"
GRUB_BEGIN = "# gentoo-agent nvidia begin"
GRUB_END = "# gentoo-agent nvidia end"
GRUB_LINE = 'GRUB_CMDLINE_LINUX="${GRUB_CMDLINE_LINUX:+$GRUB_CMDLINE_LINUX }nvidia-drm.modeset=1"'
MIN_FREE_KIB = 8 * 1024 * 1024


def _program(*lines: str) -> str:
    return "\n".join(["set -eu", *lines])


def _block_apply(path: str, begin: str, end: str, lines: list[str]) -> list[str]:
    """Replace the block between the markers (or add it): the phase stays idempotent."""
    return [
        f"touch {q(path)}",
        f"sed -i {q('/^' + begin + '$/,/^' + end + '$/d')} {q(path)}",
        "printf '%s\\n' " + " ".join(q(line) for line in [begin, *lines, end]) + f" >> {q(path)}",
    ]


def _block_check(path: str, begin: str, end: str, lines: list[str]) -> list[str]:
    return [f"grep -Fqx {q(line)} {q(path)}" for line in [begin, *lines, end]]


def _preflight() -> str:
    return _program(
        'test "$(id -u)" -eq 0',
        "ip route | grep -q default",
        "command -v emerge >/dev/null",
        'test "$(findmnt -rn -o FSTYPE /boot)" = vfat',
        f"test \"$(df -Pk / | awk 'NR==2 {{print $4}}')\" -ge {MIN_FREE_KIB}",
        'test -d "/usr/src/linux-$(uname -r)"',
        "printf 'preflight-ok\\n'",
    )


def _tuning_lines(provision: dict) -> list[str]:
    tuning = validated_tuning(provision.get("tuning"))
    return [
        f'MAKEOPTS="-j{tuning["jobs"]}"',
        f'EMERGE_DEFAULT_OPTS="--jobs={tuning["emerge_jobs"]} --load-average={tuning["load_average"]}"',
    ]


def _tuning_apply(provision: dict) -> str:
    return _program(*_block_apply(MAKE_CONF, TUNING_BEGIN, TUNING_END, _tuning_lines(provision)))


def _tuning_check(provision: dict) -> str:
    tuning = validated_tuning(provision.get("tuning"))
    return _program(
        *_block_check(MAKE_CONF, TUNING_BEGIN, TUNING_END, _tuning_lines(provision)),
        f'test "$(portageq envvar MAKEOPTS)" = -j{tuning["jobs"]}',
        f'test "$(portageq envvar EMERGE_DEFAULT_OPTS)" = '
        f'{q("--jobs=" + str(tuning["emerge_jobs"]) + " --load-average=" + str(tuning["load_average"]))}',
        "printf 'portage-tuning-ok\\n'",
    )


def _sync_apply() -> str:
    return _program("emerge-webrsync")


def _sync_check() -> str:
    return _program(
        "test -n \"$(find /var/db/repos/gentoo/metadata -maxdepth 1 -name timestamp.chk -mtime -1)\"",
        "printf 'portage-sync-ok\\n'",
    )


def _license_line(provision: dict) -> str:
    return "*/* " + " ".join(validated_provision(provision)["accept_licenses"])


def _config_apply(provision: dict) -> str:
    return _program(
        "mkdir -p /etc/portage/package.license /etc/portage/package.use",
        f"printf '%s\\n' {q(_license_line(provision))} > {q(LICENSE_FILE)}",
        f"printf '%s\\n' {q(NVIDIA_PACKAGE + ' ' + NVIDIA_USE)} > {q(USE_FILE)}",
        *_block_apply(GRUB_DEFAULTS, GRUB_BEGIN, GRUB_END, [GRUB_LINE]),
        'eselect kernel set "linux-$(uname -r)"',
    )


def _config_check(provision: dict) -> str:
    return _program(
        f"test -s {q(LICENSE_FILE)}",
        f"grep -Fqx {q(_license_line(provision))} {q(LICENSE_FILE)}",
        f"grep -Fqx {q(NVIDIA_PACKAGE + ' ' + NVIDIA_USE)} {q(USE_FILE)}",
        *_block_check(GRUB_DEFAULTS, GRUB_BEGIN, GRUB_END, [GRUB_LINE]),
        'test "$(readlink -f /usr/src/linux)" = "/usr/src/linux-$(uname -r)"',
        "printf 'nvidia-config-ok\\n'",
    )


def _drivers_apply() -> str:
    return _program(f"emerge --noreplace --verbose {NVIDIA_PACKAGE}", "grub-mkconfig -o /boot/grub/grub.cfg")


def _drivers_check() -> str:
    return _program(
        "ls -d /var/db/pkg/x11-drivers/nvidia-drivers-* >/dev/null",
        "find \"/lib/modules/$(uname -r)\" -name 'nvidia*.ko*' | grep -q .",
        "grep -q 'nvidia-drm.modeset=1' /boot/grub/grub.cfg",
        "printf 'nvidia-drivers-ok\\n'",
    )


def _verify_apply(message: str) -> str:
    return _program(f"printf '%s\\n' {q(message)} >&2", "exit 1")


def _verify_check() -> str:
    return _program(
        'modinfo -k "$(uname -r)" nvidia >/dev/null',
        "modprobe -c | grep -Eq '^blacklist nouveau$'",
        "printf 'nvidia-verify-ok\\n'",
    )


def _online_check() -> str:
    return _program(
        'PATH="$PATH:/opt/bin"',
        "lsmod | grep -q '^nvidia '",
        "nvidia-smi -L | grep -q NVIDIA",
        'test "$(cat /sys/module/nvidia_drm/parameters/modeset)" = Y',
        "printf 'gpu-online-ok\\n'",
    )


def command_for(phase: str, target: dict, provision: dict) -> str:
    """The apply program of a phase (target is part of the generator interface, like phases.command_for)."""
    programs = {
        "preflight": lambda: _preflight(),
        "portage-tuning": lambda: _tuning_apply(provision),
        "portage-sync": lambda: _sync_apply(),
        "nvidia-config": lambda: _config_apply(provision),
        "nvidia-drivers": lambda: _drivers_apply(),
        "nvidia-verify": lambda: _verify_apply("nvidia-verify has nothing to apply; read the check output"),
        "gpu-online": lambda: _verify_apply("gpu-online is only checked after a reboot (verify_install)"),
    }
    if phase not in programs:
        raise AgentError(CONFIG_INVALID, f"Unknown provision phase: {phase}", {"phase": phase})
    return programs[phase]()


def check_command_for(phase: str, target: dict, provision: dict) -> str:
    """The read-only check of a phase. Exit 0 means the phase is complete."""
    checks = {
        "preflight": lambda: _preflight(),
        "portage-tuning": lambda: _tuning_check(provision),
        "portage-sync": lambda: _sync_check(),
        "nvidia-config": lambda: _config_check(provision),
        "nvidia-drivers": lambda: _drivers_check(),
        "nvidia-verify": lambda: _verify_check(),
        "gpu-online": lambda: _online_check(),
    }
    if phase not in checks:
        raise AgentError(CONFIG_INVALID, f"Unknown provision phase: {phase}", {"phase": phase})
    return checks[phase]()
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m unittest tests.test_provision_phases tests.test_core_purity -v`
Expected: all pass. The `sed -i` and `printf` lines are only compared as text here; their behavior is verified on the real server in Task 13.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/provision_phases.py tests/test_provision_phases.py
git commit -m "Add the provision phase programs for Portage tuning and the NVIDIA driver"
```

---

### Task 6: Creating a provision plan

**Files:**
- Modify: `gentoo_agent/core/install_plan.py` (add `plan_kind`)
- Create: `gentoo_agent/core/provision_plan.py`
- Create: `tests/test_provision_plan.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provision_plan.py`:

```python
import unittest

from gentoo_agent.core import install_plan
from gentoo_agent.core.errors import CONFIG_INVALID, INVENTORY_MISSING, PLAN_HASH_MISMATCH, AgentError
from gentoo_agent.core.provision_plan import create_provision_plan
from tests.helpers import temp_home
from tests.provision_helpers import (
    GPU_SYSFS, MACHINE_ID, PROVISION_TARGET, cpu_result, gpu_result, memory_result, portage_conf_result,
    provision_inventory, provision_settings, result, save_provision_plan,
)


def create(inventory=None, settings=None, target=PROVISION_TARGET):
    return create_provision_plan(target, settings or provision_settings(),
                                 provision_inventory() if inventory is None else inventory)


class PlanKindTests(unittest.TestCase):
    def test_a_plan_without_kind_is_an_install_plan(self):
        self.assertEqual(install_plan.plan_kind({"phases": []}), "install")
        self.assertEqual(install_plan.plan_kind({"kind": "provision"}), "provision")


class CreateProvisionPlanTests(unittest.TestCase):
    def test_the_plan_freezes_identity_settings_and_every_program(self):
        plan = create()
        self.assertEqual(plan["kind"], "provision")
        self.assertEqual(plan["identity"], {"hostname": "gentoo", "machine_id": MACHINE_ID,
                                            "hostkey": "SHA256:" + "A" * 43})
        self.assertEqual(plan["provision"]["tuning"], {"jobs": 15, "emerge_jobs": 3, "load_average": 24})
        self.assertEqual(plan["provision"]["gpu"]["pci_id"], "10de:2f04")
        self.assertEqual(plan["provision"]["accept_licenses"], ["NVIDIA-r2"])
        self.assertEqual(plan["provision"]["memory_gib"], 31)
        self.assertEqual([phase["id"] for phase in plan["phases"]],
                         ["preflight", "portage-tuning", "portage-sync", "nvidia-config", "nvidia-drivers",
                          "nvidia-verify", "gpu-online"])
        self.assertEqual([phase["run"] for phase in plan["phases"]], [True] * 6 + [False])
        self.assertTrue(all(phase["apply"] and phase["check"] for phase in plan["phases"]))
        self.assertNotIn("disks", plan)
        self.assertNotIn("install", plan)
        self.assertEqual(plan["plan_id"], plan["plan_hash"][:16])

    def test_the_same_inputs_give_the_same_plan_id(self):
        inventory = provision_inventory()
        self.assertEqual(create(inventory)["plan_id"], create(inventory)["plan_id"])

    def test_different_licenses_give_a_different_plan(self):
        inventory = provision_inventory()
        other = create(inventory, provision_settings(accept_licenses=["NVIDIA-r2", "Apache-2.0"]))
        self.assertNotEqual(create(inventory)["plan_id"], other["plan_id"])

    def test_the_plan_can_be_saved_and_loaded(self):
        with temp_home():
            saved = save_provision_plan()
            loaded = install_plan.load_install_plan(saved["plan_id"])
        self.assertEqual(loaded, saved)

    def test_a_modified_file_is_refused(self):
        with temp_home() as home:
            saved = save_provision_plan()
            path = home / "plans" / f"{saved['plan_id']}.json"
            path.write_text(path.read_text(encoding="utf-8").replace("NVIDIA-r2", "Other-1"), encoding="utf-8")
            with self.assertRaises(AgentError) as caught:
                install_plan.load_install_plan(saved["plan_id"])
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_the_users_own_portage_settings_become_a_warning(self):
        plan = create(provision_inventory(portage_conf=portage_conf_result('MAKEOPTS="-j2"\n')))
        self.assertEqual(plan["warnings"],
                         ["/etc/portage/make.conf already sets MAKEOPTS; the plan's value replaces it."])


class RefusalTests(unittest.TestCase):
    def refused(self, **kwargs):
        with self.assertRaises(AgentError) as caught:
            create(**kwargs)
        return caught.exception

    def test_no_inventory(self):
        with self.assertRaises(AgentError) as caught:
            create_provision_plan(PROVISION_TARGET, provision_settings(), None)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_a_stale_inventory(self):
        inventory = provision_inventory()
        inventory["collected_at"] = "2020-01-01T00:00:00+00:00"
        self.assertEqual(self.refused(inventory=inventory).code, INVENTORY_MISSING)

    def test_no_nvidia_gpu(self):
        text = "0000:00:00.0 0x060000 0x1022 0x14d8\n"
        error = self.refused(inventory=provision_inventory(gpu=gpu_result(text)))
        self.assertEqual(error.code, CONFIG_INVALID)
        self.assertEqual(error.details["nvidia_gpus"], 0)

    def test_two_nvidia_gpus_are_ambiguous(self):
        text = GPU_SYSFS + "0000:02:00.0 0x030000 0x10de 0x2f05\n"
        error = self.refused(inventory=provision_inventory(gpu=gpu_result(text)))
        self.assertEqual(error.code, CONFIG_INVALID)
        self.assertEqual(error.details["nvidia_gpus"], 2)

    def test_invalid_settings(self):
        self.assertEqual(self.refused(settings=provision_settings(accept_licenses=[])).code, CONFIG_INVALID)

    def test_the_ssh_user_must_be_root(self):
        self.assertEqual(self.refused(target={**PROVISION_TARGET, "user": "roza"}).code, CONFIG_INVALID)

    def test_a_failed_required_result_is_refused(self):
        self.assertEqual(self.refused(inventory=provision_inventory(cpu=result("cpu", "", 1))).code, INVENTORY_MISSING)
```


- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_provision_plan -v`
Expected: `ImportError: cannot import name 'provision_plan'` and `AttributeError: ... 'plan_kind'`.

- [ ] **Step 3: Implement `plan_kind`**

In `gentoo_agent/core/install_plan.py`, add after `compute_plan_hash`:

```python
def plan_kind(plan: dict) -> str:
    """install (also for a plan without kind, written before provision plans existed) or provision."""
    return plan.get("kind", "install")
```

- [ ] **Step 4: Implement `create_provision_plan`**

Create `gentoo_agent/core/provision_plan.py`:

```python
"""Provision plans: the frozen, hashed description of changes to an installed, running system.

Same plan file, hash, and approval as an install plan. The plan holds the host identity instead of the
disks, the validated settings with the facts they were derived from, and every generated program.
"""

from __future__ import annotations

import hashlib

from gentoo_agent.core.config import now
from gentoo_agent.core.errors import CONFIG_INVALID, INVENTORY_MISSING, AgentError
from gentoo_agent.core.install_plan import (
    PLAN_ID_LENGTH,
    PLAN_SCHEMA_VERSION,
    TARGET_FIELDS,
    _check_inventory_fresh,
    canonical,
    compute_plan_hash,
)
from gentoo_agent.core.provision import derive_tuning, validated_provision
from gentoo_agent.core.provision_facts import summarize_provision_inventory
from gentoo_agent.core.provision_phases import PHASE_FLAGS, PHASES, check_command_for, command_for


def create_provision_plan(target: dict, settings: dict, inventory: dict | None) -> dict:
    if target.get("user") != "root":
        raise AgentError(CONFIG_INVALID, "Provision plans run as root; the SSH user must be root.", {"field": "user"})
    settings = validated_provision(settings)
    if inventory is None:
        raise AgentError(INVENTORY_MISSING, "No provision inventory exists; collect it first.", {})
    _check_inventory_fresh(inventory)
    facts = summarize_provision_inventory(inventory)
    nvidia = [gpu for gpu in facts["gpus"] if gpu["vendor"] == "nvidia"]
    if len(nvidia) != 1:
        raise AgentError(CONFIG_INVALID, f"Expected exactly one NVIDIA GPU on the target, found {len(nvidia)}.",
                         {"nvidia_gpus": len(nvidia)})
    provision = {
        **settings,
        "gpu": nvidia[0],
        "cpu": facts["cpu"],
        "memory_gib": facts["memory_gib"],
        "tuning": derive_tuning(facts["cpu"]["threads"], facts["memory_kib"]),
    }
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "kind": "provision",
        "target": {field: target.get(field) for field in TARGET_FIELDS},
        "identity": facts["identity"],
        "provision": provision,
        "warnings": [f"/etc/portage/make.conf already sets {name}; the plan's value replaces it."
                     for name in facts["portage_overrides"]],
        "inventory_hash": hashlib.sha256(canonical(inventory)).hexdigest(),
        "phases": [
            {
                "id": phase,
                "destructive": PHASE_FLAGS[phase][0],
                "run": PHASE_FLAGS[phase][1],
                "apply": command_for(phase, target, provision),
                "check": check_command_for(phase, target, provision),
            }
            for phase in PHASES
        ],
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    plan["plan_id"] = plan["plan_hash"][:PLAN_ID_LENGTH]
    plan["created_at"] = now()
    return plan
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m unittest tests.test_provision_plan tests.test_install_plan tests.test_core_purity -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add gentoo_agent/core/install_plan.py gentoo_agent/core/provision_plan.py tests/test_provision_plan.py
git commit -m "Create frozen provision plans bound to a host identity"
```

---

### Task 7: The runner guards by kind and skips verify-only phases

**Files:**
- Modify: `gentoo_agent/core/runner.py`
- Modify: `tests/test_execute_plan.py` (new class only)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_execute_plan.py`, after `STORAGE_PLAN` or `PLAN`, this fixture:

```python
IDENTITY = {"hostname": "gentoo", "machine_id": "0" * 32, "hostkey": "SHA256:" + "A" * 43}
PROVISION_PLAN = {
    "plan_id": "b" * 16,
    "plan_hash": "b" * 64,
    "kind": "provision",
    "target": TARGET,
    "identity": IDENTITY,
    "phases": [
        {"id": "preflight", "destructive": False, "run": True, "apply": "apply-preflight", "check": "check-preflight"},
        {"id": "nvidia-drivers", "destructive": True, "run": True, "apply": "apply-drivers", "check": "check-drivers"},
        {"id": "gpu-online", "destructive": False, "run": False, "apply": "apply-online", "check": "check-online"},
    ],
}
```

and this class before `class ExecutePlanTests`:

```python
class ProvisionPlanTests(unittest.TestCase):
    def run_provision(self, shell, plan=PROVISION_PLAN, phase_ids=None, identity_error=None):
        run_id = runs.new_run_id()
        runs.write_run(run_id, {"run_id": run_id, "plan_id": plan["plan_id"], "status": "running",
                                "pid": None, "started_at": runs.now()})
        with patch.object(runner, "remote_shell", shell), \
                patch.object(runner, "check_identity", side_effect=identity_error) as identity, \
                patch.object(runner, "check_drift") as drift:
            result = runner.execute_plan(plan, run_id, None, phase_ids=phase_ids)
        return result, identity, drift

    def test_the_host_identity_is_checked_instead_of_disks(self):
        with temp_home():
            shell = FakeShell({"nvidia-drivers-check": 1})
            result, identity, drift = self.run_provision(shell)
        self.assertEqual(result["status"], "installed_pending_reboot")
        self.assertEqual(identity.call_count, 2)  # before the run, and before the destructive apply
        identity.assert_called_with(TARGET, IDENTITY)
        self.assertEqual(drift.call_count, 0)
        self.assertEqual([call[0] for call in shell.calls],
                         ["preflight-check", "nvidia-drivers-check", "nvidia-drivers", "nvidia-drivers-verify"])

    def test_a_verify_only_phase_is_never_run(self):
        with temp_home():
            shell = FakeShell()
            result, _, _ = self.run_provision(shell)
        self.assertFalse(any(call[0].startswith("gpu-online") for call in shell.calls))
        self.assertEqual(result["completed_phases"], ["preflight", "nvidia-drivers"])

    def test_a_verify_only_phase_cannot_be_selected(self):
        with temp_home():
            with self.assertRaises(AgentError) as caught:
                self.run_provision(FakeShell(), phase_ids=["gpu-online"])
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_a_plan_without_an_identity_fails_before_any_command(self):
        plan = {key: value for key, value in PROVISION_PLAN.items() if key != "identity"}
        with temp_home():
            shell = FakeShell()
            result, identity, _ = self.run_provision(shell, plan=plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], TARGET_DRIFT)
        self.assertEqual(shell.calls, [])
        self.assertEqual(identity.call_count, 0)

    def test_a_different_host_stops_the_run_before_any_phase(self):
        error = AgentError(TARGET_DRIFT, "The target does not match the approved plan.")
        with temp_home():
            shell = FakeShell()
            result, _, _ = self.run_provision(shell, identity_error=error)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], TARGET_DRIFT)
        self.assertEqual(shell.calls, [])
```


- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_execute_plan.ProvisionPlanTests -v`
Expected: `AttributeError: <module 'gentoo_agent.core.runner'> does not have the attribute 'check_identity'`.

- [ ] **Step 3: Implement**

In `gentoo_agent/core/runner.py`:

1. Add to the imports:

```python
from gentoo_agent.core.identity import check_identity
from gentoo_agent.core.install_plan import plan_kind
```

2. In `execute_plan`, right after `current: str | None = None`, add:

```python
    kind = plan_kind(plan)

    def guard() -> None:
        # What identifies the target: the install disks, or the host a provision plan was made for.
        if kind == "provision":
            check_identity(target, plan["identity"])
        else:
            check_drift(target, plan["disks"])
```

3. Replace the line `phases = plan["phases"]` with:

```python
    # Phases marked run: false are checked by verify_install only (for example the GPU after a reboot).
    phases = [phase for phase in plan["phases"] if phase.get("run", True)]
```

4. Replace the block

```python
        if not isinstance(plan.get("disks"), list) or not plan["disks"]:
            raise AgentError(TARGET_DRIFT, "The plan has no disk identity to verify.", {})
        emit("checking that the target disk still matches the plan")
        check_drift(target, plan["disks"])
```

with:

```python
        if kind == "provision":
            if not isinstance(plan.get("identity"), dict) or not plan["identity"]:
                raise AgentError(TARGET_DRIFT, "The plan has no host identity to verify.", {})
            emit("checking that the target host still matches the plan")
        else:
            if not isinstance(plan.get("disks"), list) or not plan["disks"]:
                raise AgentError(TARGET_DRIFT, "The plan has no disk identity to verify.", {})
            emit("checking that the target disk still matches the plan")
        guard()
```

5. In the destructive block replace `check_drift(target, plan["disks"])` with `guard()` (and keep the preceding `emit` line, changing its wording to `f"{current}: re-checking that the target still matches the plan"`).

- [ ] **Step 4: Run the runner and worker tests**

Run: `.venv/bin/python -m unittest tests.test_execute_plan tests.test_worker tests.test_core_purity -v`
Expected: all pass, including the existing install tests (they patch `runner.check_drift`, which is still what an install plan calls).

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/runner.py tests/test_execute_plan.py
git commit -m "Guard a run by kind and skip verify-only phases in the runner"
```

---

### Task 8: Approval summary of a provision plan

**Files:**
- Modify: `gentoo_agent/core/approval.py`
- Create: `tests/test_provision_approval.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provision_approval.py`:

```python
import datetime as dt
import unittest

from gentoo_agent.core import approval
from tests.helpers import temp_home
from tests.provision_helpers import MACHINE_ID, save_provision_plan

START = dt.datetime(2026, 10, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


class ProvisionApprovalTests(unittest.TestCase):
    def test_the_summary_lists_what_the_plan_changes(self):
        with temp_home():
            summary = approval.approval_summary(save_provision_plan())
        for expected in (
            "Kind: provision (changes the installed system; no disk is erased)",
            "Disk erase: no",
            "Phases that change the system: portage-tuning, portage-sync, nvidia-config, nvidia-drivers",
            "Target: root@192.0.2.10:22 (gentoo-server2)",
            "machine-id=" + MACHINE_ID,
            "GPU: NVIDIA display controller [10de:2f04]",
            "CPU: Example CPU 12-Core Processor, 24 threads; memory 31 GiB",
            "Portage: MAKEOPTS=-j15, EMERGE_DEFAULT_OPTS=--jobs=3 --load-average=24",
            "Packages: x11-drivers/nvidia-drivers (USE kernel-open dist-kernel)",
            "Licenses accepted: NVIDIA-r2 (only for x11-drivers/nvidia-drivers)",
            "Reboot: required afterwards; the installer never reboots",
        ):
            self.assertIn(expected, summary)
        self.assertNotIn("Disk (erased)", summary)
        self.assertNotIn("ERASE WINDOWS", summary)

    def test_the_approval_phrase_has_no_erase_part(self):
        with temp_home():
            plan = save_provision_plan()
            self.assertEqual(approval.approval_phrase(plan), f"APPROVE {plan['plan_id']}")
            self.assertIsNone(approval.erase_phrase(plan))

    def test_a_provision_plan_can_be_approved_and_then_required(self):
        with temp_home():
            plan = save_provision_plan()
            approval.record_approval(plan, "cli", at=START)
            loaded = approval.require_approved_plan(plan["plan_id"], at=START + dt.timedelta(minutes=59))
        self.assertEqual(loaded["plan_hash"], plan["plan_hash"])
        self.assertEqual(loaded["kind"], "provision")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_provision_approval -v`
Expected: `KeyError: 'install'` from `approval_summary`.

- [ ] **Step 3: Implement**

In `gentoo_agent/core/approval.py`, change the import of the plan module to include `plan_kind` (`from gentoo_agent.core.install_plan import load_install_plan, plan_kind`), add this function above `approval_summary`:

```python
def _provision_summary(plan: dict) -> str:
    target, provision, identity = plan["target"], plan["provision"], plan["identity"]
    tuning, gpu, cpu = provision["tuning"], provision["gpu"], provision["cpu"]
    changing = [_shown(phase["id"]) for phase in plan["phases"] if phase["destructive"]]
    lines = [
        f"Plan: {_shown(plan['plan_id'])}",
        f"Plan hash: {_shown(plan['plan_hash'])}",
        "Kind: provision (changes the installed system; no disk is erased)",
        "Disk erase: no",
        "Phases that change the system: " + ", ".join(changing),
        *(f"WARNING: {_shown(warning)}" for warning in plan.get("warnings", [])),
        f"Target: {_shown(target['user'])}@{_shown(target['host'])}:{_shown(str(target.get('port') or 22))} "
        f"({_shown(target['name'])})",
        f"Host: {_shown(identity['hostname'])} machine-id={_shown(identity['machine_id'])} "
        f"host key {_shown(identity['hostkey'])}",
        f"GPU: {_shown(gpu['name'])} [{_shown(gpu['pci_id'])}]",
        f"CPU: {_shown(cpu['model'])}, {_shown(str(cpu['threads']))} threads; "
        f"memory {_shown(str(provision['memory_gib']))} GiB",
        f"Portage: MAKEOPTS=-j{int(tuning['jobs'])}, EMERGE_DEFAULT_OPTS=--jobs={int(tuning['emerge_jobs'])} "
        f"--load-average={int(tuning['load_average'])}",
        "Packages: x11-drivers/nvidia-drivers (USE kernel-open dist-kernel), built for the running kernel",
        "Licenses accepted: " + ", ".join(_shown(name) for name in provision["accept_licenses"])
        + " (only for x11-drivers/nvidia-drivers)",
        "Reboot: required afterwards; the installer never reboots",
    ]
    return "\n".join(lines)
```

and change the start of `approval_summary` to:

```python
def approval_summary(plan: dict) -> str:
    plan = _stored_plan(plan)
    if plan_kind(plan) == "provision":
        return _provision_summary(plan)
    target = plan["target"]
```

(keep the rest of the function as it is).

- [ ] **Step 4: Run the approval tests**

Run: `.venv/bin/python -m unittest tests.test_provision_approval tests.test_approval tests.test_core_purity -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/approval.py tests/test_provision_approval.py
git commit -m "Show what a provision plan changes in the approval summary"
```

---

### Task 9: Launcher and worker handle provision plans

**Files:**
- Modify: `gentoo_agent/jobs/launcher.py`
- Modify: `gentoo_agent/jobs/worker.py`
- Modify: `tests/test_launcher.py` (new test cases), `tests/test_worker.py` (new test case)

- [ ] **Step 1: Write the failing launcher test**

Add to `tests/test_launcher.py`, inside `class LauncherTests`, after `test_start_resolves_passwords_first_and_hands_them_over_stdin` (the helpers `FakeProcess`, `approval`, `launcher`, `runs`, `json`, `patch`, and `temp_home` are already imported in that file):

```python
    def test_a_provision_plan_starts_without_passwords_and_counts_only_run_phases(self):
        from tests.provision_helpers import save_provision_plan

        process = FakeProcess()
        with temp_home(), patch.object(launcher.subprocess, "Popen", return_value=process):
            plan = save_provision_plan()
            approval.record_approval(plan, "cli")
            run_id = launcher.start_run(plan["plan_id"])
            record = runs.read_run(run_id)
        self.assertEqual(record["phase_count"], 6)  # gpu-online is verify-only
        self.assertEqual(json.loads(process.written.decode("utf-8")),
                         {"password_records": None, "generated_credentials": {}})

    def test_a_verify_only_phase_cannot_be_selected_for_a_run(self):
        from tests.provision_helpers import save_provision_plan

        with temp_home(), patch.object(launcher.subprocess, "Popen") as popen:
            plan = save_provision_plan()
            approval.record_approval(plan, "cli")
            with self.assertRaises(AgentError) as caught:
                launcher.start_run(plan["plan_id"], phase_ids=["gpu-online"])
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        popen.assert_not_called()
```

- [ ] **Step 2: Write the failing worker test**

Add to `tests/test_worker.py`, inside `class WorkerTests`, after the first test (`approval`, `runs`, `worker`, `patch`, and `temp_home` are already imported):

```python
    def test_a_provision_plan_runs_without_password_records(self):
        from tests.provision_helpers import save_provision_plan

        with temp_home():
            plan = save_provision_plan()
            approval.record_approval(plan, "cli")
            run_id = self.new_run(plan)
            outcome = {"status": "installed_pending_reboot", "authentication_applied": False}
            with patch.object(worker, "execute_plan", return_value=outcome) as execute:
                result = worker.run(run_id, {"password_records": None, "generated_credentials": {}})
        self.assertEqual(result, outcome)
        self.assertIsNone(execute.call_args.args[2])
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_launcher tests.test_worker -v`
Expected: the new tests fail (`KeyError: 'install'` in `start_run`; `plan_needs_passwords` returns True).

- [ ] **Step 4: Implement the launcher change**

In `gentoo_agent/jobs/launcher.py` add `from gentoo_agent.core.install_plan import plan_kind` to the imports and change `start_run`:

```python
    plan = require_approved_plan(plan_id)
    runnable = [phase for phase in plan["phases"] if phase.get("run", True)]
    if phase_ids is not None:
        known = {phase["id"] for phase in runnable}
        unknown = sorted(set(phase_ids) - known)
        if unknown or not phase_ids:
            raise AgentError(CONFIG_INVALID, f"Unknown or empty phase selection: {unknown or list(phase_ids)}",
                             {"phase_ids": list(phase_ids)})
    if plan_kind(plan) == "install":
        install = plan["install"]
        password_variables = {
            str(name) for name in (install.get("root_password_env"), install.get("user_password_env")) if name
        }
        password_records, generated = password_input(install, prompt)
    else:
        # A provision plan sets no passwords.
        password_variables, password_records, generated = set(), None, {}
```

and in the `acquire_lock({...})` dict replace `"phase_count": len(phase_ids) if phase_ids else len(plan["phases"]),` with `"phase_count": len(phase_ids) if phase_ids else len(runnable),`.

- [ ] **Step 5: Implement the worker change**

In `gentoo_agent/jobs/worker.py` add `from gentoo_agent.core.install_plan import plan_kind` and change:

```python
def plan_needs_passwords(plan: dict) -> bool:
    if plan_kind(plan) != "install":
        return False
    install = plan.get("install") or {}
    return bool(install.get("set_root_password", True) or install.get("create_user", True))
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m unittest tests.test_launcher tests.test_worker tests.test_execute_plan tests.test_core_purity -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add gentoo_agent/jobs/launcher.py gentoo_agent/jobs/worker.py tests/test_launcher.py tests/test_worker.py
git commit -m "Start provision runs without passwords and count only the phases that run"
```

---

### Task 10: MCP tools

**Files:**
- Modify: `gentoo_agent/mcp/server.py`
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_mcp_server.py` add `"detect_provision", "create_provision_plan",` to `EXPECTED_TOOLS`, add this helper after `write_home`:

```python
def write_provision_home(home):
    from gentoo_agent.core.config import save_json
    from tests.provision_helpers import provision_inventory

    save_json(home / "config" / "target.json", {
        "name": "gentoo-server2", "host": "192.0.2.10", "port": 22, "user": "root", "identity_file": "~/.ssh/id_test",
        "target_is_disposable": False, "disk_erase_authorized": False,
    })
    save_json(home / "inventory" / "provision-inventory-20261019T120000Z.json", provision_inventory())
```

and this class at the end of the file:

```python
@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class ProvisionToolTests(unittest.TestCase):
    def test_detect_provision_summarizes_the_saved_inventory(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_provision_home(home)
            result = call("detect_provision")
        self.assertFalse(result.is_error, result.content[0].text)
        data = result.structured_content
        self.assertEqual(data["identity"]["hostname"], "gentoo")
        self.assertEqual(data["cpu"]["threads"], 24)
        self.assertEqual(data["gpus"][0]["vendor"], "nvidia")
        self.assertEqual(data["memory_gib"], 31)

    def test_detect_provision_without_an_inventory(self):
        from tests.helpers import temp_home

        with temp_home():
            result = call("detect_provision")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "INVENTORY_MISSING")

    def test_create_provision_plan_then_get_plan(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_provision_home(home)
            created = call("create_provision_plan", {"accept_licenses": ["NVIDIA-r2"]})
            shown = call("get_plan", {"plan_id": created.structured_content["plan_id"]})
        self.assertFalse(created.is_error, created.content[0].text)
        data = created.structured_content
        self.assertEqual(data["kind"], "provision")
        self.assertIn("Licenses accepted: NVIDIA-r2", data["summary"])
        self.assertEqual(data["disks"], [])
        self.assertEqual(data["provision"]["tuning"]["jobs"], 15)
        self.assertEqual(data["identity"]["hostname"], "gentoo")
        self.assertEqual(shown.structured_content["plan_hash"], data["plan_hash"])
        self.assertFalse(shown.structured_content["approval"]["approved"])

    def test_create_provision_plan_refuses_missing_licenses(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_provision_home(home)
            result = call("create_provision_plan", {"accept_licenses": []})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")

    def test_verify_install_runs_the_provision_checks(self):
        from gentoo_agent.core.install_plan import load_install_plan
        from tests.helpers import temp_home

        seen = []

        def fake_shell(target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
            seen.append(command)
            return {"label": label, "command": command, "returncode": 0, "stdout": "ok\n", "stderr": "",
                    "finished_at": "t"}

        with temp_home() as home, patch("gentoo_agent.mcp.server.remote_shell", fake_shell):
            write_provision_home(home)
            plan_id = call("create_provision_plan", {"accept_licenses": ["NVIDIA-r2"]}).structured_content["plan_id"]
            plan = load_install_plan(plan_id)
            result = call("verify_install", {"plan_id": plan_id})
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(seen, [phase["check"] for phase in plan["phases"]])
        self.assertEqual(len(result.structured_content["phases"]), 7)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_mcp_server -v`
Expected: the tool-set test and the new tests fail (unknown tool `detect_provision`).

- [ ] **Step 3: Implement**

In `gentoo_agent/mcp/server.py`:

1. Imports: add `provision_phases as core_provision_phases, provision_plan as core_provision_plan` to the `from gentoo_agent.core import paths, phases as core_phases` line; extend the install plan import to `from gentoo_agent.core.install_plan import create_install_plan, load_install_plan, plan_kind, save_install_plan`; extend the inventory import with `collect_provision_inventory, latest_provision_inventory`; add `from gentoo_agent.core.provision_facts import summarize_provision_inventory`.

2. Replace `plan_view` with:

```python
def plan_view(plan: dict) -> dict:
    kind = plan_kind(plan)
    view = {
        "plan_id": plan["plan_id"],
        "kind": kind,
        "plan_hash": plan["plan_hash"],
        "created_at": plan["created_at"],
        "summary": approval_summary(plan),
        "target": plan["target"],
        "disks": plan.get("disks", []),
        "dual_boot_entries": (plan.get("install") or {}).get("dual_boot_entries", []),
        "erases_existing_os": plan.get("erases_existing_os"),
        "secure_boot": plan.get("secure_boot"),
        "warnings": plan.get("warnings", []),
        "phases": [{"id": phase["id"], "destructive": phase["destructive"]} for phase in plan["phases"]],
        "approval": approval_status(plan["plan_id"]),
    }
    if kind == "provision":
        view["identity"] = plan["identity"]
        view["provision"] = plan["provision"]
    return view
```

3. After the `create_plan` tool add:

```python
@server.tool(annotations=REFRESHABLE_READ)
def detect_provision(refresh: bool = False) -> CallToolResult:
    """Summarize the running, installed system for provisioning: host identity, CPU threads, memory, GPUs,
    kernel, and the Portage settings a plan would replace. refresh=true runs the read-only SSH inventory
    first; otherwise the latest saved provision inventory is used."""
    def run() -> dict:
        if refresh:
            inventory, _ = collect_provision_inventory(target_config())
        else:
            inventory = latest_provision_inventory()
            if inventory is None:
                raise AgentError(INVENTORY_MISSING, "No provision inventory exists; call detect_provision with refresh=true.", {})
        return summarize_provision_inventory(inventory)
    return respond(run)


@server.tool(annotations=LOCAL_WRITE)
def create_provision_plan(accept_licenses: list[str], profile: str = "nvidia-driver") -> CallToolResult:
    """Freeze a provision plan for the running system: Portage tuning for all CPU threads and the NVIDIA
    driver. accept_licenses lists the license names the plan accepts; they are accepted only if a human
    approves the plan. The plan erases no disk. Returns plan_id, plan_hash, and a summary for the human."""
    def run() -> dict:
        plan = core_provision_plan.create_provision_plan(
            target_config(), {"profile": profile, "accept_licenses": accept_licenses}, latest_provision_inventory())
        return plan_view(save_install_plan(plan))
    return respond(run)
```

4. In `_refuse_prompt` replace `load_install_plan(plan_id)["install"].get("password_source")` with `(load_install_plan(plan_id).get("install") or {}).get("password_source")`.

5. Change the `start_install` docstring to: `"""Start the approved plan in the background. An install plan erases the target disk; a provision plan changes the installed system and erases nothing. Returns run_id."""`.

6. In `verify_install` replace the check generation inside the first `for phase in plan["phases"]:` loop with:

```python
            # Only run checks this code would generate for the plan; never trust the stored text alone.
            if plan_kind(plan) == "provision":
                expected = core_provision_phases.check_command_for(phase["id"], plan["target"], plan["provision"])
            else:
                expected = core_phases.check_command_for(phase["id"], plan["target"], plan["install"])
```

- [ ] **Step 4: Run the MCP tests**

Run: `.venv/bin/python -m unittest tests.test_mcp_server tests.test_mcp_stdio -v`
Expected: all pass. If an existing test compares the exact key set of an install `plan_view`, add `"kind"` to its expectation; no other install behavior changes.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/mcp/server.py tests/test_mcp_server.py
git commit -m "Add MCP tools to detect and plan provisioning of a running system"
```

---

### Task 11: CLI commands

**Files:**
- Modify: `gentoo_agent/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
class ProvisionCommandTests(unittest.TestCase):
    def write_home(self, home):
        save_json(home / "config" / "target.json", {
            "name": "gentoo-server2", "host": "192.0.2.10", "port": 22, "user": "root",
            "identity_file": "~/.ssh/id_test", "target_is_disposable": False, "disk_erase_authorized": False})
        from tests.provision_helpers import provision_inventory
        save_json(home / "inventory" / "provision-inventory-20261019T120000Z.json", provision_inventory())

    def test_provision_plan_saves_a_plan_and_prints_the_summary(self):
        stdout = io.StringIO()
        with temp_home() as home:
            self.write_home(home)
            with redirect_stdout(stdout):
                code = cli.main(["provision", "plan", "--accept-license", "NVIDIA-r2"])
            plans = list((home / "plans").glob("*.json"))
        self.assertEqual(code, 0)
        self.assertEqual(len(plans), 1)
        self.assertIn("Kind: provision", stdout.getvalue())
        self.assertIn("Licenses accepted: NVIDIA-r2", stdout.getvalue())
        self.assertIn(f"gentoo-agent approve {plans[0].stem}", stdout.getvalue())

    def test_provision_plan_needs_a_license(self):
        stderr = io.StringIO()
        with temp_home() as home, redirect_stderr(stderr):
            self.write_home(home)
            with self.assertRaises(SystemExit) as caught:
                cli.main(["provision", "plan"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_provision_inspect_prints_the_summary_and_the_inventory_path(self):
        from tests.provision_helpers import provision_inventory

        stdout = io.StringIO()
        with temp_home() as home:
            self.write_home(home)
            fake = (provision_inventory(), home / "inventory" / "provision-inventory-20261019T120000Z.json")
            with patch.object(cli, "collect_provision_inventory", return_value=fake), redirect_stdout(stdout):
                code = cli.main(["provision", "inspect"])
        self.assertEqual(code, 0)
        self.assertIn('"threads": 24', stdout.getvalue())
        self.assertIn("provision-inventory-20261019T120000Z.json", stdout.getvalue())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_cli.ProvisionCommandTests -v`
Expected: argparse error `invalid choice: 'provision'` (exit code 2).

- [ ] **Step 3: Implement**

In `gentoo_agent/cli.py` extend the imports:

```python
from gentoo_agent.core.inventory import (
    collect_inventory, collect_provision_inventory, latest_inventory, latest_provision_inventory, record_inventory,
)
from gentoo_agent.core.provision_facts import summarize_provision_inventory
from gentoo_agent.core.provision_plan import create_provision_plan
```

(keep the existing names that line imported), add the two commands after `cmd_plan_show`:

```python
def cmd_provision_inspect(_: argparse.Namespace) -> int:
    target = target_config()
    inventory, path = collect_provision_inventory(
        target, on_progress=lambda label: print(f"Inspecting {label}...", file=sys.stderr))
    print(json.dumps(summarize_provision_inventory(inventory), indent=2))
    print(path)
    return 0


def cmd_provision_plan(args: argparse.Namespace) -> int:
    settings = {"profile": args.profile, "accept_licenses": args.accept_license}
    plan = save_install_plan(create_provision_plan(target_config(), settings, latest_provision_inventory()))
    print(approval_summary(plan))
    print(f"\nPlan saved: {plan['plan_id']}")
    print(f"Approve it yourself in a terminal: gentoo-agent approve {plan['plan_id']}")
    return 0
```

and in `build_parser`, after the `plan` sub-commands block:

```python
    provision = sub.add_parser("provision", help="Inspect and plan changes to an installed, running system")
    provision_sub = provision.add_subparsers(dest="provision_command", required=True)
    provision_sub.add_parser(
        "inspect", help="Run the allowlisted read-only SSH inventory of the running system"
    ).set_defaults(func=cmd_provision_inspect)
    provision_plan = provision_sub.add_parser("plan", help="Freeze a provision plan for approval")
    provision_plan.add_argument("--profile", default="nvidia-driver")
    provision_plan.add_argument("--accept-license", action="append", required=True,
                                help="A license the plan accepts (repeat for several)")
    provision_plan.set_defaults(func=cmd_provision_plan)
```

- [ ] **Step 4: Run the CLI tests and the whole suite**

Run: `.venv/bin/python -m unittest tests.test_cli -v` then `.venv/bin/python -m unittest discover -s tests`
Expected: all pass; the install golden files are untouched (`git status tests/golden` shows nothing).

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/cli.py tests/test_cli.py
git commit -m "Add provision inspect and provision plan to the CLI"
```

---

### Task 12: Documentation and the spec

**Files:**
- Create: `docs/guides/provision.md`
- Modify: `docs/superpowers/specs/2026-10-19-provision-plans-nvidia-design.md`, `README.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md`, `docs/TROUBLESHOOTING.md`, `docs/ROADMAP.md`

- [ ] **Step 1: Write the guide**

Create `docs/guides/provision.md`:

```markdown
# Provisioning an installed system

A provision plan changes a Gentoo system that is already installed and running: Portage tuning for all
CPU threads and the NVIDIA driver (open kernel modules). It uses the same approval as an install plan,
never touches a disk, and never reboots.

## Prerequisites

- The system was installed by this tool, booted, and reachable over SSH as `root` with the installed key.
- The SSH host key changed when the installed system replaced the live ISO. Verify the new fingerprint
  on the target console (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`), then run
  `ssh-keygen -R <host>` and connect once to accept it. Until then every command stops with
  `HOST_KEY_UNKNOWN`.
- Fast Boot is off in the firmware if the machine dual boots Windows.
- You know which licenses Portage requires for the driver: run `emerge --pretend
  --autounmask-license=y x11-drivers/nvidia-drivers` on the target and read the license names it lists.

## Steps

1. `gentoo-agent provision inspect` (MCP: `detect_provision` with `refresh=true`). Check the CPU threads,
   memory, GPU, and the Portage settings the plan would replace.
2. `gentoo-agent provision plan --accept-license NVIDIA-r2` (repeat the option for every license; the
   name here is an example, use what Portage reports). MCP: `create_provision_plan`.
3. Read the summary. You accept the licenses by approving: `gentoo-agent approve <plan_id>`.
4. `gentoo-agent run start <plan_id>`, then `run status` and `run log`.
5. Reboot the machine yourself when the run ends in `installed_pending_reboot`.
6. After the reboot, `verify_install` (MCP) runs every check, including `gpu-online`: the module is
   loaded, `nvidia-smi` lists the card, and `nvidia_drm` modesetting is on.

## What the plan changes

- A block in `/etc/portage/make.conf` (`MAKEOPTS`, `EMERGE_DEFAULT_OPTS`) between the markers
  `# gentoo-agent portage-tuning begin` and `end`. The jobs are the smaller of the thread count and the RAM
  in GiB divided by 2; the load average equals the thread count.
- `/etc/portage/package.license/gentoo-agent-nvidia` and `/etc/portage/package.use/gentoo-agent-nvidia`.
- A block in `/etc/default/grub` between `# gentoo-agent nvidia begin` and `end` that adds
  `nvidia-drm.modeset=1`.
- `eselect kernel set` for the running kernel, then `emerge x11-drivers/nvidia-drivers` and a new
  `grub.cfg`.

Remove a change by deleting its file or block. Nothing is rolled back automatically.

## Resume

There is no partitioning in a provision plan, so `run resume <run_id>` continues after an interrupted run:
completed phases are checked and skipped.
```

- [ ] **Step 2: Update the other documents**

- `docs/ARCHITECTURE.md`: add a section "Plan kinds" stating that `plans/<id>.json` has `kind` (`install` or `provision`, missing means `install`), that a provision plan holds `identity` (host key fingerprint, machine id, hostname), `provision`, and phases with `run`, that the runner guards with `check_identity` instead of `check_drift`, and that `run: false` phases are checked by `verify_install` only. List the new modules (`identity.py`, `provision.py`, `provision_facts.py`, `provision_phases.py`, `provision_plan.py`).
- `docs/CONFIGURATION.md`: add a "Provision settings" section: `profile` (`nvidia-driver`), `accept_licenses` (1 to 8 distinct license names matching `[A-Za-z0-9][A-Za-z0-9+_.-]{0,63}`), and the derived fields `gpu`, `cpu`, `memory_gib`, `tuning` (`jobs`, `emerge_jobs`, `load_average`) with the rule that derives them.
- `docs/TROUBLESHOOTING.md`: add sections "Host key changed after the installation" (the two commands from the guide), "TARGET_DRIFT on a provision plan" (the host or its machine id differs: another server or a reinstall; create a new plan), and "emerge fails with a license or USE error in a provision run" (read the output tail, extend `accept_licenses`, create a new plan).
- `docs/ROADMAP.md`: under "Inferred Portage settings" note that `MAKEOPTS` and `EMERGE_DEFAULT_OPTS` are now derived by the provision plan; `CPU_FLAGS_X86`, `-march=native`, and `VIDEO_CARDS` stay with plan 4.
- `README.md`: one sentence in the status paragraph that provision plans change an installed system (Portage tuning and the NVIDIA driver), linking `docs/guides/provision.md`.
- `CLAUDE.md`: add a bullet under Architecture for the plan kinds and the new modules, and add `provision inspect` and `provision plan` to the commands that touch the target and are operator actions.
- The spec: apply the four decisions from the top of this plan (explicit license list, `/etc/default/grub` block, the `portage-tuning` phase, the `gpu-online` verify-only phase with `run: false`) and add `cpu`, `memory`, and `portage_conf` to the inventory section. The `gpu` command reads `/sys/bus/pci/devices` instead of `lspci` (Amendment 1): replace the `lspci -nnk` wording in the spec and drop `provision_state`.

- [ ] **Step 3: Check the text and run everything**

Run: `grep -c $'\xe2\x80\x94' README.md CLAUDE.md docs/guides/provision.md docs/ARCHITECTURE.md docs/CONFIGURATION.md docs/TROUBLESHOOTING.md docs/ROADMAP.md docs/superpowers/specs/2026-10-19-provision-plans-nvidia-design.md` (every count must be 0), then `.venv/bin/python -m unittest discover -s tests`.
Expected: all counts 0; the full suite passes.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md docs
git commit -m "Document provision plans, the Portage tuning, and the NVIDIA driver"
```

---

### Task 13: Verification on the real server (manual, with the operator)

This task confirms the assumptions that tests cannot check. Do not start a run before the first four steps are done and their output is saved.

- [ ] **Step 1: Replace the SSH host key (the operator, see the guide).** Then confirm: `ssh -o BatchMode=yes root@10.0.1.17 true` exits 0.
- [ ] **Step 2: Read the real settings.** `detect_provision` with `refresh=true`. Record the CPU threads, the memory, the GPU name and PCI id, and the `portage_overrides`. If `gpus` is empty or has no `nvidia` entry, stop: save the raw `gpu` result from the `provision-inventory-*.json` file and compare it with the parser (the sysfs format `<address> 0x<class> 0x<vendor> 0x<device>`); fix `parse_gpus` with a test built from the real output. The summary names the card only as `NVIDIA display controller [10de:xxxx]`, so check the PCI id against the card you expect (RTX 5070).
- [ ] **Step 3: Confirm the tuning rule against the Handbook.** Fetch `https://wiki.gentoo.org/wiki/Handbook:AMD64/Installation/Base` (the section on `MAKEOPTS`), record the URL, section, and retrieval time in `docs/guides/provision.md`, and compare the wording with `derive_tuning`. If the Handbook says something different, change the rule and its tests.
- [ ] **Step 4: Learn the licenses.** The operator runs `emerge --pretend --autounmask-license=y x11-drivers/nvidia-drivers` on the target and gives the license names it lists. Use exactly those names as `accept_licenses`.
- [ ] **Step 5: Create the plan and read the summary.** `create_provision_plan` with the licenses. The summary must show the real GPU, CPU, memory, and `MAKEOPTS`. Check that `MAKEOPTS` equals the smaller of the thread count and half the memory in GiB.
- [ ] **Step 6: Approve (the operator) and run.** Watch `get_run_status`. On a failure read the log tail: the assumptions to look at first are listed below.
- [ ] **Step 7: Reboot (the operator), then `verify_install`.** All seven phases must report complete, including `gpu-online`. Record `nvidia-smi` output and the console state (distorted or not) in the guide's troubleshooting notes.
- [ ] **Step 8: Merge and push the work** as the operator approved for earlier fixes, then update the memory note about the server.

Assumptions that are checked only here (adjust the generators and tests if one is wrong):

1. `portageq envvar MAKEOPTS` and `portageq envvar EMERGE_DEFAULT_OPTS` print the effective values after the `make.conf` block is written.
2. `*/* <license>` is accepted syntax in `/etc/portage/package.license/`.
3. `eselect kernel set linux-<uname -r>` names the installed sources directory.
4. `modprobe -c` lists `blacklist nouveau` once the driver is installed (the ebuild blacklists it).
5. `nvidia-smi` is found under `/opt/bin` for a non-login `sh -c` session.
6. `sed -i` with the marker pattern removes only the plan's own block, and `/etc/default/grub` is read by `grub-mkconfig`.
7. `emerge-webrsync` refreshes `/var/db/repos/gentoo/metadata/timestamp.chk`.
8. The open module builds against `/usr/src/linux-<release>-gentoo-dist-bin` with `dist-kernel` set.

---

## Self-review against the spec

- Plan kind, identity guard, approval summary, MCP and CLI: Tasks 2, 6, 7, 8, 10, 11.
- Read-only inventory (`host_identity`, `gpu`, plus `cpu`, `memory`, `portage_conf`, `kernel`): Task 1 and 3.
- Phases (`preflight`, `portage-tuning`, `portage-sync`, `nvidia-config`, `nvidia-drivers`, `nvidia-verify`, `gpu-online`): Task 5.
- No disk touched, never reboots: the scan test in Task 5.
- Resume without storage-style problems: no `storage` phase exists in a provision plan; the runner tests in Task 7 cover skipping completed phases.
- Verification before and after the reboot: `nvidia-verify` (run) and `gpu-online` (verify only), Tasks 5 and 7.
- Facts that the wiki does not cover are listed in Task 13 as things to confirm on the server.
- Install plans stay unchanged: Task 11 Step 4 requires the install golden files to be untouched.
