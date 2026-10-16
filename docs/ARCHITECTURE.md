# Architecture

This document explains how Gentoo Agent works inside. It is for contributors
and for operators who want to know what happens between "create a plan" and
"installed, pending reboot". For the procedures, see the guides linked from
the [README](../README.md).

## Contents

- [Overview](#overview)
- [End-to-end flow](#end-to-end-flow)
- [What lives on disk](#what-lives-on-disk)
- [Safety layers](#safety-layers)
- [Module map](#module-map)
- [Phases and the phase contract](#phases-and-the-phase-contract)
- [Error codes](#error-codes)
- [Platform notes](#platform-notes)

## Overview

```text
             human                         AI client (optional)
               |                                  |
               | TTY                              | stdio MCP
               v                                  v
       gentoo_agent/cli.py              gentoo_agent/mcp/server.py
       gentoo_agent/prepare.py                    |
       gentoo_agent/init_config.py                |
               |                                  |
               +---------------+------------------+
                               v
                      gentoo_agent/core/        (pure logic, no I/O to the user)
                               |
                               v
                  gentoo_agent/jobs/launcher.py  start_run()
                               |  spawn, payload over stdin
                               v
                  gentoo_agent/jobs/worker.py    detached process
                               |
                               v
                      core/runner.execute_plan()
                               |
                               v
                         core/ssh.py  ---- OpenSSH ---->  live target
```

Rules that hold across the code:

- `core/` never prints, prompts, or exits. It returns data and raises
  `AgentError(code, message, details)`. `tests/test_core_purity.py` enforces
  this.
- `core/ssh.py` is the only module that contacts the target.
- Remote programs come only from the phase generators in `core/phases.py` or
  the read-only allowlist in `core/ssh.py`. There is no free-form command
  input.
- The front ends (`cli.py`, `prepare.py`, `init_config.py`, `mcp/server.py`)
  depend on `core/`. `core/` depends on none of them.

## End-to-end flow

1. **Inspect.** `gentoo-agent inspect` (or the MCP tool `detect_hardware`
   with `refresh=true`) runs every command in `core/ssh.READ_ONLY_COMMANDS`
   over SSH and saves `inventory/inventory-<stamp>.json`. One of those
   commands, `disk_identity`, runs `lsblk -J -b -d` and records each disk's
   path, size, model, serial, and WWN. If every command fails with ssh exit
   255 or a timeout, nothing is saved and `SSH_UNREACHABLE` is raised (or
   `HOST_KEY_UNKNOWN` when ssh reports "Host key verification failed").
2. **Resolve the configuration.** The CLI reads `config/target.json` and
   `config/install.json`. The MCP server calls `core/resolve.resolve_config()`,
   which layers overrides, profile, values inferred from the inventory,
   fallbacks, and `config/install.default.json`, and records the source of
   every value. It infers the boot mode and the architecture from the live system, and the
   disk when exactly one disk is eligible; otherwise it raises `DISK_AMBIGUOUS` or
   `CONFIG_CONFLICT` instead of guessing.
3. **Create the plan.** `core/install_plan.create_install_plan()` runs
   `validated_settings()`, requires full disk-erase authorization and an
   inventory younger than 24 hours, checks the configured architecture against
   the live one, runs the `validate_installation()` review and refuses on any
   error, takes the disk identity from the latest inventory, checks
   `expected_disk_model` and `expected_disk_size_gib` against it, and generates
   the apply and check program of every phase. It writes `plans/<plan_id>.json`. The `plan_hash`
   is SHA-256 over everything except `plan_id`, `plan_hash`, and `created_at`.
   The `plan_id` is the first 16 hex characters of the hash.
4. **Approve.** A human approves in one of two channels:
   - `gentoo-agent approve <plan_id>` opens `/dev/tty`, shows the summary, and
     requires the phrase `APPROVE <plan_id>`.
   - The MCP tool `approve_plan` shows the same summary in an elicitation
     dialog. Only an explicit accept with `approve: true` counts. If the client
     cannot show dialogs, the tool returns `APPROVAL_REQUIRED` with the
     terminal command.

   Both call `core/approval.record_approval()`, which writes a signed
   `approvals/<plan_id>.json` valid for 60 minutes.
5. **Start the run.** `jobs/launcher.start_run()` is the only code path that
   starts work. It calls `require_approved_plan()`, resolves passwords (from
   environment variables, generation, or a CLI prompt callback), takes the
   single-run lock, writes `runs/<run_id>/run.json` with status `starting`,
   and spawns `python -P -m gentoo_agent.jobs.worker <run_id>` in a new
   session. The password records travel to the worker over its stdin as a
   JSON payload. The password environment variables are removed from the
   worker's environment.
6. **Worker claim.** The worker validates the payload, takes the per-run
   flock `runs/<run_id>/worker.lock` (`runs.hold_worker_lock()`, under the
   `runs/.mutex` so status probes cannot race its startup), then calls
   `runs.claim_run()`, which moves the run from `starting` to `running` only
   if the run still holds the lock. A missing or malformed payload fails the
   run without executing anything. A second worker for the same run finds the
   lock held, returns `RUN_ALREADY_ACTIVE`, and leaves the run record and the
   run lock alone.
7. **Re-check the approval.** The worker calls `require_approved_plan()` again
   and refuses the run with `PLAN_HASH_MISMATCH` if the plan hash differs from
   the one recorded at start.
8. **Execute.** `core/runner.execute_plan()` first runs the `disk_identity`
   command again and compares path, size, model, serial, and WWN with the
   plan. Any difference stops the run with `TARGET_DRIFT` before a change. The
   same check runs again before every destructive apply, so a disk that
   changed mid-run stops the run before the next destructive phase.
9. **Per phase.** For each phase in plan order, the runner checks the cancel
   flag, then runs the stored check program. Exit 0 means the phase is already
   complete and it is skipped. Otherwise it runs the stored apply program, and
   then the check again as verification. A nonzero exit stops the run with
   `PHASE_FAILED`. Every program runs as one `sh -c` invocation through
   `core/ssh.remote_shell()`.
10. **Run records.** Every step is appended to `runs/<run_id>/log.jsonl`
    (phase, kind, label, command, return code, output). `run.json` tracks the
    status, the current phase, the completed phases, and the last error. A
    successful run ends with `installed_pending_reboot`. The worker always
    releases the run lock and then closes its worker lock. The launcher waits
    for each worker in a background thread so a long-lived MCP server leaves no
    zombie processes.

Resume: `run resume <run_id>` (or the MCP tool `resume_run`) starts a new run
of the same plan through `start_run()`. Phases whose check passes are skipped.

Interrupted runs are detected lazily. When `run_status()` reads an active run
whose worker is gone, it marks the run `interrupted` and releases the lock.
Liveness is read from the worker's flock: while a worker is alive it holds
`runs/<run_id>/worker.lock`, and the kernel drops the lock when the process
ends, so a reused process id cannot make a dead worker look alive. The recorded
process id is the fallback only before the worker took the lock and for runs
without the file. There is no startup scan.

## What lives on disk

All paths are relative to the root from `core/paths.py`: the repository root,
or `GENTOO_AGENT_HOME` when it is set. Paths are resolved on every call.

| Path | Contents | Written by |
|------|----------|------------|
| `config/target.json` | SSH target, Handbook URL, erase authorization | Operator, `init_config.py`, `prepare.py` |
| `config/install.json` | Installation choices for the CLI path | Operator, `init_config.py`, `prepare.py` |
| `config/install.default.json` | Safe defaults, used by the MCP resolver | Committed template |
| `config/plan.json` | Phase descriptions, `destructive` flags, Handbook sections | Committed |
| `inventory/inventory-<stamp>.json` | Read-only inventory results | `inspect`, `prepare.py`, `detect_hardware(refresh=true)` |
| `inventory/handbook-<stamp>.html` and `.json` | Handbook snapshot with URL and retrieval time | `handbook`, `prepare.py` |
| `plans/<plan_id>.json` | Frozen plan with `plan_hash` | `plan create`, MCP `create_plan` |
| `approvals/<plan_id>.json` | Signed approval record | `approve` (TTY), MCP `approve_plan` (elicitation) |
| `secrets/approval.key` | HMAC key (directory 0700, file 0600) | Created on the first approval |
| `runs/<run_id>/run.json` | Run status and progress | Launcher, worker, runner, `run_status()` |
| `runs/<run_id>/log.jsonl` | One JSON line per check, apply, or verify step | Runner |
| `runs/<run_id>/credentials.json` | Generated passwords, mode 0600 | Worker; deleted by CLI `run status` when shown, or after 24 hours |
| `runs/<run_id>/cancel` | Cancel flag, checked between phases | `run cancel`, MCP `cancel_run` |
| `runs/<run_id>/worker.err` | Worker standard error | Launcher redirects the worker into it |
| `runs/active.lock` | Run id of the run holding the single-run lock | `acquire_lock()`, `release_lock()` |
| `runs/.mutex` | `fcntl` lock file for all run-record and lock changes | `core/runs.py` |
| `state/state.json` | Target name, last inventory, last Handbook fetch | `inspect`, `detect_hardware(refresh=true)`, `prepare.py` |

The private files (`config/target.json`, `config/install.json`,
`state/state.json`, and the contents of `inventory/`, `plans/`, `approvals/`,
`runs/`, and `secrets/`) are ignored by Git. `logs/` is also ignored; the
current code does not write to it.

## Safety layers

| Layer | Where | What it stops |
|-------|-------|---------------|
| Validation | `core/validate.validated_settings()` | Unsafe or inconsistent values reaching remote commands. It is the single gate before generation. |
| Quoting | `core/phases.py` | Every value is quoted with `shlex.quote`. |
| Erase authorization | `install_plan._check_erase_authorized()` | A plan without `disk_must_be_erased`, `target_is_disposable`, and `disk_erase_authorized` set to `true` in the configuration. |
| Plan-time review | `install_plan.create_install_plan()` | A plan from a configuration that fails the `validate` review, an inventory older than 24 hours, an architecture that differs from the live system, or a disk that differs from `expected_disk_model` or `expected_disk_size_gib`. |
| `plan_hash` | `core/install_plan.py` | Edited plan files. `load_install_plan()` rejects them with `PLAN_HASH_MISMATCH`. |
| Signed approval with TTL | `core/approval.py`, `core/signing.py` | Approvals that are unsigned, malformed, for another hash, dated in the future, or older than 60 minutes. A malformed record counts as not approved. |
| Single gate | `require_approved_plan()` | Any start without a valid approval. It runs in `start_run()` and again in the worker. |
| Run lock | `runs/active.lock` under the `runs/.mutex` `fcntl` mutex | Two runs at once (`RUN_ALREADY_ACTIVE`). |
| Claim | `runs.claim_run()` | A worker running a run it does not own, or a run that is no longer `starting`. `execute_plan()` refuses unclaimed runs. |
| Drift check | `core/disks.check_drift()` | Writing to a disk that differs from the approved one (`TARGET_DRIFT`). It runs at the start of a run and before every destructive apply. |
| Worker lock | `runs/<run_id>/worker.lock` | A dead worker that looks alive (process id reuse), and a second worker for the same run. |
| No reboot | `core/runner.py` | No phase reboots. The target stays up for console recovery. |
| Passwords on stdin only | `core/credentials.py`, authentication apply program | Passwords in commands, logs, or configuration. The program reads the records once and pipes them only to `chpasswd`. |
| No passwords to the model | `mcp/server.py` | `get_run_status` reports only `credentials_waiting` and `credentials_applied`. |
| Escaped approval dialog | `approval._shown()`, `approval_summary()` | Target or inventory values faking dialog lines. Values with newlines, control characters, or more than 200 characters are JSON-escaped and truncated. The "Disk erase" and "Destructive phases" lines come before any such value. |
| Operator profile | `docs/operator/claude-settings.json` | The agent's shell and file tools writing `approvals/`, `plans/`, `runs/`, `secrets/`, `config/`, `inventory/`, `.mcp.json`, or `.claude/`. The shell cannot read `secrets/` or `runs/`, and the file tools cannot read `secrets/` or run credentials. |
| Protected mode | [guides/protected-mode.md](guides/protected-mode.md) | The agent editing the server's own code. The server runs from a separate venv. |

The signature is a barrier only when the agent cannot read `secrets/`, which
requires the operator profile. Without it, the TTY requirement and the deny
rules in `.claude/settings.json` are a convention.

## Module map

### `gentoo_agent/core/`

| Module | Responsibility |
|--------|----------------|
| `approval.py` | Approval summary, signed approval records, `require_approved_plan()`, `approval_status()` |
| `config.py` | JSON loading, atomic saving, timestamps, `target_config()`, `install_config()`, `load_plan()` |
| `credentials.py` | Builds `chpasswd` records from environment variables, generation, or a prompt callback |
| `arch.py` | The architecture table (`ARCHITECTURES`), `architecture_spec()`, `live_architecture()` |
| `disks.py` | Disk identities from `lsblk`, eligible candidates, identity strength, `check_drift()` |
| `errors.py` | `AgentError` and the stable error codes |
| `handbook.py` | Fetches the Handbook and saves a snapshot with URL and retrieval time |
| `hardware.py` | Inventory summary for discovery: boot mode, architecture, and disks |
| `install_plan.py` | Plan creation, `plan_hash`, loading and saving plans |
| `inventory.py` | Collects and records the read-only inventory |
| `layout.py` | Partition layout from disk, boot mode, and swap size |
| `paths.py` | Every directory, resolved per call, overridable with `GENTOO_AGENT_HOME` |
| `phases.py` | `PHASES` order and the apply and check generators per phase |
| `resolve.py` | Layered configuration resolution with value sources |
| `runner.py` | `execute_plan()`, phase selection, `render_dry_run()` |
| `runs.py` | Run records, lock, mutex, per-run worker lock, claim, cancel flag, log paging, one-time credentials |
| `signing.py` | HMAC-SHA256 signing key and signatures |
| `ssh.py` | `ssh_base()`, read-only allowlist, `remote_shell()`, strict host-key checking, `host_key_failed()` |
| `state.py` | Local workflow state in `state/state.json` |
| `validate.py` | `validated_settings()` gate and the broader `validate_installation()` review |

### Other modules

| Module | Responsibility |
|--------|----------------|
| `jobs/launcher.py` | `start_run()`, `resume_run()`, `cancel_run()` |
| `jobs/worker.py` | Detached worker: payload check, claim, approval re-check, execution, credentials |
| `mcp/server.py` | stdio MCP server: tools, resources, the `install_gentoo` prompt, elicitation |
| `cli.py` | `gentoo-agent` command (argparse subcommands) |
| `prepare.py` | Interactive beginner flow: read-only discovery, choices, writes `config/install.json` |
| `init_config.py` | Creates `config/target.json` and `config/install.json` without connecting |
| `tools/gentoo_agent.py`, `tools/init_config.py`, `tools/prepare_install.py` | Shims that put the repository root on `sys.path` and call the front ends |

Entry points from `pyproject.toml`: `gentoo-agent` (`gentoo_agent.cli:main`)
and `gentoo-agent-mcp` (`gentoo_agent.mcp.server:main`, needs the `[mcp]`
extra, `mcp==2.2.0`). `python -m gentoo_agent` is equivalent to
`gentoo-agent`.

### MCP surface

| Kind | Names |
|------|-------|
| Read-only tools | `list_phases`, `get_plan`, `get_run_log`, `resolve_config` |
| Discovery | `detect_hardware` |
| Local writes | `create_plan`, `approve_plan`, `cancel_run`, `get_run_status` |
| Destructive | `start_install`, `run_phase`, `resume_run` |
| Remote read | `verify_install` |
| Resources | `gentoo://plan/{plan_id}`, `gentoo://run/{run_id}/state`, `gentoo://run/{run_id}/log` |
| Prompt | `install_gentoo` |

Every tool returns a `CallToolResult` with `structured_content`. Errors carry
`{code, message, details}` and `is_error`. The server never prints: stdout
carries the protocol. `password_source: "prompt"` is refused over MCP.

## Phases and the phase contract

`core/phases.PHASES` defines the runnable order. `config/plan.json` mirrors it
and adds the read-only `discovery` phase, which is the inventory step and not
part of a plan.

| Phase | Destructive | What it does |
|-------|-------------|--------------|
| `preflight` | no | Confirms root, disk type, boot mode, required tools, default route, and the optional expected model and size |
| `storage` | yes | Wipes the disk and writes the partition table with `sfdisk` |
| `filesystems` | yes | Creates ext4, swap, and (UEFI) vfat, and mounts them under `/mnt/gentoo` |
| `base-system` | yes | Downloads and verifies the stage3 archive, extracts it, mounts `/dev`, `/proc`, `/sys`, `/run`, runs `emerge-webrsync` |
| `configuration` | yes | Hostname, timezone, locale, fstab, networking (dhcpcd), OpenSSH with the root key |
| `authentication` | yes | Regular user, user SSH key, sudo with the `%wheel` rule, passwords through `chpasswd` |
| `kernel` | yes | Installs `sys-kernel/gentoo-kernel-bin` with dracut through installkernel |
| `bootloader` | yes | Installs GRUB for BIOS or UEFI and writes `grub.cfg` |
| `validation` | no | Read-only final checks of mounts, boot files, services, and SSH |

The `destructive` flag comes from `config/plan.json` and is copied into the
plan. The approval summary lists the destructive phases.

Every phase follows the contract in [phases/README.md](../phases/README.md):

1. `check`: read the current state without changing it.
2. `apply`: make only the changes required by the approved plan.
3. `verify`: prove the postconditions. The runner reuses the check program.
4. `rollback` or a documented recovery path where practical. There is no
   automatic rollback today: a failed run leaves the target running for
   console recovery.

Phase programs must be idempotent, target-specific, logged, and safe to rerun
after an SSH disconnect. `tests/test_golden_commands.py` pins the generated
programs for six configurations in `tests/golden/`.

## Architectures

`core/arch.py` holds the one table of supported architectures. Each entry of
`ARCHITECTURES` gives the `uname -m` machine name, the GRUB EFI target, the
EFI binary name, and whether BIOS boot is possible: `amd64` is `x86_64`,
`x86_64-efi`, `grubx64.efi`, BIOS allowed; `arm64` is `aarch64`, `arm64-efi`,
`grubaa64.efi`, UEFI only.

- `phases.py` reads the table for the stage 3 URLs, the `grub-install` target,
  the bootloader check, and the `uname -m` test in `preflight`.
- `validate.py` refuses an unknown architecture and BIOS on an architecture
  without BIOS support, and reports a mismatch with the live system.
- `live_architecture()` reads the `arch=` line of the inventory's `identity`
  result. It raises `INVENTORY_MISSING` when the probe is missing or failed and
  `CONFIG_INVALID` for a machine that is not in the table. `resolve_config()`,
  `create_install_plan()`, `validate_installation()`, `prepare_install.py`, and
  `detect_hardware` (as `unknown` on failure) all use it.

Adding an architecture takes a table entry, a golden fixture in
`tests/golden/configs/` with regenerated programs, and a real test
installation on that architecture. The golden fixture only proves the commands
are stable, not that they work.

## Error codes

Codes are defined in `gentoo_agent/core/errors.py`. The CLI prints
`error [CODE]: message`. The MCP server returns `{code, message, details}`.

| Code | Meaning |
|------|---------|
| `CONFIG_INVALID` | A configuration value, input, or record is missing, malformed, or not supported |
| `CONFIG_CONFLICT` | Values contradict each other or the hardware, such as UEFI with MBR or a requested boot mode the live system did not boot in |
| `APPROVAL_REQUIRED` | The plan has no valid approval, or approval needs a human in a terminal |
| `PHASE_FAILED` | A phase apply or verify returned nonzero, the run was interrupted, or the worker could not start |
| `HANDBOOK_UNAVAILABLE` | The Handbook could not be fetched |
| `INVENTORY_MISSING` | No inventory exists, or it lacks usable disk identity data |
| `TARGET_DRIFT` | The live disk no longer matches the plan, or the plan has no disk identity |
| `SSH_UNREACHABLE` | The target could not be reached over SSH |
| `HOST_KEY_UNKNOWN` | ssh refused the connection because the target's host key is unknown or changed |
| `DISK_AMBIGUOUS` | No single eligible disk, or a disk without serial or WWN has an identical twin |
| `PLAN_NOT_FOUND` | No plan file exists for the plan id |
| `PLAN_HASH_MISMATCH` | A plan file was modified, or an approval or run refers to a different hash |
| `APPROVAL_EXPIRED` | The approval is older than 60 minutes |
| `APPROVAL_DECLINED` | The human did not confirm the approval |
| `RUN_NOT_FOUND` | No run record exists for the run id |
| `RUN_ALREADY_ACTIVE` | Another run holds the single-run lock |
| `INTERNAL_ERROR` | An unexpected exception in an MCP tool; details are in the server log |

[TROUBLESHOOTING.md](TROUBLESHOOTING.md) explains what to do for each code.

## Platform notes

- `core/runs.py` uses `fcntl`. Background runs (`run start`, `run resume`,
  and the MCP tools that start runs) therefore need macOS or Linux. On
  Windows, use WSL.
- `approve` opens `/dev/tty` and refuses without an interactive terminal, so
  it also needs macOS, Linux, or WSL.
- Dry runs (`install --dry-run`), `validate`, `inspect`, and `plan create`
  work on any platform with Python 3.12 and OpenSSH.
- The operator profile requires macOS (Seatbelt) or Linux or WSL2
  (bubblewrap). Native Windows is not supported.
