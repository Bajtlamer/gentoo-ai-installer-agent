# Troubleshooting

Every failure in gentoo-agent carries a stable error code. The CLI prints it as
`error [CODE]: message` on stderr and exits with status 1. The MCP server
returns it as a tool error with `{code, message, details}`. A run that fails in
the background stores the same object in `error` (and the message in
`last_error`) in `runs/<run_id>/run.json`, which `run status` and
`get_run_status` show.

## Contents

- [Error codes](#error-codes)
- [SSH host key not trusted](#ssh-host-key-not-trusted)
- [Approval refused without a terminal](#approval-refused-without-a-terminal)
- [Approval expired](#approval-expired)
- [TARGET_DRIFT after a reboot of the live ISO](#target_drift-after-a-reboot-of-the-live-iso)
- [DISK_AMBIGUOUS with a USB stick](#disk_ambiguous-with-a-usb-stick)
- [Disk mounted after a failed run](#disk-mounted-after-a-failed-run)
- [Root filesystem far too small after the storage phase](#root-filesystem-far-too-small-after-the-storage-phase)
- [Kernel install fails: not enough disk space on /boot](#kernel-install-fails-not-enough-disk-space-on-boot)
- [Old installation reused instead of erased](#old-installation-reused-instead-of-erased)
- [Run stuck in starting, or interrupted](#run-stuck-in-starting-or-interrupted)
- [RUN_ALREADY_ACTIVE](#run_already_active)
- [Where the logs are](#where-the-logs-are)
- [Credentials not shown](#credentials-not-shown)
- [Secure Boot is on](#secure-boot-is-on)
- [Windows EFI partition not found](#windows-efi-partition-not-found)
- [Windows entry fails with no such device](#windows-entry-fails-with-no-such-device)
- [BitLocker recovery prompt](#bitlocker-recovery-prompt)
- [Windows in BIOS mode](#windows-in-bios-mode)
- [Windows disk on USB or removable media](#windows-disk-on-usb-or-removable-media)
- [Windows](#windows)

## Error codes

| Code | Typical causes | What to do |
|---|---|---|
| `CONFIG_INVALID` | A missing or unreadable `config/target.json`, `config/install.json`, or `config/plan.json`. A value fails validation (unsafe disk path, SSH user other than `root`, non-ext4 root, bad hostname, timezone, username, groups, or static network including a missing or invalid `interface`). A missing `architecture` in an older `config/install.json`. The configuration does not pass review at `plan create` (`details.errors` lists the findings of `validate`). The disk model or size differs from `expected_disk_model` or `expected_disk_size_gib`, or one of them has the wrong type (`expected_disk_size_gib` must be a number greater than 0, `expected_disk_model` a string). A password variable name that does not end in `_PASSWORD` or `_PW`, or a password passed as text. Missing public key next to `identity_file`. Disk erasure not fully authorized at `plan create` (`details.missing` lists the flags). A missing password variable with `generate_missing_passwords: false`. `password_source: "prompt"` without a terminal, or through MCP. `init_system` other than `openrc` (in any front end or in `install.json`), or a bootloader other than `grub` in `resolve_config`. A disk not in the latest inventory. An invalid `dual_boot` (not `auto` or `off`) or an invalid `dual_boot_entries`. An invalid plan id or run id. Background runs on native Windows. `runs/active.lock` that is not a regular file. | Read the message and `details.field`; fix the named file or value; see the [configuration reference](CONFIGURATION.md). Run `gentoo-agent validate` before `plan create`. |
| `CONFIG_CONFLICT` | UEFI requested with a partition table other than GPT. A requested `boot_mode` that differs from how the live system booted. A configured `architecture` that differs from the live system, or `arm64` with BIOS boot. Secure Boot on for a UEFI install (see [Secure Boot is on](#secure-boot-is-on)). `dual_boot_entries` together with `dual_boot: off` or a BIOS install. A chosen disk that is excluded (removable, USB, read-only, or mounted), whether it comes from `resolve_config` or from `config/install.json` at `plan create`. After a failed run the disk is usually still mounted at `/mnt/gentoo` (see [Disk mounted after a failed run](#disk-mounted-after-a-failed-run)). | Boot the VM in the firmware mode you want (or request the mode it booted in) and inspect again. Choose an eligible disk. |
| `APPROVAL_REQUIRED` | The plan has no approval yet. The approval record is malformed, unsigned, or signed with another key. `approve` run without an interactive terminal. The signing key `secrets/approval.key` is not a private regular file you own, or is malformed. `install --execute` (replaced by plans). An MCP client without elicitation support, or a failed dialog. A plan that erases a disk holding Windows whose approval lacks the extended confirmation, including approval records written before that rule (they have no `erase_confirmed`). | Approve the plan yourself in your own terminal: `gentoo-agent approve <plan_id>`. For a bad key, remove `secrets/approval.key` and approve again (existing approvals become invalid). |
| `APPROVAL_DECLINED` | The typed phrase was not exactly `APPROVE <plan_id>` (or `APPROVE <plan_id> ERASE WINDOWS <disk>` for a plan that erases Windows), or the dialog was declined, cancelled, or accepted without checking `approve`. For a plan that erases Windows, a missing or different `confirm` value (it must be exactly `ERASE WINDOWS <disk>`). | Nothing was recorded. Approve again if you meant to. |
| `APPROVAL_EXPIRED` | More than 60 minutes passed between the approval and `run start`, `run resume`, `start_install`, `run_phase`, or `resume_run`. | Approve the same plan again, then start or resume. See [Approval expired](#approval-expired). |
| `PLAN_NOT_FOUND` | No `plans/<plan_id>.json` for that id (typo, or a different `GENTOO_AGENT_HOME`). | Check the id from `plan create` and the work directory. |
| `PLAN_HASH_MISMATCH` | A plan file was edited after creation. The approval is for a different plan hash. The plan changed after the run started. `verify_install` found a stored check that does not match what the code generates now, for example after the SSH key changed. | Never edit `plans/` or `approvals/`. Create a new plan, approve it, and start it. |
| `INVENTORY_MISSING` | No inventory yet. The inventory is malformed, has no `disk_identity` result, or lists no disks. The boot mode probe failed. The `storage_json` probe failed, so mount state is unknown. At `plan create`: the latest inventory is older than 24 hours, has no valid `collected_at`, or is dated more than 5 minutes in the future (check the operator machine's clock). The identity probe did not report the live architecture. A missing or unusable `partitions` result (malformed, a value that fails the strict format checks, or a disk whose partitions all lack a partition type; a missing filesystem alone is fine), or a Windows disk with a menu entry that is missing from the `disk_identity` result. | Run `gentoo-agent inspect` (or `detect_hardware` with `refresh=true`) and check that the target answered every command. |
| `DISK_AMBIGUOUS` | In `resolve_config` and `create_plan`: none or several disks are eligible, so no disk can be inferred (`details.candidates`, `details.excluded`). At `plan create`: the disk has no serial or WWN and another disk has the same size and model (`details.twins`). In `resolve_config` and `create_plan`: the only eligible disk holds Windows, which is never inferred (`details.existing_os`). | Name the disk explicitly (the `disk` override, or `disk` in `install.json`), or detach the twin disk and inspect again. See [DISK_AMBIGUOUS with a USB stick](#disk_ambiguous-with-a-usb-stick). |
| `TARGET_DRIFT` | At the start of a run, and again before each destructive phase, the live disk at the planned path has a different size, model, serial, or WWN than the plan, or is gone. The plan has no disk identity. The same check covers every Windows disk with a menu entry: one that is gone or swapped stops the run. | Inspect again, confirm the disk, and create and approve a new plan. See [TARGET_DRIFT after a reboot of the live ISO](#target_drift-after-a-reboot-of-the-live-iso). |
| `HOST_KEY_UNKNOWN` | ssh exited with 255 and "Host key verification failed": the target's host key is not in the known hosts file, or has changed. Raised by `inspect` and `detect_hardware` (inventory), by the disk identity read at the start of a run and before each destructive phase, and by `verify_install`. | Verify the fingerprint on the target console, then add the key to the configured known hosts file. See [SSH host key not trusted](#ssh-host-key-not-trusted). |
| `SSH_UNREACHABLE` | Every inventory command failed (wrong address, key, or user); nothing was saved. `ssh` could not be executed. The disk identity could not be read at the start of a run. `verify_install` could not reach the target. | Test the connection by hand with `BatchMode=yes` and `StrictHostKeyChecking=yes`. See [SSH host key not trusted](#ssh-host-key-not-trusted). |
| `HANDBOOK_UNAVAILABLE` | `gentoo-agent handbook` could not download `handbook_url`. | Check the network and the URL in `config/target.json`. `prepare_install.py` only warns about this. |
| `PHASE_FAILED` | A phase's apply or verification returned non-zero (`details.phase`, `details.returncode`). An unexpected exception during the run. The worker could not be started, or exited before receiving its payload. The worker ended without recording a result (status `interrupted`). A Windows entry missing from `grub.cfg` fails the bootloader or validation check, and the bootloader apply fails when the Windows EFI partition is gone (see [Windows EFI partition not found](#windows-efi-partition-not-found)). | Read the end of the log for that phase, fix the cause (often network, mirrors, or a console fix), then `run resume <run_id>`. |
| `RUN_NOT_FOUND` | No `runs/<run_id>/run.json` for that id. | Check the run id and `GENTOO_AGENT_HOME`. |
| `RUN_ALREADY_ACTIVE` | Another run holds `runs/active.lock` and is still active, or `run resume` was called on a run that is still active. | See [RUN_ALREADY_ACTIVE](#run_already_active). |
| `INTERNAL_ERROR` | MCP only: a tool failed with an unexpected exception. | Read the server log on its stderr (your MCP client keeps it in its MCP server log) and report the traceback. |

## SSH host key not trusted

Symptoms: `inspect`, the disk identity read of a run, or `verify_install` fails
with `HOST_KEY_UNKNOWN` ("The target's SSH host key is unknown or has
changed."). The code means ssh exited with 255 and printed "Host key
verification failed". Other connection failures stay `SSH_UNREACHABLE`
("Could not reach the target over SSH; check the address, key, and host
key.").

Every connection uses `BatchMode=yes` and `StrictHostKeyChecking=yes`, so ssh
never asks whether to trust a key; an unknown or changed key simply fails.

1. Run the same connection by hand to see ssh's own message:

   ```sh
   ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -i <identity_file> root@<host> true
   ```

2. "Host key verification failed" or "REMOTE HOST IDENTIFICATION HAS CHANGED"
   means the key is not in the known hosts file that the target uses
   (`known_hosts_file` in `config/target.json`, or your default file when it
   is empty).
3. Verify the key out of band: print the fingerprint on the live console
   (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`) and compare it with
   the key fetched over the network (`ssh-keyscan -t ed25519 <host>`).
4. Only if the fingerprints match, remove a stale entry
   (`ssh-keygen -R <host>`) and add the verified key with `ssh-keyscan` to the
   known hosts file named by `known_hosts_file` (or your default file). The [CLI guide](guides/cli-install.md#step-2-verify-and-trust-the-host-key)
   has the full procedure.

"Permission denied (publickey)" means the key is not in the live system's
`/root/.ssh/authorized_keys`, or `identity_file` points to another key.

## Approval refused without a terminal

`approve` opens `/dev/tty` directly and refuses to run without a controlling
terminal:

```text
error [APPROVAL_REQUIRED]: approve needs an interactive terminal; run it yourself in your own terminal.
```

This happens when the command runs inside an agent's shell tool, in CI, under
`ssh` without a terminal, or on native Windows. Redirecting standard input
does not help, because the command does not read standard input. Run the
command yourself in a normal terminal window. With the MCP server, use
`approve_plan` in a client that supports elicitation, or run the command it
returns in your own terminal.

## Approval expired

An approval is valid for exactly 60 minutes from the moment it was recorded.
Starting a run after that fails:

```text
error [APPROVAL_EXPIRED]: The approval for plan <plan_id> expired at <time>; approve it again.
```

The check happens when a run starts and again when the worker claims it, not
during the run, so a long installation is not cut off when the hour ends.
To continue:

1. Approve the same plan again: `gentoo-agent approve <plan_id>` (or
   `approve_plan`). The plan does not change, so it keeps its id.
2. Run `run start <plan_id>`, or `run resume <run_id>` for a run that failed.

## TARGET_DRIFT after a reboot of the live ISO

A plan is bound to the identity of one disk: path, size in bytes, model,
serial, and WWN. At the start of every run, and again before every destructive
phase, the worker reads the live disk at the planned path again and stops if
anything differs:

```text
Disk /dev/sda does not match the approved plan.
```

`error.details` holds `expected` and `actual`. After a reboot of the live ISO,
the usual cause is that device names were assigned in a different order, for
example because a USB stick was attached and the disk you planned for is now
`/dev/sdb`. A missing path gives "Disk /dev/sda is no longer present on the
target."

A reboot also usually gives the live system a new SSH host key and can give it
a new address. Those show up as `SSH_UNREACHABLE`, not as drift.

1. Verify the new host key and update `config/target.json` if the address
   changed.
2. Run `gentoo-agent inspect` and find the disk by serial or WWN.
3. Set the correct `disk` (and layout, through `init_config.py --overwrite` or
   by hand) in `config/install.json`.
4. Run `validate`, `plan create`, `approve`, and `run start` with the new plan.

Do not edit the old plan; a modified plan is refused with `PLAN_HASH_MISMATCH`.

## DISK_AMBIGUOUS with a USB stick

How a USB stick is treated depends on the path:

- **MCP (`resolve_config`, `create_plan`).** Disks that are removable, attached
  over USB, read-only, or mounted are excluded from inference. With one
  internal disk and the boot stick attached, the internal disk is inferred and
  a warning names the excluded stick, for example
  `excluded: /dev/sdb (removable; usb transport)`. `DISK_AMBIGUOUS` ("No single
  eligible disk was found; choose one with the disk setting.") means none or
  several disks remain. `details.candidates` lists the eligible disks and
  `details.excluded` the others with the reason. Tell the AI which disk to
  use; it passes it as the `disk` override. Choosing an excluded disk, such as
  the USB stick itself, fails with `CONFIG_CONFLICT`: installing to a USB disk
  is not supported on this path.
- **CLI (`plan create`).** The disk comes from `config/install.json`; there is
  no inference. `DISK_AMBIGUOUS` here means the chosen disk has no serial
  number or WWN and another disk has the same size and model, so it cannot be
  identified reliably (`details.twins`). This is typical of two identical
  cheap USB sticks. Detach the twin, run `inspect` again, and create the plan
  again.

A disk without a unique serial or WWN (none at all, or one another disk shares,
as every VMware virtual NVMe disk reports `VMware NVME_0000`) that has no twin
is accepted, and the plan summary adds: "WARNING: this disk has no unique serial number or WWN; drift detection relies
on path, size, and model only."

## Disk mounted after a failed run

`plan create` (and `resolve_config` and `create_plan` over MCP) fails with
`CONFIG_CONFLICT` "Disk /dev/sda cannot be installed to: mounted at
/mnt/gentoo, ..." after a run that stopped partway. The `filesystems` phase
mounted the new partitions under `/mnt/gentoo` and turned on swap, and they
stay that way after a failure. A mounted disk is never offered for a new plan,
and this rule has no exception for `/mnt/gentoo`: the tool cannot tell its own
mounts from someone else's.

Choose one:

- Keep the configuration: run `gentoo-agent run resume <run_id>` (not after the
  storage mistake in
  [Root filesystem far too small after the storage phase](#root-filesystem-far-too-small-after-the-storage-phase); MCP:
  `resume_run`). A resume runs the already approved plan, which needs no new
  plan; approve it again first if the 60 minutes have passed. Completed phases
  are checked and skipped.
- Change the configuration first: on the live system run
  `umount -R /mnt/gentoo` and `swapoff <planned swap partition>` (the
  `linux-swap` entry of `partition_layout` in `config/install.json`, for
  example `/dev/sda2` or `/dev/nvme0n1p2` with UEFI and `/dev/sda1` with
  BIOS; skip it when `swap_mb` is 0), or reboot the live ISO (then verify the
  new host key). Then run `inspect` again and `plan create`. Do not use
  `swapoff -a`: it would also turn off any other swap on the live system.

## Root filesystem far too small after the storage phase

Symptom: `base-system` fails while unpacking stage3 with `No space left on
device`, and the `filesystems` log shows a root of a few hundred MB (for
example `/dev/nvme0n1p3 ext4 856.3M`) or `Setting up swapspace version 1, size =
1.8 TiB`. The partition IDs in the `blkid` output at the end of the
`filesystems` step are the old ones of the previous disk contents.

Cause (fixed in plans created after 2026-10-19): the storage check only asked
whether the disk had a GPT label and partitions with the planned numbers. A
disk that already had three partitions, for example an old Windows disk with
MSR, NTFS, and recovery, passed it, so the runner skipped the partitioning and
the `filesystems` phase formatted the old partitions: the old data partition
became swap and the small recovery partition became the root. Only the chosen
install disk is affected. The check now also compares each partition's type and
size, and the last partition has to fill the rest of the disk.

Do not resume the run. A resume runs the commands frozen in the old plan,
including the old check. Instead:

1. On the live system, release the disk: `umount -R /mnt/gentoo` and
   `swapoff <planned swap partition>` (see
   [Disk mounted after a failed run](#disk-mounted-after-a-failed-run); not
   `swapoff -a`).
2. Restart the MCP server (restart Claude Code) so it runs the fixed code.
3. Run `inspect` (MCP: `detect_hardware` with `refresh=true`), create a new
   plan, approve it, and start it. The storage check now fails on the old
   layout, so the disk is wiped and partitioned as planned.

## Old installation reused instead of erased

Symptom: the run finishes `storage` and `filesystems` within seconds on a
disk that held an earlier installation, there is no `mkfs` output, and the
`base-system` phase downloads no stage3. The old system is still there with the
new configuration on top.

Cause (fixed after 2026-10-19): the storage check passed because the old
layout matched the plan (same partition types and sizes), so the phase was
skipped and the old filesystems were reused. The `storage` phase now never
takes that shortcut: every run, a resume included, releases what is mounted or
swapped on the install disk, discards and wipes it, writes the table, and wipes
the new partitions. If something on the disk cannot be released (for example an
active LVM or RAID device), the program stops before any write with `disk ... is
still mounted or used as swap`; release it by hand and start again.

## Kernel install fails: not enough disk space on /boot

Symptom: the `kernel` phase fails in the `postinst` step of
`sys-kernel/gentoo-kernel-bin` with `Not enough disk space. Need at least 47312
KiB, found 16116 KiB.` (the numbers vary), although the plan has a 512 MB EFI
partition. The `filesystems` log shows no `mkfs.fat` line, and `blkid` lists the
EFI partition with a UUID that an earlier run created.

Cause (fixed in plans created after 2026-10-19): `sfdisk` writes the new
partition table but leaves the contents of the partitions alone. A new
partition that starts where an old one did kept the old filesystem signature,
for example a 16 MB vfat from an earlier failed run, and the `filesystems`
phase skips `mkfs` when it already sees the right filesystem type. The
partition is 512 MB, but `/boot` has the size of the old filesystem. The same
could leave an old root filesystem, with its files, in place. The `storage`
apply now clears the signatures of every new partition right after it writes
the table (`wipefs -a` on the planned partitions), in every run.

Do not resume the run. The frozen plan still has the old storage program, and
the storage check passes on a correct table, so the partitions would not be
wiped. Instead, on the live system:

1. `umount -R /mnt/gentoo` and `swapoff <planned swap partition>` (see
   [Disk mounted after a failed run](#disk-mounted-after-a-failed-run); not
   `swapoff -a`).
2. Clear the partition table of the install disk, which the plan already
   authorizes to erase: `wipefs -a <install disk>` (for example
   `wipefs -a /dev/nvme0n1`). The storage check then fails, so the new plan
   partitions the disk and wipes the new partitions.
3. Restart the MCP server (restart Claude Code), run `inspect` (MCP:
   `detect_hardware` with `refresh=true`), create a new plan, approve it, and
   start it.

## Run stuck in starting, or interrupted

A run is `starting` from the launch until the worker claims it (then
`running`). `run status` and `get_run_status` check whether the worker is
still alive:

- If the worker's lock file `runs/<run_id>/worker.lock` exists and no
  process holds it, or (before the worker took the lock, or for a run without
  the file) the recorded process id is gone, or no process id was recorded and
  the run started more than 60 seconds ago, the run is marked `interrupted` with "The worker process ended without recording
  a result." and the run lock is released. While some process holds the
  worker lock, the run stays active whatever the recorded process id says.
- A worker killed during a phase leaves its ssh running the remote program,
  under a small holder process (a `python -I -c ...` process whose child is
  ssh, recognizable by the marker `gentoo-agent-ssh-holder` in its command
  line). The holder keeps the worker lock until ssh exits, so the run stays
  `running` until the remote program ends; only then can you resume. If the
  remote program hangs, see "Clearing a run whose remote program hangs" below.
- A run is also `interrupted` when the worker itself was interrupted during a
  phase ("Installation was interrupted.").

To continue:

1. Read `runs/<run_id>/worker.err` for the worker's own error output (for
   example an import error in the environment the worker was started with).
2. Read the end of `run log <run_id>` to see which phase was in progress.
3. Run `gentoo-agent run resume <run_id>`. Completed phases are detected by
   their checks and skipped.

A run that is stuck in `running` with a live worker is usually waiting for a
long phase (`base-system`, `kernel`). The log gets an entry only when each step
ends.

### Clearing a run whose remote program hangs

A run stays `running` for as long as the ssh of its current phase runs, even
when the worker is gone, and `run cancel` takes effect only between phases.
When you are sure the remote program hangs (check it on the target's
console), stop it on the operator machine:

1. Find the holder process. Its command line always contains the marker
   `gentoo-agent-ssh-holder`:

   ```sh
   pgrep -fl gentoo-agent-ssh-holder
   ```

   Each match is the holder of one phase. With more than one match, confirm
   the target address in the ssh command line with
   `ps -o pid,pgid,command -p <pid>`.
2. Kill the holder's whole process group. The holder starts its own session,
   so its process id is also the group id:

   ```sh
   kill -KILL -- -<pid>
   ```

   Never kill only the holder: its ssh child would keep running the remote
   program without the worker lock, and a resume could then start a second
   program on the same disk.
3. Run `gentoo-agent run status <run_id>`. With the worker gone the run
   becomes `interrupted`; with the worker still alive the phase fails and the
   run becomes `failed`.
4. The remote side may still be running after ssh is gone. Check on the
   target's console that nothing from the phase is left (for example with
   `ps aux`), then resume or re-plan as described in
   [Disk mounted after a failed run](#disk-mounted-after-a-failed-run).

## RUN_ALREADY_ACTIVE

Only one run can be active at a time. The lock is `runs/active.lock` and holds
the run id of the current run.

```text
error [RUN_ALREADY_ACTIVE]: Run <run_id> is still active.
```

1. Check the named run: `gentoo-agent run status <run_id>`. If its worker is
   gone, this marks it `interrupted` and releases the lock.
2. If it is really running, wait for it, or stop it with
   `gentoo-agent run cancel <run_id>` (it stops before the next phase).
3. "Another run holds the run lock." without a run id means the lock file is
   empty; it is treated as active until it is 60 seconds old.
4. A lock holder whose record is missing or unreadable also counts as active
   until the lock file is 60 seconds old.

`run resume` on a run that is still active fails with the same code.

## Where the logs are

All paths are relative to the repository root, or to `GENTOO_AGENT_HOME`.

| Path | Content |
|---|---|
| `runs/<run_id>/run.json` | The run record: status, phase index, current phase, completed phases, last error. |
| `runs/<run_id>/log.jsonl` | One JSON line per check, apply, and verify step: phase, kind, label, command, return code, output, finish time. Read it with `run log` or `get_run_log`. |
| `runs/<run_id>/worker.err` | Standard error of the background worker. |
| `runs/<run_id>/credentials.json` | Generated passwords until they are shown once (mode 0600). |
| `plans/<plan_id>.json` | The frozen plan. |
| `approvals/<plan_id>.json` | The signed approval record. |
| `inventory/inventory-<time>.json` | Each read-only inventory. |
| `inventory/handbook-<time>.html`, `.json` | Handbook snapshots with URL and retrieval time. |
| `state/state.json` | Local workflow state (`gentoo-agent status`). |

The MCP server logs to its standard error, which your MCP client captures.

## Credentials not shown

`run status` prints generated passwords once, and only in these conditions:

- At least one password was generated. Passwords that came from your
  environment variables or a prompt are never stored or shown.
- The run has ended. While the run is `starting` or `running`, nothing is
  shown.
- Nobody has read them yet. The first `run status` call after the run ended
  takes them and deletes the file. If an agent ran `run status` in its shell,
  it received them; in [protected mode](guides/protected-mode.md) the agent's
  shell cannot read `runs/`.
- They are not older than 24 hours. Any `run status` call deletes credentials
  that were written more than 24 hours earlier.
- You use the same work directory. In protected mode, set
  `GENTOO_AGENT_HOME` to the work directory and use the venv's
  `gentoo-agent`.

`get_run_status` never shows passwords. It reports `credentials_waiting` and
`credentials_applied`, and a `credentials_hint` with the command to run in your
terminal.

If the passwords are lost, log in on the target's console or with the SSH key
(the root key is always installed) and set new passwords with `passwd`.

## Secure Boot is on

`plan create` or `create_plan` refuses a UEFI install with `CONFIG_CONFLICT`
("Secure Boot is on, and the installed GRUB is not signed. ..."). The installed
GRUB is not signed, so the firmware would refuse to start it.

1. Turn Secure Boot off in the firmware setup.
2. Boot the live ISO again.
3. Refresh the inventory: `gentoo-agent inspect`, or `detect_hardware` with
   `refresh=true`. The summary should report `secure_boot` as `off`.
4. Create the plan again.

Shim and MOK enrollment are not supported. When the state cannot be read, the
plan carries a warning ("Secure Boot state is unknown; ...") instead of a
refusal: check the firmware setup before approving.

## Windows EFI partition not found

The bootloader apply printed `Windows EFI partition <uuid> not found; refresh
the inventory` and the run failed with `PHASE_FAILED`. The Windows disk was
detached or reformatted after the inventory was taken.

The line `Windows EFI partition <uuid> is not on <disk>; refresh the inventory`
means the UUID now resolves to a partition on another disk (for example a
cloned EFI partition), so the entry would boot something else.

Refresh the inventory, create and approve a new plan, and start it; completed
phases are skipped. A Windows disk that was swapped for another one stops the
run earlier with `TARGET_DRIFT`.

## Windows entry fails with no such device

Gentoo boots, but choosing `Windows Boot Manager (on <disk>)` in GRUB stops
with:

```text
error: .../grub-core/commands/search.c:grub_search_fs_uuid:371:no such device: <uuid>.
error: .../grub-core/fs/fshelp.c:find_file:260:file `/EFI/Microsoft/Boot/bootmgfw.efi' not found.
Press any key to continue...
```

The firmware boots with Fast Boot (quick boot) and connects only the device
it boots from, so GRUB cannot see the Windows disk at all. The second error
only follows from the first: GRUB looked for the Windows loader on the Gentoo
EFI partition. Windows itself and its EFI partition are intact, and the
installer did not write to them. This happens even when both disks sit on one
controller, for example two namespaces of one VMware virtual NVMe controller.

Fix it in the firmware, not in GRUB:

- **VMware (Workstation, Fusion, ESXi):** `efi.quickBoot` is on by default.
  Power the VM off completely, close it in VMware, add this line to its `.vmx`
  file, and start it again:

  ```text
  efi.quickBoot.enabled = "FALSE"
  ```

- **Physical hardware:** turn off "Fast Boot" (sometimes "Quick Boot" or
  "Ultra Fast Boot") in the firmware setup, and save.

To check before you change anything, press `c` in the GRUB menu and run `ls`.
When only the Gentoo disk and its partitions appear, the firmware did not
connect the Windows disk. To check that Windows itself is intact, boot
`Windows Boot Manager` from the firmware boot menu, or on the installed
Gentoo:

```sh
sudo mkdir -p /mnt/winesp
sudo mount -o ro "$(blkid -U <uuid>)" /mnt/winesp
find /mnt/winesp -maxdepth 4 -iname 'bootmgfw.efi'
sudo umount /mnt/winesp
```

Some distributions patch GRUB with a `connectefi` command for this case. The
upstream GRUB that Gentoo ships does not have it, so the generated menu entry
does not use it.

## BitLocker recovery prompt

After the first boot through the GRUB menu entry, Windows may ask once for its
BitLocker recovery key, because the boot path changed. Enter the key. Nothing
on the Windows disk was changed by the installer.

## Windows in BIOS mode

A Windows installed in BIOS mode (MBR, no EFI system partition) is reported as
`windows-bios`. The plan warns `Windows in BIOS mode on <disk> will not be in
the GRUB menu.` and this version never adds it to the menu. Either convert
Windows to UEFI (outside this tool), or boot it through the firmware boot menu.

## Windows disk on USB or removable media

Only the install disk must be fixed. A Windows disk on USB or other removable
media still gets a GRUB menu entry, and its identity joins the plan like any
other Windows disk with an entry. Unplugging it during a run stops the run with
`TARGET_DRIFT` before the next destructive phase. Plug it back in: if it comes
back under the same device path, `run resume <run_id>` (or `resume_run`) works
while the approval is still valid; otherwise inspect again and create and
approve a new plan. A Windows disk that is not connected when
the inventory is taken gets no entry.

## Windows

Native Windows is not supported for the steps that matter:

- `approve` needs `/dev/tty` and fails with `APPROVAL_REQUIRED`.
- `run start` and `run resume` need `fcntl` and fail with `CONFIG_INVALID`
  ("Background runs need macOS or Linux; on Windows use WSL.").
- The protected mode sandbox needs macOS or Linux/WSL2.

Dry run, `validate`, `inspect`, and `plan create` work on any platform. Use
WSL for the full workflow, with the repository, SSH key, and known hosts file
inside the WSL file system.
