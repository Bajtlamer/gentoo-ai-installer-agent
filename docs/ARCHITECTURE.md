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
- [Plan kinds](#plan-kinds)
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
   path, size, model, serial, and WWN. The allowlist also includes `partitions`
   (`lsblk -J -b -e7` with the columns `PATH`, `TYPE`, `PKNAME`, `PARTTYPE`,
   `PARTTYPENAME`, `FSTYPE`, `UUID`, `PARTUUID`, `SIZE`, and `LABEL`) and
   `secure_boot` (the last data byte of the SecureBoot EFI variable). If every
   command fails with ssh exit 255 or a timeout, nothing is saved and
   `SSH_UNREACHABLE` is raised (or `HOST_KEY_UNKNOWN` when ssh reports "Host key
   verification failed").
2. **Resolve the configuration.** The CLI reads `config/target.json` and
   `config/install.json`. The MCP server calls `core/resolve.resolve_config()`,
   which layers overrides, profile, values inferred from the inventory,
   fallbacks, and `config/install.default.json`, and records the source of
   every value. It infers the boot mode and the architecture from the live system, and the
   disk when exactly one disk is eligible and it holds no Windows (a disk that
   holds Windows is never inferred); otherwise it raises `DISK_AMBIGUOUS` or
   `CONFIG_CONFLICT` instead of guessing.
3. **Create the plan.** `core/install_plan.create_install_plan()` runs
   `validated_settings()`, requires full disk-erase authorization and an
   inventory younger than 24 hours and not dated more than 5 minutes ahead, checks the configured architecture against
   the live one, runs the `validate_installation()` review and refuses on any
   error, takes the disk identity from the latest inventory, refuses a disk
   that `disks.disk_candidates()` excludes (removable, USB, read-only, or
   mounted) with `CONFIG_CONFLICT`, checks `expected_disk_model` and `expected_disk_size_gib` against it, and generates
   the apply and check program of every phase. For dual boot it refuses a UEFI
   install while Secure Boot is on (`CONFIG_CONFLICT`), derives
   `install.dual_boot_entries` from the `partitions` result, and freezes
   `secure_boot`, `warnings`, `erases_existing_os`, and the identity of every
   Windows disk with an entry (after the install disk in `plan["disks"]`, so the
   drift check re-reads it). It writes `plans/<plan_id>.json`. The `plan_hash`
   is SHA-256 over everything except `plan_id`, `plan_hash`, and `created_at`.
   The `plan_id` is the first 16 hex characters of the hash.
4. **Approve.** A human approves in one of two channels:
   - `gentoo-agent approve <plan_id>` opens `/dev/tty`, shows the summary, and
     requires the phrase `APPROVE <plan_id>`, or `APPROVE <plan_id> ERASE
     WINDOWS <disk>` when the plan erases a disk that holds Windows.
   - The MCP tool `approve_plan` shows the same summary in an elicitation
     dialog. Only an explicit accept with `approve: true` counts. For a plan
     that erases Windows, the form also needs `confirm` equal to `ERASE WINDOWS
     <disk>`. If the client cannot show dialogs, the tool returns
     `APPROVAL_REQUIRED` with the terminal command.

   Both call `core/approval.record_approval()`, which writes a signed
   `approvals/<plan_id>.json` valid for 60 minutes. The record carries
   `erase_confirmed`, and `require_approved_plan()` refuses a plan that erases
   Windows unless it is true.
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
   then the check again as verification. The `storage` phase has no such
   shortcut: it is applied in every run, a resume included, with no check
   deciding that the disk is already done. The generated program releases
   whatever is mounted or swapped on the install disk, discards and wipes it,
   writes the table, and wipes the new partitions. A disk with an old
   installation of the same layout used to pass the check, and its filesystems
   were reused. A resume therefore starts over from `storage`; the later
   phases then find nothing to skip. A nonzero exit stops the run with
   `PHASE_FAILED`. Every program runs as one `sh -c` invocation through
   `core/ssh.remote_shell()`. The worker passes its worker-lock descriptor as
   `keep_fds`. With `keep_fds`, `remote_shell()` does not start ssh directly:
   it starts a small holder process (`ssh.HOLDER`, whose first line is the
   marker comment `# gentoo-agent-ssh-holder` so `pgrep -f` finds it, run by the interpreter the
   worker passes as `holder_python`, `sys.executable`, with `-I` so modules in
   the working directory cannot shadow the standard library) with
   `pass_fds`, and the holder runs ssh as its child with stdin, stdout, and
   stderr inherited and exits with ssh's return code. If ssh cannot be
   started, the holder writes `gentoo-agent holder: cannot run ssh: <error>`
   to stderr and exits 255, as ssh does for its own errors. OpenSSH closes every
   inherited descriptor above stderr at startup, so ssh cannot hold the lock
   itself; the holder shares the lock's open file description until ssh
   exits. A worker killed with SIGKILL during a phase therefore keeps the run
   active until that ssh, and with it the remote program, has ended; a resume
   cannot start a second program on the same disk. The holder starts its own
   session, and a timeout or any other exception while it runs (for example
   a failing output callback) kills the whole process group (holder, ssh, and any
   ProxyCommand), so no ssh is left behind. Calls without `keep_fds` run ssh
   directly.
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
ends (and the holder process of the current phase, which keeps the
descriptor until its ssh exits, has ended too), so a reused process id cannot
make a dead worker look alive. A held lock
means the run is active even when no process id was recorded. The recorded
process id is the fallback only before the worker took the lock, for runs
without the file, and when `flock` itself fails on the lock file. There is no startup scan.

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
| Plan-time review | `install_plan.create_install_plan()` | A plan from a configuration that fails the `validate` review, an inventory older than 24 hours, an architecture that differs from the live system, an excluded disk (removable, USB, read-only, or mounted), or a disk that differs from `expected_disk_model` or `expected_disk_size_gib`. |
| `plan_hash` | `core/install_plan.py` | Edited plan files. `load_install_plan()` rejects them with `PLAN_HASH_MISMATCH`. |
| Signed approval with TTL | `core/approval.py`, `core/signing.py` | Approvals that are unsigned, malformed, for another hash, dated in the future, or older than 60 minutes. A malformed record counts as not approved. |
| Single gate | `require_approved_plan()` | Any start without a valid approval. It runs in `start_run()` and again in the worker. |
| Run lock | `runs/active.lock` under the `runs/.mutex` `fcntl` mutex | Two runs at once (`RUN_ALREADY_ACTIVE`). |
| Claim | `runs.claim_run()` | A worker running a run it does not own, or a run that is no longer `starting`. `execute_plan()` refuses unclaimed runs. |
| Drift check | `core/disks.check_drift()` | Writing to a disk that differs from the approved one (`TARGET_DRIFT`). It runs at the start of a run and before every destructive apply. |
| Worker lock | `runs/<run_id>/worker.lock` | A dead worker that looks alive (process id reuse), a second worker for the same run, and a resume while a killed worker's ssh program still runs (the ssh holder process keeps the lock descriptor until ssh exits). |
| No reboot | `core/runner.py` | No phase reboots. The target stays up for console recovery. |
| Passwords on stdin only | `core/credentials.py`, authentication apply program | Passwords in commands, logs, or configuration. The program reads the records once and pipes them only to `chpasswd`. |
| No passwords to the model | `mcp/server.py` | `get_run_status` reports only `credentials_waiting` and `credentials_applied`. |
| Escaped approval dialog | `approval._shown()`, `approval_summary()` | Target or inventory values faking dialog lines. Values with newlines, control characters, or more than 200 characters are JSON-escaped and truncated. The "Disk erase" and "Destructive phases" lines come before any such value. The install disk is labeled `Disk (erased)`, a Windows disk with a menu entry `Windows disk (kept, not written)`. |
| Strict inventory formats | `core/foreign_os.py`, `core/validate.py` | Untrusted inventory values reaching a program or the dialog. A device path or ESP UUID must pass `DEVICE_PATH` or `ESP_UUID` first; anything else makes the inventory unusable (`INVENTORY_MISSING`). Nothing mounts or writes the Windows disk. |
| Extended Windows confirmation | `core/approval.py`, `cli.py`, `mcp/server.py`, `prepare.py` | Erasing a disk that holds Windows with an ordinary approval. It needs `ERASE WINDOWS <disk>`, recorded as `erase_confirmed` in the signed record. |
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
| `foreign_os.py` | Windows detection from the `partitions` inventory (`windows` by the strict rule, the only one that builds menu entries; `windows-bios` and `windows-data` by the broader rule that protects a disk from an ordinary erase), `parse_secure_boot()`, derived `dual_boot_entries` |
| `hardware.py` | Inventory summary for discovery: boot mode, architecture, Secure Boot state, and disks with their existing OS |
| `install_plan.py` | Plan creation, `plan_hash`, loading and saving plans |
| `identity.py` | Provision plans: parses and compares the host identity (SSH host key fingerprint, root filesystem UUID, hostname), `check_identity()` |
| `inventory.py` | Collects and records the read-only inventories (`collect_inventory()` for install, `collect_provision_inventory()` for a running system) |
| `layout.py` | Partition layout from disk, boot mode, and swap size |
| `provision.py` | Validated provision settings (`validated_provision()`, `validated_tuning()`) and the Portage tuning rule (`derive_tuning()`) |
| `provision_facts.py` | Strict parsers for GPU, CPU, memory, kernel, and `make.conf` facts; `summarize_provision_inventory()` |
| `hardware_profile.py` | Pure `hardware_profile()`: architecture, threads, memory, GPUs, vendors, `video_cards` from a fixed vendor map (an unknown vendor gives `None` and `unknown_vendors`), `virtual` |
| `profiles.py` | The profile registry (`PROFILES`, `Profile`, `get_profile()`, `profile_overview()`, `provision_overview()`) |
| `desktop_phases.py` | Phases, flags, and apply and check generators of the `desktop-plasma` profile (a `VIDEO_CARDS` block, `package.use`, the profile switch, a `display-manager` block, runlevels) |
| `provision_phases.py` | `PHASES` and `PHASE_FLAGS` of a provision plan and the apply and check generators |
| `provision_plan.py` | `create_provision_plan()` |
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
| Discovery | `detect_hardware`, `detect_provision` |
| Local writes | `create_plan`, `create_provision_plan`, `approve_plan`, `cancel_run`, `get_run_status` |
| Destructive | `start_install`, `run_phase`, `resume_run` |
| Remote read | `verify_install` |
| Resources | `gentoo://plan/{plan_id}`, `gentoo://run/{run_id}/state`, `gentoo://run/{run_id}/log` |
| Prompt | `install_gentoo` |

Every tool returns a `CallToolResult` with `structured_content`. Errors carry
`{code, message, details}` and `is_error`. The server never prints: stdout
carries the protocol. `password_source: "prompt"` is refused over MCP.

## Plan kinds

A plan file `plans/<plan_id>.json` has a `kind`: `install` or `provision`. A file without `kind` (written
before provision plans existed) is an `install` plan (`plan_kind()` in `install_plan.py`). The plan hash, plan
id, verification on load, approval records, launcher, worker, run records, and logs are the same for both
kinds.

| Field | `install` | `provision` |
|-------|-----------|-------------|
| `disks` | disk identities, one erased | not present |
| `identity` | not present | host identity: `hostname`, `root_uuid`, `hostkey` (SSH ed25519 fingerprint) |
| `install` | validated install config | not present |
| `provision` | not present | validated settings (`profile`, `accept_licenses`, `package_use`) and derived facts (`gpu`, `cpu`, `memory_gib`, `tuning`) |
| `erases_existing_os` | possible | never |

Every phase of a plan has `destructive` (the phase changes the target; the guard runs before it) and `run`
(default true). A phase with `run: false` is skipped by the worker and by phase selection, and is checked only
by `verify_install`. The provision plan uses it for `gpu-online`, which can only pass after the reboot, so the
run ends in `installed_pending_reboot` and `verify_install` proves the GPU afterwards.

The runner guards by kind. For `install` it reads the disk identity again (`check_drift()`); for `provision` it
reads the host identity again (`check_identity()`), before the run and before every phase with `destructive`
true. A mismatch is `TARGET_DRIFT`: another server, or a reinstalled one with a new host key and root filesystem UUID.
The storage rule (the disk is always erased) applies to install plans only. The launcher resolves passwords
for install plans only; a provision plan carries none. `verify_install` regenerates each phase's check with the
generator of the plan's kind and refuses a stored check that differs (`PLAN_HASH_MISMATCH`).

The approval summary is built per kind (`_provision_summary()` in `approval.py`). The `ERASE WINDOWS` phrase
applies to install plans only.

The provision inventory is a second allowlist, `PROVISION_READ_ONLY_COMMANDS` in `ssh.py`, saved as
`inventory/provision-inventory-*.json` (`latest_provision_inventory()`; the install inventory is separate):

| Command | Reads |
|---------|-------|
| `host_identity` | host name, the UUID of the root filesystem (`findmnt`), the ed25519 host key fingerprint |
| `cpu` | thread count (`nproc --all`) and `lscpu` |
| `memory` | `MemTotal` and `SwapTotal` from `/proc/meminfo` |
| `gpu` | one line per PCI device from `/sys/bus/pci/devices` (address, class, vendor, device); no `lspci`, a stage3 has none |
| `portage_conf` | the user's own `MAKEOPTS` and `EMERGE_DEFAULT_OPTS`, outside the plan's block |
| `kernel` | `uname -r` |
| `arch` | `uname -m`, mapped to `amd64` or `arm64` |
| `portage_profile` | `eselect profile show`, the current Portage profile |
| `portage_profiles` | `eselect profile list`, every profile with its stability and the current one |
| `binrepos` | the https `sync-uri` values of `/etc/portage/binrepos.conf/*.conf` |

Every value is parsed strictly and reaches the summary only in a known format; the GPU name is built in code
from the vendor id. `create_provision_plan()` needs `root` as the SSH user, an inventory under 24 hours old,
and a profile that applies to the hardware. A provision plan changes files named `gentoo-agent-*` or blocks between
`gentoo-agent` markers, and a test scans every generated program for disk, mount, and reboot commands. See
[guides/provision.md](guides/provision.md).

### Hardware profile and profile registry

`hardware_profile(facts)` turns the summarized provision inventory into one data record that profiles decide
on. `profiles.PROFILES` maps a profile id to a `Profile` (title, `applicable`, `validate`, phases, flags, the
apply and check generators, `build` for the plan's `provision` block, and `summary` for the approval lines). `applicable(hardware)` returns `None` when the profile fits and a reason text
otherwise; a profile applies only when its reason is `None`. `create_provision_plan()` looks the profile up with
`get_profile()` and refuses when it does not apply. `detect_provision` adds `hardware` and `profiles` to its
result. Two profiles exist. `nvidia-driver` needs exactly one NVIDIA GPU. `desktop-plasma` needs amd64, a plain OpenRC
Portage profile (or already the Plasma one) whose `<base>/desktop/plasma` is offered exactly once, a known GPU
vendor, and at least one display device; it takes no licenses. Everything that depends on the profile goes
through the registry: `create_provision_plan` (`build`), the approval summary (`summary`), `verify_install` and
the runner (`check_command_for`). The `desktop-plasma` plan stores `gpus`, `video_cards`, `nvidia`,
`portage_profile`, `plasma_profile`, `binrepos`, `cpu`, and `memory_gib`. Its eight phases end in
`desktop-online` (`run: false`, checked by `verify_install` after the restart). The SDDM greeter stays on X11
and the Plasma Wayland session is offered, because the Wayland greeter wrappers on OpenRC are fragile and are
overwritten by SDDM updates. Details: [guides/provision.md](guides/provision.md#the-desktop-plasma-profile).

## Phases and the phase contract

`core/phases.PHASES` defines the runnable order. `config/plan.json` mirrors it
and adds the read-only `discovery` phase, which is the inventory step and not
part of a plan.

| Phase | Destructive | What it does |
|-------|-------------|--------------|
| `preflight` | no | Confirms root, disk type, boot mode, required tools, default route, and the optional expected model and size |
| `storage` | yes | Wipes the disk and writes the partition table with `sfdisk`; its check compares every partition's type and size, so an old disk with the same partition numbers never counts as done; after writing a new table the apply clears the signatures of every new partition, so an old filesystem at the same offset is never reused |
| `filesystems` | yes | Creates ext4, swap, and (UEFI) vfat, and mounts them under `/mnt/gentoo` |
| `base-system` | yes | Downloads and verifies the stage3 archive, extracts it, mounts `/dev`, `/proc`, `/sys`, `/run`, runs `emerge-webrsync` |
| `configuration` | yes | Hostname, timezone, locale, fstab (with the EFI partition at `/boot` on UEFI), networking (dhcpcd), OpenSSH with the root key, OpenRC user services off |
| `authentication` | yes | Regular user, user SSH key, sudo with the `%wheel` rule, passwords through `chpasswd` |
| `kernel` | yes | Installs `sys-kernel/gentoo-kernel-bin` with dracut through installkernel |
| `bootloader` | yes | Installs GRUB for BIOS or UEFI and writes `grub.cfg`; on UEFI also `efibootmgr` and a `/EFI/BOOT` fallback copy, and a menu entry per Windows (see below) |
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
programs for seven configurations in `tests/golden/`.

For dual boot, the bootloader apply checks that each Windows EFI partition
resolves (`blkid -U <uuid>`) to a partition whose parent disk
(`lsblk -no PKNAME`) is the entry's disk, then writes `/etc/grub.d/45_gentoo_agent_dualboot`
inside the chroot between `grub-install` and `grub-mkconfig`. The script is
rewritten on every run, and `/etc/grub.d/40_custom` is never touched. The
bootloader check and the `validation` phase grep `grub.cfg` for each entry's
`search --no-floppy --fs-uuid --set=root <uuid>` line. Without entries the
programs are byte-identical to the ones without dual boot
(`test_empty_dual_boot_entries_change_nothing`); the seventh golden
configuration is `uefi_gpt_dualboot_windows`.

The entry finds the Windows EFI partition with GRUB's `search`, which sees
only devices the firmware connected. Fast Boot (VMware `efi.quickBoot`, on by
default) connects only the boot disk, so the entry then fails with `no such
device`. Fast Boot is a firmware prerequisite, not something the installer
can check from the live system; the plan summary names it.

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
