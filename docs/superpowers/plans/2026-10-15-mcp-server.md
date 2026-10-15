# MCP Server Implementation Plan (Plan 3 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the installer as a stdio MCP server an AI client drives end to end (discover, resolve config, plan, approve through elicitation, run, monitor), and make approval a real barrier with signed approval records plus an opt-in OS sandbox profile for the agent.

**Architecture:** `gentoo_agent/mcp/server.py` is a thin `MCPServer` (mcp 2.2.0) adapter over `core/` and `jobs/launcher`. Every tool returns a `CallToolResult` with `structured_content`; errors carry `{code, message, details}` and `is_error`. Approval records gain an HMAC-SHA256 signature with a key in `secrets/approval.key`; only the human's CLI and the MCP server (both outside the agent's sandbox) can read it. `core/resolve.py` turns the AI's `overrides`/`fallbacks` into a validated install config with value sources. An operator profile (Claude Code sandbox `denyWrite`/`denyRead` plus permission deny rules) keeps the agent's shell and file tools away from `approvals/`, `plans/`, `runs/`, and `secrets/`.

**Tech Stack:** Python 3.12+, `mcp==2.2.0` (optional extra `[mcp]`), `pydantic` (via mcp), `unittest`, `anyio` (via mcp) for async tests.

**Spec:** `docs/superpowers/specs/2026-10-15-mcp-server-design.md`, build order step 7. Decisions recorded 2026-10-15:
- Approval barrier: Claude Code sandbox for the agent's Bash plus HMAC-signed approval records (operator choice).
- Background runs and `approve`: macOS and Linux only.
- The MCP server never returns generated passwords to the model (they would land in the transcript); it reports that credentials are waiting and the human reads them with `gentoo-agent run status <run_id>` in their own terminal.
- The sandbox profile is opt-in (`docs/operator/claude-settings.json`), not committed project settings, because a project-wide sandbox would also restrict normal development sessions (network allowlist, writes). The barrier is only real when the MCP server runs from an installed copy the agent cannot modify (`docs/OPERATING.md`).
- Deny rules for top-level directories are root-anchored (`Edit(/plans/**)`): an unanchored `Edit(plans/**)` also matches nested directories such as `docs/superpowers/plans/`.

**Deviation from the spec:** tools return `CallToolResult` directly so errors can carry structured data; mcp 2.2.0 then publishes no `outputSchema` for them. `structured_content` is always present.

**Verified SDK facts used here (mcp 2.2.0):** `from mcp.server.mcpserver import MCPServer, Context`; `@server.tool(annotations=ToolAnnotations(read_only_hint=..., destructive_hint=..., idempotent_hint=..., open_world_hint=...), meta={...})` (decorator needs parentheses); `CallToolResult(content=[TextContent(type="text", text=...)], structured_content={...}, is_error=True)`; `await ctx.elicit(message, PydanticModel)` returning an object with `.action` in `accept|decline|cancel` and `.data`; `ctx.client_capabilities` (`None` or an object with `.elicitation`); tests use `from mcp import Client` and `async with Client(server, mode="legacy", elicitation_callback=cb) as client:` (without `mode="legacy"` elicitation fails in-process); a client without `elicitation_callback` advertises no elicitation; `ElicitResult(action="accept", content={...})` from `mcp.types`; `@server.resource("gentoo://plan/{plan_id}")`, `@server.prompt()`; results expose `is_error`, `structured_content`, `content`. Never `print()` in server code. If the installed SDK differs from any of these, keep the behavior, adapt the call, and report the difference.

**Environment for MCP tests:** core tests keep running with the system `python3`. MCP tests need the extra installed in a project venv:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[mcp]'
.venv/bin/python -m unittest discover -s tests -v
```

MCP test modules skip themselves when `mcp` is not importable, so `python3 -m unittest discover -s tests` still passes without the venv.

---

## File structure after this plan

```
pyproject.toml                     + optional extra mcp, script gentoo-agent-mcp
gentoo_agent/
  core/
    paths.py                       + secrets_dir()
    signing.py          (new)      approval signing key and HMAC
    approval.py                    records carry a signature
    layout.py           (new)      partition layout from disk, boot mode, swap
    resolve.py          (new)      overrides > profile > inferred > fallbacks > defaults
    hardware.py         (new)      inventory summary for discovery
    runner.py                      execute_plan(..., phase_ids=None)
  jobs/
    launcher.py                    start_run(..., phase_ids=None)
    worker.py                      passes phase_ids from the run record
  mcp/
    __init__.py         (new)
    server.py           (new)      MCPServer: tools, resources, prompt
  init_config.py, prepare.py       use core/layout.py
.mcp.json.example       (new)
docs/OPERATING.md       (new)      protected operation mode
docs/operator/claude-settings.json (new)  sandbox and deny profile
tests/
  test_signing.py, test_layout.py, test_resolve.py, test_hardware.py,
  test_mcp_server.py (skips without mcp), test_operator_profile.py
```

---

### Task 1: Signed approval records

An approval record is only valid with an HMAC-SHA256 signature made with `secrets/approval.key`. Writers (`record_approval`, called by the human's CLI and the MCP server) create the key on first use; the verifier never creates it.

**Files:**
- Modify: `gentoo_agent/core/paths.py`, `gentoo_agent/core/approval.py`, `.gitignore`
- Create: `gentoo_agent/core/signing.py`, `tests/test_signing.py`
- Modify: `tests/test_approval.py`

- [ ] **Step 1: Write the failing signing tests**

`tests/test_signing.py`:

```python
import unittest

from gentoo_agent.core import approval, paths, signing
from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.errors import APPROVAL_REQUIRED, AgentError
from tests.helpers import temp_home
from tests.test_approval import START, make_plan


class SigningKeyTests(unittest.TestCase):
    def test_key_is_created_once_with_private_permissions(self):
        with temp_home():
            first = signing.load_or_create_key()
            second = signing.load_or_create_key()
            path = signing.key_path()
            self.assertEqual(first, second)
            self.assertEqual(len(first), 64)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.parent, paths.secrets_dir())

    def test_load_key_never_creates(self):
        with temp_home():
            self.assertIsNone(signing.load_key())
            self.assertFalse(signing.key_path().exists())

    def test_malformed_key_is_rejected(self):
        with temp_home():
            signing.key_path().parent.mkdir(mode=0o700, parents=True)
            signing.key_path().write_text("short", encoding="ascii")
            with self.assertRaises(AgentError) as caught:
                signing.load_key()
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_sign_and_verify(self):
        key = b"a" * 64
        fields = {"plan_id": "x", "n": 1}
        signature = signing.sign(fields, key)
        self.assertTrue(signing.verify(fields, signature, key))
        self.assertFalse(signing.verify({**fields, "n": 2}, signature, key))
        self.assertFalse(signing.verify(fields, signature, b"b" * 64))
        self.assertFalse(signing.verify(fields, None, key))


class SignedApprovalTests(unittest.TestCase):
    def test_recorded_approval_is_signed_and_accepted(self):
        with temp_home():
            plan = make_plan()
            record = approval.record_approval(plan, "cli", at=START)
            self.assertEqual(len(record["signature"]), 64)
            approval.require_approved_plan(plan["plan_id"], at=START)

    def test_tampered_record_is_rejected(self):
        with temp_home():
            plan = make_plan()
            approval.record_approval(plan, "cli", at=START)
            path = approval.approval_path(plan["plan_id"])
            record = load_json(path)
            record["channel"] = "elicitation"
            save_json(path, record)
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_forged_record_without_the_key_is_rejected(self):
        with temp_home():
            plan = make_plan()
            signing.load_or_create_key()
            unsigned = {
                "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"], "channel": "cli",
                "approved_at": START.isoformat(), "expires_at": "2026-10-15T13:00:00+00:00",
            }
            save_json(approval.approval_path(plan["plan_id"]),
                      {**unsigned, "signature": signing.sign(unsigned, b"f" * 64)})
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_missing_key_invalidates_existing_records(self):
        with temp_home():
            plan = make_plan()
            approval.record_approval(plan, "cli", at=START)
            signing.key_path().unlink()
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
            self.assertFalse(signing.key_path().exists())
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_signing -v`
Expected: ERROR, `ImportError: cannot import name 'signing'`.

- [ ] **Step 3: Add `secrets_dir()` to `gentoo_agent/core/paths.py`**

```python
def secrets_dir() -> Path:
    return root() / "secrets"
