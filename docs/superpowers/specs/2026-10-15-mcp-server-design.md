# MCP server for AI-managed Gentoo installation: design

Date: 2026-10-15
Status: approved in brainstorming, pending written review

## Goal

Let an AI client (Claude Code, Claude Desktop, or any MCP client) drive a
complete Gentoo installation through typed MCP tools, with as few questions as
possible. A rich profile or a single prompt can supply every decision. The
only mandatory human interaction is one confirmation of the destructive plan.

The installer also inspects the target hardware and builds a kernel tailored
to it, with a distribution kernel as a boot fallback.

## Decisions

| Topic | Decision |
|---|---|
| Destructive approval | A human confirms once, inside the MCP flow (elicitation). CLI approval is the fallback. The model can never approve. |
| Server location | Operator machine, stdio transport, SSH to the live target (unchanged trust model). |
| Kernel | `gentoo-sources` with `localmodconfig` plus required and CPU options, and `gentoo-kernel-bin` as a fallback GRUB entry. |
| Missing values | Infer everything technical from hardware; personal values come from prompt, profile, fallbacks, then built-in defaults. No extra questions except ambiguity or conflict. |
| Passwords | Only environment-variable references. Empty or missing variable means generate and show once at the end. Plain-text passwords in tool input are rejected. |
| Code structure | New `gentoo_agent/` package; CLI and MCP server are thin front ends over a shared core. |

## Architecture

```
gentoo_agent/
  core/
    config.py       profile schema, merge, value sources
    validate.py     validation (today's validated_settings(), extended)
    hardware.py     inventory parsing: CPU, disks with serial/WWN, firmware, GPU, NICs, modules
    kernel.py       kernel plan
    phases.py       per phase: check / apply / verify generators
    plan.py         plan creation, plan_hash, persistence in plans/
    approval.py     approval records, expiry, require_approved_plan()
    runner.py       executor loop, state, logs, cancellation at phase boundaries
    ssh.py          ssh_base, remote_shell, read-only allowlist
  jobs/worker.py    detached background process that runs one approved plan
  cli.py            gentoo-agent command (inspect, plan, approve, install, status)
  mcp/server.py     stdio MCP server, thin adapter over core
tests/
```

Rules:

- `core/` never prints, reads input, or exits. It returns data and raises typed
  errors carrying stable error codes. The CLI and MCP server translate them.
- Only `core/ssh.py` talks to the target. Remote programs come only from the
  phase generators or the read-only allowlist. There is no free-form command
  input anywhere.
- `mcp/` and `cli.py` depend on `core/`; `core/` depends on neither.
- The MCP server uses the official Python MCP SDK. It is the only new runtime
  dependency.
- Existing `tools/*.py` behavior moves into the package unchanged before any
  new feature is added (see Build order).

## Value resolution

Every configuration value is resolved in this order, highest first:

1. `overrides` (values the AI extracted from the user's prompt)
2. profile file (`profile_path`)
3. values inferred from the hardware inventory
4. `fallbacks` (user-supplied defaults for anything not set above)
5. built-in defaults

The resolved config records the source of every value, for example
`hostname: server (fallback)` or `disk: /dev/nvme0n1 (inferred: only non-removable disk)`.

Normalization and conflicts:

- Usernames are lowercased (`Alex` becomes `alex`); the original spelling goes
  into the GECOS field. The change is reported as a warning.
- A requested boot mode that differs from the firmware the live system booted
  with returns `CONFIG_CONFLICT`. The server never guesses.
- More than one candidate disk without an explicit choice returns
  `DISK_AMBIGUOUS`; the AI asks the user.

Inferred technical values:

- `COMMON_FLAGS="-march=native -O2 -pipe"`
- `MAKEOPTS="-jN"`, N = core count capped at RAM / 2 GiB
- `CPU_FLAGS_X86` from `cpuid2cpuflags`
- `VIDEO_CARDS` from the GPU vendor
- `GRUB_PLATFORMS="efi-64"` for UEFI
- boot mode and partition table from firmware; swap size from RAM

## MCP tools

Every tool declares `inputSchema` and `outputSchema` and returns
`structuredContent`. Annotations: RO = `readOnlyHint`, D = `destructiveHint`,
I = `idempotentHint`.

### Discovery

| Tool | Input | Output |
|---|---|---|
| `detect_hardware` [RO, I] | `refresh: bool` | CPU (model, flags, cores), firmware, disks with serial/WWN, NICs, GPU, loaded modules, `inventory_hash` |
| `list_phases` [RO, I] | none | phases, order, destructive flag, Handbook sections |
| `resolve_config` [RO, I] | `overrides`, `fallbacks`, `profile_path?` | merged config, value sources, conflicts, warnings |

### Planning (writes only to local `plans/`)

| Tool | Input | Output |
|---|---|---|
| `create_plan` [I] | same as `resolve_config` | `plan_id`, `plan_hash`, steps with `destructive` flag, kernel plan, value sources |
| `get_plan` [RO, I] | `plan_id` | stored plan and approval status |

### Approval

| Tool | Input | Output |
|---|---|---|
| `approve_plan` | `plan_id` | `approved` with `approved_until`, or `APPROVAL_REQUIRED` with the CLI command, or `APPROVAL_DECLINED` |

### Execution

| Tool | Input | Output |
|---|---|---|
| `start_install` [D] | `plan_id` | `run_id`, returned immediately |
| `run_phase` [D] | `plan_id`, `phase` | `run_id` |
| `resume_run` [D] | `run_id` | `run_id` |
| `cancel_run` | `run_id` | `cancel_requested` |

### Monitoring

| Tool | Input | Output |
|---|---|---|
| `get_run_status` [RO] | `run_id` | status, phase index (`5/9`), current step, elapsed time, `last_error`, generated credentials (once, after completion) |
| `get_run_log` [RO] | `run_id`, `offset`, `limit` | log slice, `next_offset` |
| `verify_install` [RO] | `run_id` | read-only checks: fstab, custom and fallback kernels, GRUB entries, sshd, user, sudo |

Progress is reported as phase index, current step, and elapsed time. There is
no percentage, because phase durations differ by orders of magnitude.

### Resources and prompts

- Resources: `gentoo://plan/{id}`, `gentoo://run/{id}/state`,
  `gentoo://run/{id}/log`. Each is also reachable through a tool, because
  client support for resources varies.
- Prompt `install_gentoo`: guides the model through detect, resolve, create
  plan, approve, start, monitor, verify.

### Error codes

`HOST_KEY_UNKNOWN`, `SSH_UNREACHABLE`, `CONFIG_INVALID`, `CONFIG_CONFLICT`,
`DISK_AMBIGUOUS`, `INVENTORY_MISSING`, `HANDBOOK_UNAVAILABLE`,
`PLAN_NOT_FOUND`, `PLAN_HASH_MISMATCH`, `APPROVAL_REQUIRED`,
`APPROVAL_EXPIRED`, `APPROVAL_DECLINED`, `TARGET_DRIFT`, `RUN_NOT_FOUND`,
`RUN_ALREADY_ACTIVE`, `PHASE_PRECONDITION_FAILED`, `PHASE_FAILED`.

Errors are returned as `isError: true` results with `code`, `message`, and
structured `details`, never as raw exceptions.

### Example

Prompt: "Run the installation as a fully managed process with username Alex,
password from ALEX_PW, UEFI and GRUB. Otherwise use username gentoo, hostname
server, timezone Europe/Prague."

```json
create_plan({
  "overrides": {
    "username": "Alex",
    "user_password": { "env": "ALEX_PW" },
    "boot_mode": "uefi",
    "bootloader": "grub"
  },
  "fallbacks": {
    "username": "gentoo",
    "hostname": "server",
    "timezone": "Europe/Prague"
  }
})
```

## Plan and plan_hash

A plan is stored as `plans/<plan_id>.json` and contains:

- plan schema version
- resolved config with value sources
- target disk list, each with device path, serial, and WWN (a list from the
  start so RAID can be added later without a format change)
- `inventory_hash` of the inventory the plan was built from
- generated commands for every phase (check, apply, verify)
- kernel plan
- names of password environment variables (never their values)

`plan_hash` is a SHA-256 over a canonical serialization of all of the above.
Changing the schema version, the generators' output, any config value, or any
disk serial changes the hash.

`create_plan` verifies that referenced password variables are set or that
generation is allowed; it does not read the values into the plan.

## Approval

Single rule: an approval record is created only by a human, never by the model.

`approve_plan`:

1. Loads the stored plan. The dialog content is built by the server from that
   plan: target disks with serials, destructive steps, kernel plan summary,
   `plan_hash`. The model cannot supply or alter any of this text.
2. If the client supports elicitation, the server sends the dialog. Only an
   explicit accept writes `approvals/<plan_id>.json` with `plan_hash`,
   `approved_at`, `expires_at`, and `channel: "elicitation"`.
3. If the client does not support elicitation, the tool writes nothing and
   returns `APPROVAL_REQUIRED` with the command `gentoo-agent approve <plan_id>`.

CLI approval (`gentoo-agent approve <plan_id>`):

- prints the same server-built summary;
- reads the confirmation phrase from `/dev/tty` and refuses when no interactive
  terminal is attached, which stops a model's non-interactive shell (a
  convention, not a security boundary, since a process can allocate its own
  pseudo-terminal);
- writes the record with `channel: "cli"`.

A committed `.claude/settings.json` adds a deny rule for
`Bash(gentoo-agent approve:*)` as a second barrier against the model running
the CLI itself. The human runs it in their own terminal. Commands run through Claude Code's `!` prefix may not have an interactive terminal, so a separate terminal is the reliable choice.

`require_approved_plan(plan_id)` in `core/approval.py` is the only gate. It is
called by `start_install`, `run_phase`, and `resume_run`, and checks that the
record exists, has not expired, and matches the current `plan_hash`.