```

Add `secrets/` to `.gitignore`.

- [ ] **Step 4: Create `gentoo_agent/core/signing.py`**

```python
"""HMAC signatures for approval records.

The key lives in secrets/approval.key (directory 0700, file 0600). Only the
human's CLI and the MCP server write approval records, and both run outside
the agent's sandbox; the operator profile denies the agent's shell and file
tools any access to secrets/. Verification never creates a key.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from gentoo_agent.core import paths
from gentoo_agent.core.errors import APPROVAL_REQUIRED, AgentError
from gentoo_agent.core.install_plan import canonical


KEY_FILE = "approval.key"
KEY_LENGTH = 64
HEX_DIGITS = set("0123456789abcdef")


def key_path() -> Path:
    return paths.secrets_dir() / KEY_FILE


def _read(path: Path) -> bytes:
    try:
        value = path.read_text(encoding="ascii").strip()
    except UnicodeDecodeError:
        value = ""
    if len(value) != KEY_LENGTH or not set(value) <= HEX_DIGITS:
        raise AgentError(
            APPROVAL_REQUIRED,
            "The approval signing key is malformed; remove secrets/approval.key and approve again.",
            {},
        )
    return value.encode("ascii")


def load_key() -> bytes | None:
    try:
        return _read(key_path())
    except FileNotFoundError:
        return None


def load_or_create_key() -> bytes:
    existing = load_key()
    if existing is not None:
        return existing
    path = key_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read(path)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(secrets.token_hex(KEY_LENGTH // 2))
    return _read(path)


def sign(fields: dict, key: bytes) -> str:
    return hmac.new(key, canonical(fields), hashlib.sha256).hexdigest()


def verify(fields: dict, signature: object, key: bytes) -> bool:
    return isinstance(signature, str) and hmac.compare_digest(sign(fields, key), signature)
```

- [ ] **Step 5: Sign and verify records in `gentoo_agent/core/approval.py`**

- Import `from gentoo_agent.core.signing import load_key, load_or_create_key, sign, verify`.
- `RECORD_KEYS` gains `"signature"`.
- In `record_approval`, build the record without the signature exactly as today, then before saving:

```python
    record["signature"] = sign(record, load_or_create_key())
```

  (`sign` is computed while `record` does not yet contain `signature`.)
- In `_validated_record`, right after the key-set check (`set(record) != RECORD_KEYS`), verify the signature over the record without its `signature` field:

```python
    key = load_key()
    unsigned = {name: value for name, value in record.items() if name != "signature"}
    if key is None or not verify(unsigned, record["signature"], key):
        raise invalid()
```

  `load_key()` may raise `AgentError(APPROVAL_REQUIRED)` for a malformed key; let it propagate.
- Update the module docstring: records are signed; only processes that can read `secrets/` can create valid ones.

- [ ] **Step 6: Update the existing approval tests that hand-write records**

In `tests/test_approval.py`, every test that writes a record with `save_json` to exercise a later check (wrong `plan_hash`, far-future expiry, wrong `plan_id`, unknown channel, future `approved_at`, and similar) must re-sign it so the check under test is the one that fails. Add this helper and use it in those tests:

```python
def save_signed(plan_id: str, record: dict) -> None:
    from gentoo_agent.core import signing

    unsigned = {name: value for name, value in record.items() if name != "signature"}
    save_json(approval.approval_path(plan_id),
              {**unsigned, "signature": signing.sign(unsigned, signing.load_or_create_key())})
```

`test_approval_for_another_hash_is_rejected` must still expect `PLAN_HASH_MISMATCH` (sign the record carrying the other hash). Tests that write malformed records (non-JSON, `[]`, missing or extra keys) keep expecting `APPROVAL_REQUIRED` unchanged; an extra-key test must now treat `signature` as a required key.

- [ ] **Step 7: Run all tests**

Run: `python3 -m unittest discover -s tests -v`
Expected: all pass (9 new). Golden files unchanged.

- [ ] **Step 8: Commit**

```bash
git add -A gentoo_agent/core tests .gitignore
git commit -m "Sign approval records with a key the agent cannot read"
```

---

### Task 2: Partition layout in core

`init_config.py` and `prepare.py` each build the same partition layout. Move it to `core/layout.py` so the resolver can use it.

**Files:**
- Create: `gentoo_agent/core/layout.py`, `tests/test_layout.py`
- Modify: `gentoo_agent/init_config.py`, `gentoo_agent/prepare.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_layout.py`:

```python
import unittest

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.layout import build_layout, partition_device


class LayoutTests(unittest.TestCase):
    def test_partition_device_names(self):
        self.assertEqual(partition_device("/dev/sda", 2), "/dev/sda2")
        self.assertEqual(partition_device("/dev/nvme0n1", 1), "/dev/nvme0n1p1")

    def test_uefi_layout(self):
        table, layout = build_layout("/dev/nvme0n1", "uefi", 4096)
        self.assertEqual(table, "gpt")
        self.assertEqual(layout, [
            {"device": "/dev/nvme0n1p1", "size_mib": 512, "type": "efi-system", "filesystem": "vfat", "mount": "/boot"},
            {"device": "/dev/nvme0n1p2", "size_mib": 4096, "type": "linux-swap", "mount": None},
            {"device": "/dev/nvme0n1p3", "size": "remaining", "type": "linux-root", "filesystem": "ext4", "mount": "/"},
        ])

    def test_bios_layout(self):
        table, layout = build_layout("/dev/sda", "bios", 2048)
        self.assertEqual(table, "mbr")
        self.assertEqual([item["device"] for item in layout], ["/dev/sda1", "/dev/sda2"])
        self.assertEqual(layout[0]["type"], "linux-swap")

    def test_invalid_inputs(self):
        for args in (("/dev/sda", "efi", 1024), ("/dev/sda", "bios", -1), ("/dev/sda", "bios", True)):
            with self.subTest(args=args), self.assertRaises(AgentError) as caught:
                build_layout(*args)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_layout -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'gentoo_agent.core.layout'`.

- [ ] **Step 3: Create `gentoo_agent/core/layout.py`**

```python
"""Partition layout derived from disk, boot mode, and swap size."""

from __future__ import annotations

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError


EFI_SIZE_MIB = 512


def partition_device(disk: str, number: int) -> str:
    """Partition path for both /dev/sda and /dev/nvme0n1 styles."""
    return f"{disk}{'p' if disk[-1:].isdigit() else ''}{number}"


def build_layout(disk: str, boot_mode: str, swap_mb: int, root_filesystem: str = "ext4") -> tuple[str, list[dict]]:
    if boot_mode not in {"uefi", "bios"}:
        raise AgentError(CONFIG_INVALID, "boot_mode must be 'uefi' or 'bios'", {"field": "boot_mode"})
    if not isinstance(swap_mb, int) or isinstance(swap_mb, bool) or swap_mb < 0:
        raise AgentError(CONFIG_INVALID, "swap_mb must be a non-negative integer", {"field": "swap_mb"})
    entries: list[dict] = []
    if boot_mode == "uefi":
        entries.append({"size_mib": EFI_SIZE_MIB, "type": "efi-system", "filesystem": "vfat", "mount": "/boot"})
    entries.append({"size_mib": swap_mb, "type": "linux-swap", "mount": None})
    entries.append({"size": "remaining", "type": "linux-root", "filesystem": root_filesystem, "mount": "/"})
    layout = [{"device": partition_device(disk, number), **entry} for number, entry in enumerate(entries, start=1)]
    return ("gpt" if boot_mode == "uefi" else "mbr"), layout
```

- [ ] **Step 4: Use it in `init_config.py` and `prepare.py`**

In `gentoo_agent/init_config.py`, delete the local `partition_device` and replace the `if args.boot_mode == "uefi": ... else: ...` layout block with:

```python
    partition_table, layout = build_layout(args.disk, args.boot_mode, args.swap_mb, args.root_filesystem)
```

In `gentoo_agent/prepare.py`, delete the local `partition_device` and replace the layout block in `build_install_config` with:

```python
    partition_table, layout = build_layout(disk_path, boot_mode, swap_mb, root_filesystem)
```

Import `from gentoo_agent.core.layout import build_layout` in both. The produced dicts must be identical to the old ones (same keys, values, and order); compare by running the old and new code for both boot modes before deleting the old block.

- [ ] **Step 5: Run all tests and smoke-test init_config**

Run: `python3 -m unittest discover -s tests -v` (all pass, 4 new) and `python3 tools/init_config.py --help`.

- [ ] **Step 6: Commit**

```bash
git add gentoo_agent tests/test_layout.py
git commit -m "Build partition layouts in core"
```

---

### Task 3: Config resolution

`resolve_config` merges, highest first: `overrides` (from the user's request), `profile` (optional dict), values inferred from the inventory, `fallbacks`, and `config/install.default.json`. It records the source of every value, rejects plain-text passwords, and returns a validated install config.

**Files:**
- Create: `gentoo_agent/core/resolve.py`, `tests/test_resolve.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_resolve.py`:

```python
import json
import unittest
from pathlib import Path

from gentoo_agent.core.errors import (
    CONFIG_CONFLICT,
    CONFIG_INVALID,
    DISK_AMBIGUOUS,
    INVENTORY_MISSING,
    AgentError,
)
from gentoo_agent.core.resolve import resolve_config


REPO = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
          "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True}
DEFAULTS = json.loads((REPO / "config" / "install.default.json").read_text(encoding="utf-8"))


def inventory(boot: str = "uefi", disks: tuple[str, ...] = ("/dev/nvme0n1",)) -> dict:
    devices = [{"path": path, "type": "disk", "size": 512110190592, "model": "NVMe", "serial": f"S{index}", "wwn": None}
               for index, path in enumerate(disks)]
    return {"results": [
        {"label": "boot_mode", "returncode": 0, "stdout": boot if boot == "uefi" else "bios-or-unknown"},
        {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": devices})},
    ]}


def resolve(overrides=None, fallbacks=None, **kwargs):
    return resolve_config(TARGET, overrides or {}, fallbacks or {}, DEFAULTS, kwargs.get("inv", inventory()),
                          profile=kwargs.get("profile"))


class ResolveTests(unittest.TestCase):
    def test_spec_example(self):
        result = resolve(
            {"username": "Alex", "user_password": {"env": "ALEX_PW"}, "boot_mode": "uefi", "bootloader": "grub"},
            {"username": "gentoo", "hostname": "server", "timezone": "Europe/Prague"},
        )
        install, sources = result["install"], result["sources"]
        self.assertEqual(install["username"], "alex")
        self.assertIn("username Alex was lowercased to alex", result["warnings"])
        self.assertEqual(install["user_password_env"], "ALEX_PW")
        self.assertEqual(install["password_source"], "environment")
        self.assertEqual(install["hostname"], "server")
        self.assertEqual(sources["hostname"], "fallback")
        self.assertEqual(sources["username"], "override")
        self.assertEqual(sources["user_password_env"], "override")
        self.assertEqual(install["disk"], "/dev/nvme0n1")
        self.assertEqual(sources["disk"], "inferred")
        self.assertEqual(install["partition_table"], "gpt")
        self.assertEqual(install["partition_layout"][0]["device"], "/dev/nvme0n1p1")
        self.assertTrue(install["disk_must_be_erased"])
        self.assertNotIn("user_password", install)
        self.assertNotIn("user_password", sources)

    def test_precedence(self):
        result = resolve({"hostname": "a"}, {"hostname": "c"}, profile={"hostname": "b"})
        self.assertEqual(result["install"]["hostname"], "a")
        result = resolve({}, {"hostname": "c"}, profile={"hostname": "b"})
        self.assertEqual((result["install"]["hostname"], result["sources"]["hostname"]), ("b", "profile"))
        result = resolve({}, {})
        self.assertEqual(result["sources"]["hostname"], "default")

    def test_plain_text_password_is_rejected(self):
        for value in ("secret", {"env": "lowercase"}, {"value": "x"}, {"env": "OK", "extra": 1}):
            with self.subTest(value=value), self.assertRaises(AgentError) as caught:
                resolve({"user_password": value})
            self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_passwords_only_in_overrides_or_profile(self):
        with self.assertRaises(AgentError) as caught:
            resolve({}, {"root_password": {"env": "ROOT_PW"}})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"partition_layout": []})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(caught.exception.details["keys"], ["partition_layout"])

    def test_requested_boot_mode_must_match_the_live_system(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"boot_mode": "uefi"}, inv=inventory(boot="bios"))
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)
        self.assertEqual(caught.exception.details, {"requested": "uefi", "live": "bios"})

    def test_boot_mode_is_inferred_from_the_live_system(self):
        result = resolve({}, inv=inventory(boot="bios", disks=("/dev/sda",)))
        self.assertEqual(result["install"]["boot_mode"], "bios")
        self.assertEqual(result["sources"]["boot_mode"], "inferred")
        self.assertEqual(result["install"]["partition_table"], "mbr")

    def test_several_disks_need_a_choice(self):
        with self.assertRaises(AgentError) as caught:
            resolve({}, inv=inventory(disks=("/dev/sda", "/dev/sdb")))
        self.assertEqual(caught.exception.code, DISK_AMBIGUOUS)
        self.assertEqual(caught.exception.details["candidates"], ["/dev/sda", "/dev/sdb"])
        result = resolve({"disk": "/dev/sdb"}, inv=inventory(disks=("/dev/sda", "/dev/sdb")))
        self.assertEqual(result["install"]["disk"], "/dev/sdb")

    def test_missing_inventory(self):
        with self.assertRaises(AgentError) as caught:
            resolve_config(TARGET, {}, {}, DEFAULTS, None)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_unsupported_bootloader(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"bootloader": "systemd-boot"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_erase_flags_come_from_the_target(self):
        result = resolve_config({**TARGET, "disk_erase_authorized": False}, {}, {}, DEFAULTS, inventory())
        self.assertFalse(result["install"]["disk_erase_authorized"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_resolve -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'gentoo_agent.core.resolve'`.

- [ ] **Step 3: Create `gentoo_agent/core/resolve.py`**

```python
"""Resolve an install configuration from layered inputs.

Priority, highest first: overrides (from the user's request), profile,
values inferred from the inventory, fallbacks, and defaults. The result
records the source of every value. Passwords are accepted only as
environment-variable references, never as text.
"""

from __future__ import annotations

import copy
import re

from gentoo_agent.core.disks import parse_disk_identities
from gentoo_agent.core.errors import (
    CONFIG_CONFLICT,
    CONFIG_INVALID,
    DISK_AMBIGUOUS,
    INVENTORY_MISSING,
    AgentError,
)
from gentoo_agent.core.layout import build_layout
from gentoo_agent.core.validate import validated_settings


SETTABLE = {
    "username", "hostname", "timezone", "boot_mode", "init_system", "swap_mb", "disk",
    "network_method", "network_static", "create_user", "configure_sudo", "set_root_password",
    "allow_root_password_ssh", "install_user_ssh_key", "bootloader", "user_groups",
}
PASSWORD_FIELDS = {"user_password": "user_password_env", "root_password": "root_password_env"}
DERIVED = {
    "disk", "partition_layout", "partition_table", "disk_must_be_erased", "target_is_disposable",
    "disk_erase_authorized", "expected_disk_model", "expected_disk_size_gib",
}
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
SUPPORTED_BOOTLOADERS = {"grub"}


def _check_layer(name: str, layer: object, allow_passwords: bool) -> dict:
    if layer is None:
        return {}
    if not isinstance(layer, dict):
        raise AgentError(CONFIG_INVALID, f"{name} must be an object", {"layer": name})
    allowed = SETTABLE | (set(PASSWORD_FIELDS) if allow_passwords else set())
    unknown = sorted(set(layer) - allowed)
    if unknown:
        raise AgentError(CONFIG_INVALID, f"{name} contains settings that cannot be set: {', '.join(unknown)}",
                         {"layer": name, "keys": unknown})
    for field in PASSWORD_FIELDS:
        if field in layer:
            value = layer[field]
            if not (isinstance(value, dict) and set(value) == {"env"} and isinstance(value["env"], str)
                    and ENV_NAME.fullmatch(value["env"])):
                raise AgentError(
                    CONFIG_INVALID,
                    'Passwords are passed by environment-variable name, never as text: {"env": "NAME"}',
                    {"field": field},
                )
    return layer


def _live_boot_mode(results: dict) -> str:
    stdout = str(results.get("boot_mode", {}).get("stdout", "")).strip()
    return "uefi" if stdout == "uefi" else "bios"


def resolve_config(
    target: dict,
    overrides: dict | None,
    fallbacks: dict | None,
    defaults: dict,
    inventory: dict | None,
    *,
    profile: dict | None = None,
) -> dict:
    overrides = _check_layer("overrides", overrides, allow_passwords=True)
    profile = _check_layer("profile", profile, allow_passwords=True)
    fallbacks = _check_layer("fallbacks", fallbacks, allow_passwords=False)
    if inventory is None:
        raise AgentError(INVENTORY_MISSING, "No inventory exists; run detect_hardware with refresh first.", {})
    results = {item.get("label"): item for item in inventory.get("results", [])}
    identity = results.get("disk_identity")
    if identity is None or identity.get("returncode") != 0:
        raise AgentError(INVENTORY_MISSING, "The latest inventory has no disk identity; refresh it.", {})
    disks = [disk["path"] for disk in parse_disk_identities(identity.get("stdout", ""))]
    live_boot = _live_boot_mode(results)

    inferred: dict = {"boot_mode": live_boot}
    if len(disks) == 1:
        inferred["disk"] = disks[0]

    base = {key: value for key, value in copy.deepcopy(defaults).items() if key not in DERIVED}
    layers = [("override", overrides), ("profile", profile), ("inferred", inferred),
              ("fallback", fallbacks), ("default", base)]
    install: dict = {}
    sources: dict = {}
    for name, layer in reversed(layers):
        for key, value in layer.items():
            install[key] = copy.deepcopy(value)
            sources[key] = name
    warnings: list[str] = []

    if install.get("bootloader", "grub") not in SUPPORTED_BOOTLOADERS:
        raise AgentError(CONFIG_INVALID, "Only the grub bootloader is supported.", {"field": "bootloader"})
    if sources.get("boot_mode") != "inferred" and install["boot_mode"] != live_boot:
        raise AgentError(
            CONFIG_CONFLICT,
            f"boot_mode {install['boot_mode']} was requested but the live system booted in {live_boot} mode.",
            {"requested": install["boot_mode"], "live": live_boot},
        )
    if "disk" not in install:
        if not disks:
            raise AgentError(INVENTORY_MISSING, "The inventory lists no disks.", {})
        raise AgentError(DISK_AMBIGUOUS, "Several disks are present; choose one with the disk setting.",
                         {"candidates": disks})

    username = install.get("username")
    if isinstance(username, str) and username != username.lower():
        warnings.append(f"username {username} was lowercased to {username.lower()}")
        install["username"] = username.lower()

    for field, env_field in PASSWORD_FIELDS.items():
        reference = install.pop(field, None)
        source = sources.pop(field, None)
        if reference is not None:
            install[env_field] = reference["env"]
            install["password_source"] = "environment"
            install["generate_missing_passwords"] = True
            sources[env_field] = source

    install["partition_table"], install["partition_layout"] = build_layout(
        install["disk"], install["boot_mode"], install.get("swap_mb", 2048), install.get("root_filesystem", "ext4")
    )
    install["disk_must_be_erased"] = True
    install["target_is_disposable"] = target.get("target_is_disposable") is True
    install["disk_erase_authorized"] = target.get("disk_erase_authorized") is True
    for key in ("partition_table", "partition_layout", "disk_must_be_erased", "target_is_disposable",
                "disk_erase_authorized"):
        sources[key] = "derived"

    validated_settings(target, install)
    return {"install": install, "sources": sources, "warnings": warnings}
```

- [ ] **Step 4: Run all tests**

Run: `python3 -m unittest discover -s tests -v` (all pass, 11 new). `tests/test_core_purity.py` must pass.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/resolve.py tests/test_resolve.py
git commit -m "Resolve install config from overrides, profile, inventory, fallbacks, and defaults"
```

---

### Task 4: Phase subsets for runs

The `run_phase` tool runs selected phases of an approved plan. The launcher validates and records the phase list in the run; the worker passes it to `execute_plan`, which runs only those phases, in plan order.

**Files:**
- Modify: `gentoo_agent/core/runner.py`, `gentoo_agent/jobs/launcher.py`, `gentoo_agent/jobs/worker.py`
- Modify: `tests/test_execute_plan.py`, `tests/test_launcher.py`, `tests/test_worker.py`

- [ ] **Step 1: Write the failing tests**

Append to `ExecutePlanTests` in `tests/test_execute_plan.py`:

```python
    def test_phase_subset_runs_only_selected_phases_in_plan_order(self):
        with temp_home():
            shell = FakeShell({"kernel-check": 1, "preflight-check": 1})
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "running",
                                    "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift"):
                result = runner.execute_plan(PLAN, run_id, RECORDS, phase_ids=["kernel", "preflight"])
        self.assertEqual(result["completed_phases"], ["preflight", "kernel"])
        self.assertNotIn("authentication-check", [call[0] for call in shell.calls])

    def test_unknown_phase_id_is_rejected_before_any_command(self):
        from gentoo_agent.core.errors import CONFIG_INVALID

        with temp_home():
            shell = FakeShell()
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "status": "running", "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift"):
                with self.assertRaises(AgentError) as caught:
                    runner.execute_plan(PLAN, run_id, RECORDS, phase_ids=["reboot"])
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(shell.calls, [])
```

Append to `LauncherTests` in `tests/test_launcher.py`:

```python
    def test_phase_subset_is_validated_and_recorded(self):
        from gentoo_agent.core.errors import CONFIG_INVALID

        with temp_home(), patch.object(launcher.subprocess, "Popen", return_value=FakeProcess()) as popen:
            plan = approved_plan()
            with self.assertRaises(AgentError) as caught:
                launcher.start_run(plan["plan_id"], phase_ids=["reboot"])
            popen.assert_not_called()
            run_id = launcher.start_run(plan["plan_id"], phase_ids=["kernel"])
            record = runs.read_run(run_id)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(record["phase_ids"], ["kernel"])
        self.assertEqual(record["phase_count"], 1)
```

Append to `WorkerTests` in `tests/test_worker.py` a test that creates a run whose record includes `"phase_ids": ["kernel"]` (in the record given to `runs.acquire_lock`), patches `worker.execute_plan`, runs `worker.run`, and asserts `execute.call_args.kwargs["phase_ids"] == ["kernel"]`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_execute_plan tests.test_launcher tests.test_worker -v`
Expected: the new tests fail with `TypeError: ... unexpected keyword argument 'phase_ids'` or a missing record key.

- [ ] **Step 3: Implement**

`gentoo_agent/core/runner.py`: add a keyword parameter `phase_ids: list[str] | None = None` to `execute_plan`. Right after the claimed-status check and before the first `update_run`, select the phases (and remove the earlier `phases = plan["phases"]` assignment):

```python
    phases = plan["phases"]
    if phase_ids is not None:
        known = {phase["id"] for phase in phases}
        unknown = sorted(set(phase_ids) - known)
        if unknown or not phase_ids:
            raise AgentError(CONFIG_INVALID, f"Unknown or empty phase selection: {unknown or list(phase_ids)}",
                             {"phase_ids": list(phase_ids)})
        selected = set(phase_ids)
        phases = [phase for phase in phases if phase["id"] in selected]
```

`gentoo_agent/jobs/launcher.py`: add `phase_ids: list[str] | None = None` to `start_run`. After `require_approved_plan`, validate it the same way against `plan["phases"]` (raise `CONFIG_INVALID` before resolving passwords or taking the lock). Record `"phase_ids": phase_ids` and `"phase_count": len(phase_ids) if phase_ids else len(plan["phases"])`. `resume_run` passes `phase_ids=record.get("phase_ids")`.

`gentoo_agent/jobs/worker.py`: call `execute_plan(plan, run_id, password_records, phase_ids=record.get("phase_ids"))`.

- [ ] **Step 4: Run all tests**

Run: `python3 -m unittest discover -s tests -v` (all pass, 4 new).

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent tests
git commit -m "Run selected phases of an approved plan"
```

---

### Task 5: MCP dependency, hardware summary, and server skeleton

**Files:**
- Modify: `pyproject.toml`, `tests/test_core_purity.py`
- Create: `gentoo_agent/mcp/__init__.py`, `gentoo_agent/mcp/server.py`, `gentoo_agent/core/hardware.py`, `tests/test_hardware.py`, `tests/test_mcp_server.py`

- [ ] **Step 1: Add the optional dependency and the script**

In `pyproject.toml` add:

```toml
[project.optional-dependencies]
mcp = ["mcp==2.2.0"]
```

and under `[project.scripts]`:

```toml
gentoo-agent-mcp = "gentoo_agent.mcp.server:main"
```

Create the venv: `python3 -m venv .venv` and `.venv/bin/pip install -e '.[mcp]'`. Confirm: `.venv/bin/python -c "import mcp.server.mcpserver; print('ok')"`.

- [ ] **Step 2: Write the failing hardware summary test**

`tests/test_hardware.py`:

```python
import json
import unittest

from gentoo_agent.core.hardware import summarize_inventory


class HardwareSummaryTests(unittest.TestCase):
    def test_summary(self):
        disks = [{"path": "/dev/sda", "type": "disk", "size": 100, "model": "QEMU", "serial": "", "wwn": None}]
        inventory = {"target": "gentoo", "collected_at": "2026-10-15T12:00:00+00:00", "results": [
            {"label": "boot_mode", "returncode": 0, "stdout": "uefi"},
            {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": disks})},
        ]}
        summary = summarize_inventory(inventory)
        self.assertEqual(summary["boot_mode"], "uefi")
        self.assertEqual(summary["collected_at"], "2026-10-15T12:00:00+00:00")
        self.assertEqual(summary["disks"][0]["strength"], "weak")
        self.assertEqual(len(summary["inventory_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
```

Run it; expected `ModuleNotFoundError`.

- [ ] **Step 3: Create `gentoo_agent/core/hardware.py`**

```python
"""Hardware summary for discovery. Plan 4 extends this with CPU, GPU, NIC, and module data."""

from __future__ import annotations

import hashlib

from gentoo_agent.core.disks import identity_strength, parse_disk_identities
from gentoo_agent.core.install_plan import canonical


def summarize_inventory(inventory: dict) -> dict:
    results = {item.get("label"): item for item in inventory.get("results", [])}
    boot = str(results.get("boot_mode", {}).get("stdout", "")).strip()
    identity = results.get("disk_identity") or {}
    disks = parse_disk_identities(identity.get("stdout", "")) if identity.get("returncode") == 0 else []
    return {
        "target": inventory.get("target"),
        "collected_at": inventory.get("collected_at"),
        "inventory_hash": hashlib.sha256(canonical(inventory)).hexdigest(),
        "boot_mode": "uefi" if boot == "uefi" else "bios",
        "disks": [{**disk, "strength": identity_strength(disk)} for disk in disks],
    }
```

- [ ] **Step 4: Write the failing server skeleton test**

`tests/test_mcp_server.py`:

```python
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

HAS_MCP = importlib.util.find_spec("mcp") is not None

if HAS_MCP:
    import anyio
    from mcp import Client

    from gentoo_agent.mcp import server as mcp_server

REPO = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "detect_hardware", "list_phases", "resolve_config", "create_plan", "get_plan", "approve_plan",
    "start_install", "run_phase", "resume_run", "cancel_run", "get_run_status", "get_run_log",
    "verify_install",
}


def call(name, arguments=None, *, elicitation_callback=None):
    async def go():
        kwargs = {"mode": "legacy"}
        if elicitation_callback is not None:
            kwargs["elicitation_callback"] = elicitation_callback
        async with Client(mcp_server.server, **kwargs) as client:
            return await client.call_tool(name, arguments or {})
    return anyio.run(go)


def write_home(home, *, boot="uefi", disks=("/dev/nvme0n1",)):
    from gentoo_agent.core.config import save_json

    save_json(home / "config" / "target.json", {
        "name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root", "identity_file": "~/.ssh/id_test",
        "target_is_disposable": True, "disk_erase_authorized": True,
    })
    save_json(home / "config" / "plan.json", json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8")))
    save_json(home / "config" / "install.default.json",
              json.loads((REPO / "config" / "install.default.json").read_text(encoding="utf-8")))
    devices = [{"path": path, "type": "disk", "size": 512110190592, "model": "NVMe", "serial": f"S{i}", "wwn": None}
               for i, path in enumerate(disks)]
    save_json(home / "inventory" / "inventory-20261015T120000Z.json", {"target": "gentoo", "collected_at": "x", "results": [
        {"label": "boot_mode", "returncode": 0, "stdout": boot},
        {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": devices})},
    ]})


@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class SkeletonTests(unittest.TestCase):
    def test_tools_are_registered_with_annotations(self):
        async def go():
            async with Client(mcp_server.server, mode="legacy") as client:
                return await client.list_tools()
        tools = {tool.name: tool for tool in anyio.run(go).tools}
        self.assertEqual(set(tools), EXPECTED_TOOLS)
        self.assertFalse(tools["detect_hardware"].annotations.read_only_hint)
        self.assertTrue(tools["list_phases"].annotations.read_only_hint)
        self.assertTrue(tools["start_install"].annotations.destructive_hint)
        self.assertTrue(tools["get_run_log"].annotations.read_only_hint)

    def test_errors_are_structured(self):
        from tests.helpers import temp_home

        with temp_home():
            result = call("get_plan", {"plan_id": "../x"})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")

    def test_list_phases(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            result = call("list_phases")
        ids = [phase["id"] for phase in result.structured_content["phases"]]
        self.assertIn("storage", ids)


if __name__ == "__main__":
    unittest.main()
```

Run `.venv/bin/python -m unittest tests.test_mcp_server -v`; expected `ModuleNotFoundError: No module named 'gentoo_agent.mcp'`.

- [ ] **Step 5: Create the package and the skeleton**

`gentoo_agent/mcp/__init__.py`:

```python
"""MCP front end. Requires the optional extra: pip install 'gentoo-agent[mcp]'."""
```

`gentoo_agent/mcp/server.py` registers all thirteen tools now. `list_phases`, `get_plan`, and `get_run_log` are implemented here; the others return a `not implemented` error until Tasks 6 to 8 replace them:

```python
"""MCP server: a thin adapter over gentoo_agent.core and gentoo_agent.jobs.

Never write to stdout: it carries the protocol. Every tool returns a
CallToolResult with structured_content; errors carry {code, message, details}
and is_error. Approval is recorded only after a human accepts an elicitation
dialog built by this server, and the server never returns passwords.
"""

from __future__ import annotations

import json
import logging

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from gentoo_agent.core.approval import approval_status, approval_summary
from gentoo_agent.core.config import load_plan
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.install_plan import load_install_plan
from gentoo_agent.core.runs import read_log


logger = logging.getLogger("gentoo_agent.mcp")

INSTRUCTIONS = (
    "Installs Gentoo on a disposable target over SSH. Workflow: detect_hardware(refresh=true), "
    "resolve_config to preview, create_plan, approve_plan (a human confirms in a dialog), "
    "start_install, then get_run_status and get_run_log until the run ends. Never ask for or "
    "pass passwords as text; reference environment variables with {\"env\": \"NAME\"}."
)

server = MCPServer("gentoo-agent", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
REMOTE_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
HUMAN_CONFIRMS = {"anthropic/requiresUserInteraction": True}


def ok_result(data: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, indent=2))], structured_content=data)


def error_result(error: AgentError) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"{error.code}: {error.message}")],
                          structured_content=error.to_dict(), is_error=True)


def respond(function, *args, **kwargs) -> CallToolResult:
    try:
        return ok_result(function(*args, **kwargs))
    except AgentError as error:
        return error_result(error)


def plan_view(plan: dict) -> dict:
    return {
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "created_at": plan["created_at"],
        "summary": approval_summary(plan),
        "target": plan["target"],
        "disks": plan["disks"],
        "phases": [{"id": phase["id"], "destructive": phase["destructive"]} for phase in plan["phases"]],
        "approval": approval_status(plan["plan_id"]),
    }


def _not_implemented() -> CallToolResult:
    return error_result(AgentError(CONFIG_INVALID, "not implemented", {}))


@server.tool(annotations=READ_ONLY)
def list_phases() -> CallToolResult:
    """List the installation phases, their order, and which ones change the target."""
    return respond(lambda: {"phases": [
        {"id": item["id"], "description": item.get("description", ""),
         "destructive": bool(item.get("destructive", True)), "handbook_sections": item.get("handbook_sections", [])}
        for item in load_plan().get("phases", [])
    ]})


@server.tool(annotations=READ_ONLY)
def get_plan(plan_id: str) -> CallToolResult:
    """Show a stored plan, its summary, and its approval status."""
    return respond(lambda: plan_view(load_install_plan(plan_id)))


@server.tool(annotations=READ_ONLY)
def get_run_log(run_id: str, offset: int = 0, limit: int = 50) -> CallToolResult:
    """Read part of a run's log (offset and limit count log lines)."""
    return respond(read_log, run_id, offset, limit)


# Placeholders, replaced in Tasks 6 to 8.
@server.tool(annotations=LOCAL_WRITE)
def detect_hardware(refresh: bool = False) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=READ_ONLY)
def resolve_config(overrides: dict | None = None, fallbacks: dict | None = None) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=LOCAL_WRITE)
def create_plan(overrides: dict | None = None, fallbacks: dict | None = None) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=LOCAL_WRITE)
async def approve_plan(plan_id: str, ctx: Context) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def start_install(plan_id: str) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def run_phase(plan_id: str, phases: list[str]) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def resume_run(run_id: str) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=LOCAL_WRITE)
def cancel_run(run_id: str) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=READ_ONLY)
def get_run_status(run_id: str) -> CallToolResult:
    return _not_implemented()