Approvals are valid for 60 minutes to start work. A started run may finish.
Resuming or running a single phase needs a valid approval; re-approving the
same `plan_hash` is one dialog.

## Execution

`start_install`:

1. `require_approved_plan()`.
2. Takes the single-run lock (`RUN_ALREADY_ACTIVE` if held).
3. Resolves passwords from the server's environment (generating any that are
   empty or missing) and starts `python -m gentoo_agent.jobs.worker <run_id>`
   as a detached process. Passwords go to the worker through a pipe, never as
   arguments, environment of child commands, or files.
4. Returns `run_id` immediately.

The worker:

1. Re-reads disk serials over SSH. A mismatch with the plan stops the run with
   `TARGET_DRIFT` before any change.
2. For each phase: read-only check (exit 0 means complete, skip), apply,
   verify with the same check.
3. Writes `runs/<run_id>/state.json` and `runs/<run_id>/log.jsonl` after
   every step.
4. Checks `runs/<run_id>/cancel` between phases.
5. Never reboots the target.

Generated passwords are written to `runs/<run_id>/credentials` (mode 0600)
because the worker outlives the server. `get_run_status` returns them once
after successful completion and deletes the file; an unread file is deleted
after 24 hours. They never appear in plans, logs, or state.

## Hardware profile and kernel

Read-only inventory additions: `/proc/cpuinfo`, `nproc`, `free -b`,
`lsblk -J` with `SERIAL` and `WWN`, `/sys/firmware/efi`, `lspci -nnk`,
`lsusb`, `lsmod`, DMI `sys_vendor` and `product_name` (hypervisor detection).

Kernel phase:

1. Copy the live environment's `lsmod` output to the target and run
   `make LSMOD=<file> localmodconfig` on `gentoo-sources` in the chroot.
2. Enable required options with `scripts/config`: ext4, devtmpfs; for UEFI,
   EFI, EFI partition support, and VFAT; storage drivers for the detected
   hardware (NVMe, AHCI, virtio); the detected NIC driver built in.
3. CPU optimization through the kernel's native-CPU option when the source
   version provides it, otherwise the generic x86-64 build. Microcode from
   `intel-microcode` or `linux-firmware` (AMD).
4. Build and install through `installkernel` with dracut, so an initramfs is
   always produced.
5. Install `gentoo-kernel-bin` as fallback. GRUB lists the custom kernel first
   and the fallback second.
6. Verify both kernels and initramfs images exist and `grub.cfg` contains both
   entries.

Known limit: `localmodconfig` only sees modules the live environment loaded.
The fallback kernel covers hardware that was absent or idle during inspection.

## Failure handling

- Phase failure: `status: failed`, `last_error`, failed phase recorded; log
  kept; target left running; no automatic rollback. `resume_run` re-runs from
  the start of the plan, skipping phases whose check passes.
- SSH drop during a phase: treated as failure; the phase check decides on
  resume whether it completed. Phases must stay idempotent
  (`phases/README.md`).
- Server or client restart: the worker continues. On startup the server scans
  `runs/`; a lock with a dead PID marks the run `interrupted`, resumable.
- Cancellation: only at phase boundaries, status `cancelled`.
- Never automatic: reboot, repair of an existing system, concurrent runs.

## Testing

1. Golden snapshots: before the refactor, capture `install --dry-run` output
   for BIOS/MBR, UEFI/GPT, with and without user and sudo. After the package
   move, generated commands must match byte for byte.
2. Core unit tests without SSH: merge order and value sources, validation,
   `plan_hash` stability and sensitivity (config, disk serial, schema version),
   `require_approved_plan()` for missing, expired, and mismatched approvals.
3. Hardware fixtures: stored inventories from a QEMU VM, an NVMe machine, and
   a multi-disk machine; disk selection, `DISK_AMBIGUOUS`, kernel option set.
4. Runner with a fake SSH layer: failure, resume, cancel, interrupted worker,
   `TARGET_DRIFT`.
5. MCP tests with the SDK in-memory client: output schemas; `approve_plan`
   without elicitation returns `APPROVAL_REQUIRED` and writes nothing;
   plain-text password rejected. CLI `approve` refuses without a TTY.
6. End-to-end on a disposable QEMU VM, BIOS and UEFI (OVMF), operator present.

## Out of scope for version 1

RAID, LUKS, filesystems other than ext4, systemd, repair mode, HTTP transport,
automatic reboot, multiple plans or runs per target.

## Build order

1. Golden dry-run snapshots of the current code.
2. Move `tools/*.py` into `gentoo_agent/` with unchanged behavior; snapshots
   and existing tests pass.
3. Make `core/` free of printing, input, and exits; typed errors with codes.
4. Plan, `plan_hash`, approval records, `require_approved_plan()`.
5. Background worker, run state, cancel, interrupted detection.
6. CLI on the new core, including TTY-only `approve`.
7. MCP server with tools, resources, prompt, and elicitation.
8. Hardware profile, value inference, and kernel plan.