@server.tool(annotations=REMOTE_READ)
def verify_install(plan_id: str) -> CallToolResult:
    return _not_implemented()


def main() -> int:
    logging.basicConfig(level=logging.INFO)  # stderr; stdout carries the protocol
    server.run()
    return 0
```

The `BaseModel` import is used by Task 7.

- [ ] **Step 6: Extend the purity guard to the MCP package**

In `tests/test_core_purity.py`, add `test_mcp_package_never_prints_or_exits`: for every `gentoo_agent/mcp/*.py`, report calls to `print`/`input`/`breakpoint`, any `SystemExit` name, and `sys.exit`/`os._exit` attribute calls, using the existing AST walk (do not apply the core module-import rules there).

- [ ] **Step 7: Run all tests**

Run: `.venv/bin/python -m unittest discover -s tests -v` (all pass) and `python3 -m unittest discover -s tests` (all pass; MCP tests skipped).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml gentoo_agent tests
git commit -m "Add the MCP server skeleton with structured results"
```

---

### Task 6: Discovery and planning tools

**Files:**
- Modify: `gentoo_agent/mcp/server.py`, `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_mcp_server.py`)

```python
@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class DiscoveryAndPlanningTests(unittest.TestCase):
    def test_detect_hardware_summarizes_the_latest_inventory(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            result = call("detect_hardware", {})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["disks"][0]["path"], "/dev/nvme0n1")

    def test_detect_hardware_without_inventory_asks_for_refresh(self):
        from gentoo_agent.core.config import save_json
        from tests.helpers import temp_home

        with temp_home() as home:
            save_json(home / "config" / "target.json", {"name": "gentoo", "host": "192.0.2.10", "user": "root"})
            result = call("detect_hardware", {})
        self.assertEqual(result.structured_content["code"], "INVENTORY_MISSING")

    def test_resolve_config_preview_never_writes(self):
        from gentoo_agent.core import paths
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            result = call("resolve_config", {"overrides": {"username": "Alex"}, "fallbacks": {"hostname": "server"}})
            plans_exist = paths.plans_dir().exists()
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(result.structured_content["install"]["username"], "alex")
        self.assertEqual(result.structured_content["sources"]["hostname"], "fallback")
        self.assertFalse(plans_exist)

    def test_create_plan_then_get_plan(self):
        from tests.helpers import temp_home

        with temp_home() as home, patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            write_home(home)
            created = call("create_plan", {"overrides": {"user_password": {"env": "ALEX_PW"}}})
            plan_id = created.structured_content["plan_id"]
            shown = call("get_plan", {"plan_id": plan_id})
        self.assertFalse(created.is_error, created.content[0].text)
        self.assertEqual(shown.structured_content["plan_hash"], created.structured_content["plan_hash"])
        self.assertFalse(shown.structured_content["approval"]["approved"])
        self.assertIn("serial=S0", created.structured_content["summary"])
        self.assertEqual(created.structured_content["sources"]["user_password_env"], "override")

    def test_plain_text_password_is_refused(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            result = call("create_plan", {"overrides": {"user_password": "hunter2"}})
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")
        self.assertNotIn("hunter2", result.content[0].text)
```

Run `.venv/bin/python -m unittest tests.test_mcp_server -v`; the new tests fail with `not implemented`.

- [ ] **Step 2: Implement the tools**

Replace the `detect_hardware`, `resolve_config`, and `create_plan` placeholders:

```python
def _resolved(overrides: dict | None, fallbacks: dict | None) -> tuple[dict, dict | None, dict]:
    target = target_config()
    defaults = load_json(paths.config_dir() / "install.default.json")
    inventory = latest_inventory()
    return target, inventory, resolve_install(target, overrides, fallbacks, defaults, inventory)


@server.tool(annotations=LOCAL_WRITE)
def detect_hardware(refresh: bool = False) -> CallToolResult:
    """Summarize the target: firmware boot mode and disks with serial/WWN. refresh=true runs the
    read-only SSH inventory first; otherwise the latest saved inventory is used."""
    def run() -> dict:
        target = target_config()
        if refresh:
            inventory, path = collect_inventory(target)
            record_inventory(target, path)
        else:
            inventory = latest_inventory()
            if inventory is None:
                raise AgentError(INVENTORY_MISSING, "No inventory exists; call detect_hardware with refresh=true.", {})
        return summarize_inventory(inventory)
    return respond(run)


@server.tool(annotations=READ_ONLY)
def resolve_config(overrides: dict | None = None, fallbacks: dict | None = None) -> CallToolResult:
    """Preview the merged install config (overrides > inferred > fallbacks > defaults) with the
    source of every value. Writes nothing. Passwords only as {"env": "NAME"}."""
    return respond(lambda: _resolved(overrides, fallbacks)[2])


@server.tool(annotations=LOCAL_WRITE)
def create_plan(overrides: dict | None = None, fallbacks: dict | None = None) -> CallToolResult:
    """Freeze the resolved config, the disk identity, and every generated command into a plan.
    Same inputs as resolve_config. Returns plan_id, plan_hash, and a summary for the human."""
    def run() -> dict:
        target, inventory, resolved = _resolved(overrides, fallbacks)
        plan = save_install_plan(create_install_plan(target, resolved["install"], load_plan(), inventory))
        return {**plan_view(plan), "sources": resolved["sources"], "warnings": resolved["warnings"]}
    return respond(run)
```

Add imports: `from gentoo_agent.core import paths`; `from gentoo_agent.core.config import load_json, load_plan, target_config`; `INVENTORY_MISSING` from errors; `from gentoo_agent.core.hardware import summarize_inventory`; `from gentoo_agent.core.install_plan import create_install_plan, load_install_plan, save_install_plan`; `from gentoo_agent.core.inventory import collect_inventory, latest_inventory, record_inventory`; `from gentoo_agent.core.resolve import resolve_config as resolve_install`.

- [ ] **Step 3: Run all tests and commit**

Run: `.venv/bin/python -m unittest discover -s tests -v` (all pass).

```bash
git add gentoo_agent/mcp tests/test_mcp_server.py
git commit -m "Add MCP discovery and planning tools"
```

---

### Task 7: Approval through elicitation

`approve_plan` shows the human a dialog built by the server from the stored plan. Only an explicit accept with `approve: true` records an approval. A client without elicitation, or any elicitation failure, returns `APPROVAL_REQUIRED` with the CLI command; nothing is recorded.

**Files:**
- Modify: `gentoo_agent/mcp/server.py`, `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class ApprovalTests(unittest.TestCase):
    def make_plan(self, home):
        with patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            write_home(home)
            return call("create_plan", {}).structured_content["plan_id"]

    def test_accepted_dialog_records_an_approval(self):
        from mcp.types import ElicitResult
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        seen = []

        async def accept(context, params):
            seen.append(params.message)
            return ElicitResult(action="accept", content={"approve": True})

        with temp_home() as home:
            plan_id = self.make_plan(home)
            result = call("approve_plan", {"plan_id": plan_id}, elicitation_callback=accept)
            status = approval_status(plan_id)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertTrue(status["approved"])
        self.assertEqual(status["channel"], "elicitation")
        self.assertIn(plan_id, seen[0])
        self.assertIn("Disk erase: YES", seen[0])

    def test_declined_cancelled_or_unchecked_dialog_records_nothing(self):
        from mcp.types import ElicitResult
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        answers = [ElicitResult(action="decline"), ElicitResult(action="cancel"),
                   ElicitResult(action="accept", content={"approve": False})]
        for answer in answers:
            async def reply(context, params, answer=answer):
                return answer
            with self.subTest(answer=answer.action), temp_home() as home:
                plan_id = self.make_plan(home)
                result = call("approve_plan", {"plan_id": plan_id}, elicitation_callback=reply)
                status = approval_status(plan_id)
            self.assertEqual(result.structured_content["code"], "APPROVAL_DECLINED")
            self.assertFalse(status["approved"])

    def test_client_without_elicitation_gets_the_cli_command(self):
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        with temp_home() as home:
            plan_id = self.make_plan(home)
            result = call("approve_plan", {"plan_id": plan_id})
            status = approval_status(plan_id)
        self.assertEqual(result.structured_content["code"], "APPROVAL_REQUIRED")
        self.assertEqual(result.structured_content["details"]["command"], f"gentoo-agent approve {plan_id}")
        self.assertFalse(status["approved"])

    def test_failing_elicitation_fails_closed(self):
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        async def broken(context, params):
            raise RuntimeError("dialog crashed")

        with temp_home() as home:
            plan_id = self.make_plan(home)
            result = call("approve_plan", {"plan_id": plan_id}, elicitation_callback=broken)
            status = approval_status(plan_id)
        self.assertTrue(result.is_error)
        self.assertIn(result.structured_content["code"], {"APPROVAL_REQUIRED", "APPROVAL_DECLINED"})
        self.assertFalse(status["approved"])
```

- [ ] **Step 2: Implement**

Replace the `approve_plan` placeholder:

```python
class ApprovalAnswer(BaseModel):
    approve: bool


@server.tool(annotations=LOCAL_WRITE)
async def approve_plan(plan_id: str, ctx: Context) -> CallToolResult:
    """Ask the human, in a dialog built by this server, to approve a stored plan for 60 minutes.
    The model cannot answer the dialog. Without dialog support, returns the terminal command."""
    try:
        plan = load_install_plan(plan_id)
        summary = approval_summary(plan)
    except AgentError as error:
        return error_result(error)
    fallback = AgentError(
        APPROVAL_REQUIRED,
        f"Approve this plan yourself in a terminal: gentoo-agent approve {plan_id}",
        {"plan_id": plan_id, "command": f"gentoo-agent approve {plan_id}"},
    )
    capabilities = ctx.client_capabilities
    if capabilities is None or capabilities.elicitation is None:
        return error_result(fallback)
    message = (
        f"{summary}\n\nApprove this plan? The approval is valid for {APPROVAL_TTL_MINUTES} minutes "
        "and covers exactly the commands and disk above. Check 'approve' and accept only if you, "
        "the human operator, agree."
    )
    try:
        answer = await ctx.elicit(message, ApprovalAnswer)
    except Exception:  # unsupported transport, client error, or timeout: fail closed
        logger.exception("elicitation failed for plan %s", plan_id)
        return error_result(fallback)
    data = getattr(answer, "data", None)
    if answer.action != "accept" or data is None or data.approve is not True:
        return error_result(AgentError(APPROVAL_DECLINED, "Approval not recorded.", {"plan_id": plan_id}))
    try:
        record = record_approval(plan, "elicitation")
    except AgentError as error:
        return error_result(error)
    return ok_result({"approved": True, "plan_id": plan_id, "channel": "elicitation",
                      "approved_until": record["expires_at"]})
```

Add imports `APPROVAL_DECLINED, APPROVAL_REQUIRED` and `from gentoo_agent.core.approval import APPROVAL_TTL_MINUTES, approval_status, approval_summary, record_approval`. If the SDK raises `MCPError` for a failing callback in a way that escapes the `except Exception`, report it; the test `test_failing_elicitation_fails_closed` must pass either way.

- [ ] **Step 3: Run all tests and commit**

```bash
git add gentoo_agent/mcp tests/test_mcp_server.py
git commit -m "Approve plans through an elicitation dialog built by the server"
```

---

### Task 8: Execution and monitoring tools

`start_install`, `run_phase`, and `resume_run` go through `jobs.launcher` (and therefore `require_approved_plan`). `get_run_status` never returns passwords; it says when credentials are waiting for the human.

**Files:**
- Modify: `gentoo_agent/mcp/server.py`, `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class ExecutionTests(unittest.TestCase):
    def create(self, home, **install_changes):
        from gentoo_agent.core.config import load_json, save_json

        write_home(home)
        if install_changes:
            path = home / "config" / "install.default.json"
            save_json(path, {**load_json(path), **install_changes})
        with patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            return call("create_plan", {}).structured_content["plan_id"]

    def approve(self, plan_id):
        from gentoo_agent.core.approval import record_approval
        from gentoo_agent.core.install_plan import load_install_plan

        record_approval(load_install_plan(plan_id), "cli")

    def test_unapproved_plan_is_refused(self):
        from tests.helpers import temp_home

        with temp_home() as home, patch("gentoo_agent.jobs.launcher.subprocess.Popen") as popen:
            plan_id = self.create(home)
            result = call("start_install", {"plan_id": plan_id})
        self.assertEqual(result.structured_content["code"], "APPROVAL_REQUIRED")
        popen.assert_not_called()

    def test_start_and_status_never_return_passwords(self):
        from gentoo_agent.core import runs
        from tests.helpers import temp_home
        from tests.test_launcher import FakeProcess

        with temp_home() as home, patch("gentoo_agent.jobs.launcher.subprocess.Popen", return_value=FakeProcess()):
            plan_id = self.create(home, password_source="generate")
            self.approve(plan_id)
            started = call("start_install", {"plan_id": plan_id})
            run_id = started.structured_content["run_id"]
            runs.update_run(run_id, status="failed")
            runs.release_lock(run_id)
            runs.write_credentials(run_id, {"credentials": {"root": "s3cret"}, "applied": True})
            status = call("get_run_status", {"run_id": run_id})
            still_there = runs.credentials_path(run_id).exists()
        self.assertFalse(started.is_error, started.content[0].text)
        self.assertTrue(status.structured_content["credentials_waiting"])
        self.assertIn(f"gentoo-agent run status {run_id}", status.structured_content["credentials_hint"])
        self.assertNotIn("s3cret", json.dumps(status.structured_content) + status.content[0].text)
        self.assertTrue(still_there)

    def test_run_phase_records_the_selection(self):
        from gentoo_agent.core import runs
        from tests.helpers import temp_home
        from tests.test_launcher import FakeProcess

        with temp_home() as home, patch("gentoo_agent.jobs.launcher.subprocess.Popen", return_value=FakeProcess()):
            plan_id = self.create(home, password_source="generate")
            self.approve(plan_id)
            result = call("run_phase", {"plan_id": plan_id, "phases": ["kernel"]})
            record = runs.read_run(result.structured_content["run_id"])
        self.assertEqual(record["phase_ids"], ["kernel"])

    def test_prompt_password_source_is_refused(self):
        from tests.helpers import temp_home

        with temp_home() as home, patch("gentoo_agent.jobs.launcher.subprocess.Popen") as popen:
            plan_id = self.create(home, password_source="prompt")
            self.approve(plan_id)
            result = call("start_install", {"plan_id": plan_id})
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")
        popen.assert_not_called()

    def test_verify_install_runs_only_plan_checks(self):
        from gentoo_agent.core.install_plan import load_install_plan
        from tests.helpers import temp_home

        seen = []

        def fake_shell(target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
            seen.append((label, command, input_data))
            return {"label": label, "command": command, "returncode": 0, "stdout": "ok\n", "stderr": "",
                    "finished_at": "t"}

        with temp_home() as home, patch("gentoo_agent.mcp.server.remote_shell", fake_shell):
            plan_id = self.create(home)
            plan = load_install_plan(plan_id)
            result = call("verify_install", {"plan_id": plan_id})
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual([command for _, command, _ in seen], [phase["check"] for phase in plan["phases"]])
        self.assertTrue(all(data is None for _, _, data in seen))
        self.assertTrue(all(item["complete"] for item in result.structured_content["phases"]))
```

- [ ] **Step 2: Implement**

Replace the remaining placeholders:

```python
PROMPT_UNAVAILABLE = (
    "password_source 'prompt' cannot be used through MCP; reference environment variables or generate passwords."
)


def _refuse_prompt(plan_id: str) -> None:
    if load_install_plan(plan_id)["install"].get("password_source") == "prompt":
        raise AgentError(CONFIG_INVALID, PROMPT_UNAVAILABLE, {"field": "password_source"})


def _start(plan_id: str, phase_ids: list[str] | None = None) -> dict:
    _refuse_prompt(plan_id)
    run_id = start_run(plan_id, phase_ids=phase_ids)
    return {"run_id": run_id, "plan_id": plan_id, "status": "starting"}


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def start_install(plan_id: str) -> CallToolResult:
    """Start the approved plan in the background. Erases the target disk. Returns run_id."""
    return respond(_start, plan_id)


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def run_phase(plan_id: str, phases: list[str]) -> CallToolResult:
    """Run only the listed phases of an approved plan (in plan order), in the background."""
    return respond(_start, plan_id, phases)


@server.tool(annotations=DESTRUCTIVE, meta=HUMAN_CONFIRMS)
def resume_run(run_id: str) -> CallToolResult:
    """Start a new run of the same approved plan; completed phases are checked and skipped."""
    def run() -> dict:
        _refuse_prompt(read_run(run_id)["plan_id"])
        return {"run_id": launcher_resume(run_id), "resumed_from": run_id}
    return respond(run)


@server.tool(annotations=LOCAL_WRITE)
def cancel_run(run_id: str) -> CallToolResult:
    """Stop a run at the next phase boundary."""
    return respond(launcher_cancel, run_id)


@server.tool(annotations=READ_ONLY)
def get_run_status(run_id: str) -> CallToolResult:
    """Run status, phase index, current phase, and last error. Never returns passwords."""
    def run() -> dict:
        record = run_status(run_id)
        waiting = credentials_path(run_id).exists()
        result = {**record, "credentials_waiting": waiting}
        if waiting:
            result["credentials_hint"] = (
                f"Generated passwords are waiting. Read them yourself in a terminal: gentoo-agent run status {run_id}"
            )
        return result
    return respond(run)


@server.tool(annotations=REMOTE_READ)
def verify_install(plan_id: str) -> CallToolResult:
    """Run each phase's read-only check from the stored plan and report which are complete."""
    def run() -> dict:
        plan = load_install_plan(plan_id)
        phases = []
        for phase in plan["phases"]:
            try:
                result = remote_shell(plan["target"], f"{phase['id']}-verify-install", phase["check"], timeout=300)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AgentError(SSH_UNREACHABLE, f"Could not run the {phase['id']} check: {exc}",
                                 {"phase": phase["id"]}) from exc
            phases.append({"id": phase["id"], "complete": result["returncode"] == 0,
                           "output_tail": output_tail(result["stdout"] + result["stderr"])})
        return {"plan_id": plan_id, "phases": phases}
    return respond(run)
```

Add imports: `import subprocess`; `SSH_UNREACHABLE` from errors; `from gentoo_agent.core.runner import output_tail`; `from gentoo_agent.core.runs import credentials_path, read_log, read_run, run_status`; `from gentoo_agent.core.ssh import remote_shell`; `from gentoo_agent.jobs.launcher import cancel_run as launcher_cancel, resume_run as launcher_resume, start_run`.

- [ ] **Step 3: Run all tests and commit**

```bash
git add gentoo_agent/mcp tests/test_mcp_server.py
git commit -m "Add MCP execution and monitoring tools"
```

---

### Task 9: Resources and the workflow prompt

**Files:**
- Modify: `gentoo_agent/mcp/server.py`, `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing test** (append)

```python
@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class ResourceAndPromptTests(unittest.TestCase):
    def test_plan_resource_and_prompt(self):
        from tests.helpers import temp_home

        async def go(plan_id):
            async with Client(mcp_server.server, mode="legacy") as client:
                resource = await client.read_resource(f"gentoo://plan/{plan_id}")
                prompt = await client.get_prompt("install_gentoo", {"request": "user alex, UEFI"})
                return resource, prompt

        with temp_home() as home, patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            write_home(home)
            plan_id = call("create_plan", {}).structured_content["plan_id"]
            resource, prompt = anyio.run(go, plan_id)
        self.assertEqual(json.loads(resource.contents[0].text)["plan_id"], plan_id)
        text = prompt.messages[0].content.text
        self.assertIn("approve_plan", text)
        self.assertIn("user alex, UEFI", text)
```

- [ ] **Step 2: Implement**

```python
@server.resource("gentoo://plan/{plan_id}")
def plan_resource(plan_id: str) -> str:
    return json.dumps(plan_view(load_install_plan(plan_id)), indent=2)


@server.resource("gentoo://run/{run_id}/state")
def run_state_resource(run_id: str) -> str:
    return json.dumps(run_status(run_id), indent=2)


@server.resource("gentoo://run/{run_id}/log")
def run_log_resource(run_id: str) -> str:
    return json.dumps(read_log(run_id, 0, 500), indent=2)


@server.prompt()
def install_gentoo(request: str = "") -> str:
    return (
        "Install Gentoo on the configured disposable target.\n"
        f"User request: {request}\n\n"
        "1. detect_hardware(refresh=true). If several disks are listed, ask the user which one.\n"
        "2. Translate the request into overrides and fallbacks; passwords only as {\"env\": \"NAME\"}.\n"
        "3. resolve_config to preview; report conflicts to the user instead of guessing.\n"
        "4. create_plan, then approve_plan. Never try to approve any other way.\n"
        "5. start_install, then poll get_run_status and read get_run_log until the run ends.\n"
        "6. On failure, report the error code and output tail; resume_run only if the user agrees.\n"
        "7. Never reboot the target. If credentials are waiting, tell the user to read them in a terminal."
    )
```

Resources raise `AgentError` on bad ids; the SDK turns that into a protocol error, which is acceptable.

- [ ] **Step 3: Run all tests and commit**

```bash
git add gentoo_agent/mcp tests/test_mcp_server.py
git commit -m "Add MCP resources and the install_gentoo prompt"
```

---

### Task 10: Operator profile and project deny rules

**Files:**
- Modify: `.claude/settings.json`, `tests/test_cli.py`
- Create: `docs/operator/claude-settings.json`, `.mcp.json.example`, `docs/OPERATING.md`, `tests/test_operator_profile.py`

- [ ] **Step 1: Write the failing test**

`tests/test_operator_profile.py`:

```python
import json
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class OperatorProfileTests(unittest.TestCase):
    def test_sandbox_profile(self):
        profile = json.loads((REPO / "docs" / "operator" / "claude-settings.json").read_text(encoding="utf-8"))
        sandbox = profile["sandbox"]
        self.assertTrue(sandbox["enabled"])
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])
        for path in ("./approvals", "./plans", "./runs", "./secrets"):
            self.assertIn(path, sandbox["filesystem"]["denyWrite"])
        self.assertIn("./secrets", sandbox["filesystem"]["denyRead"])
        deny = profile["permissions"]["deny"]
        for rule in ("Read(/secrets/**)", "Edit(/approvals/**)", "Edit(/plans/**)", "Edit(/runs/**)"):
            self.assertIn(rule, deny)
        self.assertFalse(any(rule.startswith("Write(") for rule in deny))
        self.assertFalse(any(rule in deny for rule in ("Edit(plans/**)", "Edit(approvals/**)")))

    def test_project_settings_keep_secrets_and_credentials_away_from_file_tools(self):
        deny = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["deny"]
        for rule in ("Read(/secrets/**)", "Read(/runs/*/credentials.json)", "Read(/runs/*/.credentials.*)",
                     "Edit(/runs/**)", "Edit(/secrets/**)"):
            self.assertIn(rule, deny)

    def test_mcp_example(self):
        example = json.loads((REPO / ".mcp.json.example").read_text(encoding="utf-8"))
        server = example["mcpServers"]["gentoo-agent"]
        self.assertEqual(server["type"], "stdio")
        self.assertIn("GENTOO_AGENT_HOME", server["env"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Create the files**

`docs/operator/claude-settings.json`:

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "allowUnsandboxedCommands": false,
    "filesystem": {
      "denyWrite": ["./approvals", "./plans", "./runs", "./secrets"],
      "denyRead": ["./secrets"]
    }
  },
  "permissions": {
    "deny": [
      "Read(/secrets/**)",
      "Read(/runs/*/credentials.json)",
      "Read(/runs/*/.credentials.*)",
      "Edit(/approvals/**)",
      "Edit(/plans/**)",
      "Edit(/runs/**)",
      "Edit(/secrets/**)",
      "Bash(gentoo-agent approve:*)",
      "Bash(python -m gentoo_agent approve:*)",
      "Bash(python3 -m gentoo_agent approve:*)"
    ]
  }
}
```

`.mcp.json.example`:

```json
{
  "mcpServers": {
    "gentoo-agent": {
      "type": "stdio",
      "command": "/ABSOLUTE/PATH/TO/protected-venv/bin/gentoo-agent-mcp",
      "args": [],
      "env": {
        "GENTOO_AGENT_HOME": "/ABSOLUTE/PATH/TO/gentoo-agent-workdir"
      }
    }
  }
}
```

Append to the `deny` list in `.claude/settings.json`: `"Read(/secrets/**)"`, `"Read(/runs/*/credentials.json)"`, `"Read(/runs/*/.credentials.*)"`, `"Edit(/runs/**)"`, `"Edit(/secrets/**)"`. Keep every rule root-anchored.

`docs/OPERATING.md`: a short operator guide (no em dashes, no emoji) with these sections:
- **Why a protected mode exists:** the MCP server enforces approvals, so the agent must not be able to change the server's code, its data, or the signing key. In development mode (server run from this repository) the agent can edit `gentoo_agent/` and the barrier does not hold.
- **Install a protected copy:** `python3 -m venv ~/.local/share/gentoo-agent/venv`, then `~/.local/share/gentoo-agent/venv/bin/pip install 'gentoo-agent[mcp] @ git+https://github.com/Bajtlamer/gentoo-ai-installer-agent@<tag>'`. Create a work directory (for example `~/gentoo-agent-work`) containing `config/plan.json`, `config/install.default.json` (copied from the repository), and `config/target.json`.
- **Register the server:** copy `.mcp.json.example` to `.mcp.json` in the work directory (or use `claude mcp add`) and set the absolute venv command and `GENTOO_AGENT_HOME`.
- **Apply the sandbox profile:** run Claude Code from the work directory with `docs/operator/claude-settings.json` as user settings (`~/.claude/settings.json`) or, so it cannot be overridden, as managed settings. Also deny writes to the venv: add its absolute path to `sandbox.filesystem.denyWrite` and an `Edit(//ABSOLUTE/PATH/venv/**)` rule. Requires macOS (Seatbelt) or Linux/WSL2 (bubblewrap); native Windows is not supported.
- **What the barrier covers and what it does not:** the agent's shell and file tools cannot read the signing key or write approvals, plans, runs, or secrets, so it cannot create a valid approval; approvals require the human to accept the server's dialog or run `gentoo-agent approve` in their own terminal. Not covered: a user-configured Elicitation hook that answers dialogs automatically, commands the human runs with `!`, and settings the human loosens. Managed settings lock the sandbox keys.
- **Credentials:** generated passwords are never sent to the model; read them with `GENTOO_AGENT_HOME=<work dir> gentoo-agent run status <run_id>` in a terminal.

- [ ] **Step 3: Update the deny-rule test in `tests/test_cli.py`**

The existing tests keep passing; nothing else changes there.

- [ ] **Step 4: Run all tests and commit**

Run: `python3 -m unittest discover -s tests -v` and `.venv/bin/python -m unittest discover -s tests -v`.

```bash
git add .claude/settings.json docs/operator docs/OPERATING.md .mcp.json.example tests
git commit -m "Add the operator sandbox profile and protected operation guide"
```

---

### Task 11: Documentation

**Files:**
- Modify: `README.md`, `AGENTS.md`, `CLAUDE.md`, `docs/CONFIGURATION.md`, `docs/superpowers/specs/2026-10-15-mcp-server-design.md`

Writing rules for every added sentence: no em dashes, no emoji.

- [ ] **Step 1: README.md:** add `## AI-driven installation (MCP)` after the CLI workflow: install the extra (`pip install -e '.[mcp]'`), register the server (point to `.mcp.json.example` and `docs/OPERATING.md`), the tool workflow (detect_hardware, resolve_config, create_plan, approve_plan dialog, start_install, get_run_status/get_run_log), passwords only as `{"env": "NAME"}`, and generated passwords read in a terminal. Replace the sentence calling the approval check "a convention, not a security boundary" with: "With the operator profile and protected mode from docs/OPERATING.md the agent cannot create a valid approval; without them, the check is a convention."
- [ ] **Step 2: AGENTS.md:** add rules: use the MCP tools when the gentoo-agent server is available; never pass passwords as text; never try to approve except through `approve_plan`; if `approve_plan` returns `APPROVAL_REQUIRED`, ask the human to run the given command in their own terminal; never read `secrets/` or run credentials.
- [ ] **Step 3: CLAUDE.md:** commands (`.venv` setup and `.venv/bin/python -m unittest discover -s tests`); architecture bullets for `core/signing.py`, `core/resolve.py`, `core/layout.py`, `core/hardware.py`, `gentoo_agent/mcp/server.py` (CallToolResult with structured content, elicitation approval, no passwords to the model, never print), the operator profile, and the root-anchored deny-rule convention.
- [ ] **Step 4: docs/CONFIGURATION.md:** document `secrets/approval.key` (private, generated, never commit), the `resolve_config` layers and settable keys, and `phase_ids`/`run_phase`.
- [ ] **Step 5: Spec:** in `## Approval`, describe signed records, the sandbox profile, and the protected operation mode; in `## MCP tools`, note that tools return `CallToolResult` with structured content (no outputSchema), that `run_phase` takes a list of phases, and that `get_run_status` reports `credentials_waiting` instead of passwords; reconcile `### Error codes` with `core/errors.py`.
- [ ] **Step 6: Verify and commit**

Run: `grep -rn "—" README.md AGENTS.md CLAUDE.md docs/CONFIGURATION.md docs/OPERATING.md docs/superpowers/specs/2026-10-15-mcp-server-design.md` (no output) and both test commands.

```bash
git add README.md AGENTS.md CLAUDE.md docs
git commit -m "Document the MCP server, signed approvals, and protected operation"
```
