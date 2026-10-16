# Installing Gentoo with the CLI

This guide walks through a complete Gentoo installation driven from the
`gentoo-agent` command line, from a booted live ISO to a system that is ready
for its first reboot. Do it on a disposable virtual machine first. Use physical
hardware only after the whole flow has worked on a VM.

The installer erases the target disk during the `storage` phase. Nothing
destructive runs until you have created a plan, read it, and approved it
yourself in your own terminal.

## Contents

- [Before you start](#before-you-start)
- [Step 1: Boot the target and start SSH](#step-1-boot-the-target-and-start-ssh)
- [Step 2: Verify and trust the host key](#step-2-verify-and-trust-the-host-key)
- [Step 3: Create the configuration](#step-3-create-the-configuration)
- [Step 4: Decide how passwords are set](#step-4-decide-how-passwords-are-set)
- [Step 5: Inspect the target](#step-5-inspect-the-target)
- [Step 6: Validate the configuration](#step-6-validate-the-configuration)
- [Step 7: Read the dry run](#step-7-read-the-dry-run)
- [Dual boot with Windows](#dual-boot-with-windows)
- [Step 8: Create a plan](#step-8-create-a-plan)
- [Step 9: Approve the plan](#step-9-approve-the-plan)
- [Step 10: Start and follow the run](#step-10-start-and-follow-the-run)
- [Step 11: Read generated credentials](#step-11-read-generated-credentials)
- [Step 12: Check the result and reboot](#step-12-check-the-result-and-reboot)
- [Repeating or recovering an installation](#repeating-or-recovering-an-installation)

## Before you start

You need:

| Item | Requirement |
|---|---|
| Operator machine | macOS or Linux. `approve`, `run start`, and `run resume` need a POSIX terminal and `fcntl`; on Windows use WSL. Dry run, `inspect`, and `plan create` work on any platform. |
| Python | 3.12 or newer. |
| OpenSSH client | `ssh` on `PATH` (and `ssh-keygen`, `ssh-keyscan` for the host key steps). |
| SSH key | A dedicated key pair for the live session. The public key must sit next to the private key as `<private key>.pub`, because the installer copies it into the installed system. |
| Live medium | A current Gentoo live ISO (for example the minimal installation CD) attached to the VM. |
| Target | A VM with one disk you are willing to erase, and network access from the operator machine. Supported architectures: amd64 (BIOS or UEFI) and arm64 (UEFI only). |

Get the code and, optionally, install the `gentoo-agent` command:

```sh
git clone https://github.com/Bajtlamer/gentoo-ai-installer-agent.git
cd gentoo-ai-installer-agent
python3 -m venv .venv
.venv/bin/pip install -e .
. .venv/bin/activate
```

Every command in this guide is shown as `gentoo-agent ...`. Without the
installation, `python3 tools/gentoo_agent.py ...` and `python3 -m gentoo_agent ...`
are equivalent. All files are read and written relative to the repository root,
or to `GENTOO_AGENT_HOME` when that variable is set.

Create the dedicated key:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/gentoo-live_ed25519 -C gentoo-live
```

## Step 1: Boot the target and start SSH

1. Boot the VM from the Gentoo live ISO and open its console.
2. Find the live system's address:

   ```sh
   ip -brief address
   ```

3. Give root a way to log in. Either set a temporary root password:

   ```sh
   passwd
   ```

   or paste your public key into `/root/.ssh/authorized_keys` on the console.

4. Start the SSH server (the Gentoo live ISO uses OpenRC):

   ```sh
   rc-service sshd start
   ```

5. If you set a password, install your key from the operator machine:

   ```sh
   ssh-copy-id -i ~/.ssh/gentoo-live_ed25519.pub root@<live-ip>
   ```

The installer connects with `BatchMode=yes`, so it can only use key
authentication, and the SSH user must be `root`: the dry run and
`plan create` refuse any other user with `CONFIG_INVALID` ("The installation
executor currently requires SSH user root.").

## Step 2: Verify and trust the host key

Every SSH call uses `StrictHostKeyChecking=yes` and `BatchMode=yes`. An unknown
or changed host key is never accepted automatically; the connection fails
instead. Verify the key out of band, through the console, and only then trust
it.

1. On the live console, print the host key fingerprint:

   ```sh
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

2. On the operator machine, fetch the key over the network and print its
   fingerprint:

   ```sh
   ssh-keyscan -t ed25519 <live-ip> > /tmp/gentoo-live.key
   ssh-keygen -lf /tmp/gentoo-live.key
   ```

3. Compare the two fingerprints character by character. If they differ, stop:
   you are not talking to the machine on your console.
4. Add the verified key to a known hosts file. Either use your default file:

   ```sh
   cat /tmp/gentoo-live.key >> ~/.ssh/known_hosts
   ```

   or keep a dedicated file and point `known_hosts_file` in
   `config/target.json` at it. A relative path is resolved from the
   repository root, and `ssh-known-hosts` in the root is already ignored by
   Git:

   ```sh
   cat /tmp/gentoo-live.key >> ssh-known-hosts
   ```

5. Test the connection exactly as the installer will make it:

   ```sh
   ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -i ~/.ssh/gentoo-live_ed25519 root@<live-ip> true
   ```

A live ISO usually creates new host keys every time it boots. After a reboot of
the live system, remove the old entry (`ssh-keygen -R <live-ip>`) and repeat
this step.

If you skip this step, or the key changed, `inspect` and the other commands
that connect fail with `HOST_KEY_UNKNOWN` ("The target's SSH host key is
unknown or has changed."). Do not add the key blindly: compare the fingerprints
as above, then add it. See
[SSH host key not trusted](../TROUBLESHOOTING.md#ssh-host-key-not-trusted).

## Step 3: Create the configuration

The installer reads two private files, both ignored by Git:

- `config/target.json`: how to reach the live system (address, port, user, key,
  known hosts file) and whether the target is disposable.
- `config/install.json`: what to install (disk, boot mode, layout, hostname,
  timezone, user, passwords).

[Configuration reference](../CONFIGURATION.md) describes every field. Create
the files with one of the two tools below.

### Option A: init_config.py (local only)

`tools/init_config.py` writes both files from the visible defaults in
`config/target.default.json` and `config/install.default.json`. It never
connects to the target.

```sh
python3 tools/init_config.py \
  --host <live-ip> \
  --disk /dev/sda \
  --identity-file ~/.ssh/gentoo-live_ed25519 \
  --name gentoo-live \
  --boot-mode uefi \
  --hostname gentoo \
  --timezone Europe/Prague \
  --username alex \
  --allow-disk-erase
```

Output:

```text
Created local configuration only:
  /path/to/gentoo-ai-installer-agent/config/target.json
  /path/to/gentoo-ai-installer-agent/config/install.json
Nothing can run until a plan is created and approved; review both files first.
```

What it writes:

- `--allow-disk-erase` is mandatory. Without it the tool stops with
  "Refusing to create an erase-enabled plan. Add --allow-disk-erase after
  confirming the exact target disk." With it, `target_is_disposable` and
  `disk_erase_authorized` are set to `true` in both files and
  `disk_must_be_erased` to `true` in `install.json`.
- The partition layout is generated from `--disk`, `--boot-mode` (`uefi` gives
  GPT with a 512 MiB EFI system partition; `bios` gives MBR) and `--swap-mb`
  (default 2048; 0 means no swap).
- Passwords default to `password_source: "environment"` with
  `GENTOO_ROOT_PASSWORD` and `GENTOO_USER_PASSWORD`, and
  `generate_missing_passwords: true`.
- The default identity file is `~/.ssh/id_rsa`; pass `--identity-file` for the
  dedicated key.
- Existing files are never replaced unless you add `--overwrite`.

Run `python3 tools/init_config.py --help` for every option (`--no-create-user`,
`--no-sudo`, `--no-root-password`, `--allow-root-password-ssh`,
`--password-source`, `--root-password-env`, `--user-password-env`, and more).
`--architecture amd64|arm64` (default `amd64`) must match the live system:
`inspect` and `plan create` refuse a different one, and arm64 needs
`--boot-mode uefi`. `--init-system` accepts only `openrc`: the generated
commands are written for OpenRC and systemd is refused everywhere.

For static networking, `network_method` and `network_static` (with the
`interface`, `address`, `gateway`, and `dns` keys) are edited in
`config/install.json` afterwards; see
[Static network](../CONFIGURATION.md#static-network).

An `install.json` written before the `architecture` field existed fails
`validate`, the dry run, and `plan create`. Add `"architecture": "amd64"` (or
`"arm64"`) to it.

### Option B: prepare_install.py (interactive, read-only discovery)

`tools/prepare_install.py` asks questions, connects to the live system with the
read-only command set, and writes `config/install.json`. It never partitions,
formats, installs, or reboots.

The tool asks for the erase decision itself (see below) and saves it in both
`config/target.json` and `config/install.json`, so you do not edit
`target.json` by hand. Run:

```sh
python3 tools/prepare_install.py
```

What it does, in order:

1. Creates `config/target.json` from the default template if it is missing,
   then asks for the live address, SSH user, private key path, and port, and
   saves them.
2. Runs the read-only inventory over SSH (`Read-only check: identity`,
   `Read-only check: boot_mode`, and so on) and saves it under `inventory/`.
3. Lists the eligible disks and asks which one to use, defaulting to the
   first. Removable, USB, read-only, and mounted disks are listed separately
   with the reason and cannot be chosen (the same rule `plan create`
   enforces); if no disk is eligible, the tool stops. It reads the
   architecture from the live system (it does not ask) and sets `architecture`
   and `handbook_url` in `target.json` from it. On arm64 the boot mode is
   `uefi` without a question. It then asks for boot mode (amd64 only),
   filesystem, swap, hostname, timezone, network, user, sudo, root password,
   root password SSH login, and password source. The init system is `openrc`
   without a question. For static networking it asks for the network
   interface on the installed system (take the name from the `network` section
   of the inventory, or `ip -brief address` on the live system), the address in
   CIDR form, the gateway, and the DNS servers.
4. Asks you to type `ERASE <disk>` for the disk chosen in this run, every
   time: an authorization saved by an earlier run never skips the question.
   Only the exact line authorizes erasure; any other answer sets
   `disk_must_be_erased`, `target_is_disposable`, and `disk_erase_authorized`
   to `false`, so an earlier authorization does not linger. The decision is
   saved in `target.json` as well as `install.json`.
5. Saves a Handbook snapshot under `inventory/` for the live architecture (a
   failure is only a warning).
6. Writes `config/install.json`, `config/target.json`, and `state/state.json`,
   prints the complete configuration and the result of local validation, and
   stops.

The boot mode question defaults to `uefi` whatever the live system reports, so
check the validation output for a boot mode conflict.

## Step 4: Decide how passwords are set

The installed system gets a root password (unless `set_root_password` is
`false`) and a password for the regular user (unless `create_user` is `false`).
Passwords never appear in configuration files, plans, commands, or logs. They
travel only over SSH standard input to `chpasswd`.

Choose one source with `password_source` in `config/install.json`:

| `password_source` | Behavior |
|---|---|
| `environment` (default) | Reads the variables named by `root_password_env` and `user_password_env` when you run `run start` or `run resume`. A missing or empty variable is replaced by a generated password when `generate_missing_passwords` is `true`, and is an error otherwise. |
| `generate` | Always generates random passwords. |
| `prompt` | `run start` asks for each password twice on the terminal (`New Gentoo root password:`, `Repeat Gentoo root password:`). |

Variable names must end in `_PASSWORD` or `_PW` (for example `ALEX_PW`).
System and credential variables such as `PATH`, `HOME`, or names starting with
`SSH_` or containing `TOKEN` or `SECRET` are refused. A password must not be
empty or contain a newline.

To supply your own passwords, set them in the same shell that will run
`run start`, without leaving them in your shell history:

```sh
read -rs GENTOO_ROOT_PASSWORD && export GENTOO_ROOT_PASSWORD
read -rs GENTOO_USER_PASSWORD && export GENTOO_USER_PASSWORD
```

The values are read once, before the background worker starts, and the
variables are removed from the worker's environment. Unset them after the run
has started:

```sh
unset GENTOO_ROOT_PASSWORD GENTOO_USER_PASSWORD
```

To let the installer generate passwords instead, leave the variables unset. You
read the generated values once with `run status` (see
[Step 11](#step-11-read-generated-credentials)).

## Step 5: Inspect the target

Skip this step if you used `prepare_install.py`; it already collected an
inventory.

```sh
gentoo-agent inspect
```

`inspect` runs only the built-in, read-only command allowlist (identity, boot
mode, `lsblk` views including disk serial and WWN, `blkid`, mounts, network,
kernel warnings, the partition types and filesystems, and the Secure Boot
state). Progress goes to stderr and the path of the saved
inventory to stdout:

```text
Inspecting identity...
Inspecting boot_mode...
Inspecting storage...
Inspecting storage_json...
Inspecting disk_identity...
Inspecting block_ids...
Inspecting mounts...
Inspecting network...
Inspecting errors...
Inspecting partitions...
Inspecting secure_boot...
/path/to/gentoo-ai-installer-agent/inventory/inventory-20261016T091204Z.json
```

If none of the commands reached the target, nothing is saved and the command
fails with `SSH_UNREACHABLE` ("Could not reach the target over SSH; check the
address, key, and host key."), or with `HOST_KEY_UNKNOWN` when ssh reports
"Host key verification failed". Read the inventory file and confirm that the
disk you plan to erase is the one you expect: size, model, serial, and WWN.
The `partitions` and `secure_boot` results are what `plan create` uses to find
Windows and to check Secure Boot; the summary that the MCP `detect_hardware`
tool builds from them reports `secure_boot` (`on`, `off`, or `unknown`) and an
`existing_os` per disk (see [Dual boot with Windows](#dual-boot-with-windows)).

Optionally record the Handbook source as well:

```sh
gentoo-agent handbook
```

## Step 6: Validate the configuration

```sh
gentoo-agent validate
```

`validate` checks `config/install.json` against `config/target.json` and the
latest inventory without contacting the target. It prints JSON and exits with
status 2 when there are errors:

```json
{
  "valid_for_execution": true,
  "errors": [],
  "warnings": [],
  "target": "gentoo-live",
  "installation": {
    "schema_version": 1,
    "profile": "gentoo-amd64-openrc",
    "boot_mode": "uefi",
    "partition_table": "gpt",
    "disk": "/dev/sda",
    "disk_must_be_erased": true,
    ...
  }
}
```

Typical errors are a configured disk that is not in the inventory, or a UEFI
install requested while the live system booted in BIOS mode ("desired UEFI
install conflicts with the latest live boot mode (bios-or-unknown); boot the VM
with EFI enabled and inspect again"). Fix the configuration or the VM settings
and run `inspect` and `validate` again.

`gentoo-agent status` prints the local workflow state from `state/state.json`.

## Step 7: Read the dry run

```sh
gentoo-agent install --dry-run
```

The dry run renders every phase's shell program from the current configuration.
It does not connect to the target. The output starts like this:

```text
Starting from the beginning; completed phases will be checked and skipped.
Target: root@192.0.2.10:22
Disk:   /dev/sda (uefi/gpt)
Mode:   DRY RUN - no SSH connection and no target changes

===== preflight =====
set -eu
disk=/dev/sda
test "$(id -u)" -eq 0
test -b "$disk"
...
```

Each `===== <phase> =====` block is the exact program for one phase, in this
order: `preflight`, `storage`, `filesystems`, `base-system`, `configuration`,
`authentication`, `kernel`, `bootloader`, `validation`. Read at least:

- the header: the target address and the disk;
- `storage`: the `sfdisk` partition table that will be written to the disk;
- `filesystems`: which partitions get which filesystem;
- `authentication`: which user is created and which groups it joins.

To print from one phase onward, use `--from-phase`:

```sh
gentoo-agent install --dry-run --from-phase kernel
```

`gentoo-agent phase <phase>` prints the dry run from that phase too, except
`gentoo-agent phase discovery`, which runs `inspect` and so contacts the
target.
`install --execute` no longer exists; it fails with `APPROVAL_REQUIRED` and
points you to plans.

## Dual boot with Windows

When Windows is installed in UEFI mode on another disk of the target, the
installer can keep it bootable. It adds an explicit GRUB menu entry for it,
frozen into the plan, and it never writes to the Windows disk.

Prerequisites:

- Windows is installed in UEFI mode on a disk other than the install disk.
- Secure Boot is off in the firmware setup. `plan create` refuses a UEFI
  install while it is on (`CONFIG_CONFLICT`), because the installed GRUB is not
  signed. An unknown state is only a `WARNING` line in the plan.
- You booted the live ISO in UEFI mode. After any firmware change, boot the
  live ISO again and run `gentoo-agent inspect` again: the plan is built from
  the inventory, not from the live system.

What the result looks like:

- GRUB lists Gentoo first (the default) and `Windows Boot Manager (on /dev/sdX)`.
- The bootloader phase writes `/etc/grub.d/45_gentoo_agent_dualboot` inside the
  new system and runs `grub-mkconfig`. Nothing on the Windows disk is mounted
  or written. The plan only records that disk's identity, so the run stops with
  `TARGET_DRIFT` if the disk is gone or swapped.
- Windows may ask once for its BitLocker recovery key after the first boot
  through GRUB, because the boot path changed. Have the key ready.

In the plan summary, the `Dual boot:` line names the Windows disk and its EFI
partition UUID, and the Windows disk appears as `Windows disk (kept, not
written)` below the `Disk (erased)` line:

```text
Dual boot: Windows on /dev/sda (EFI UUID ABCD-1234) will be added to the GRUB menu. Gentoo becomes the default; Windows may ask once for its BitLocker recovery key.
...
Disk (erased): /dev/nvme0n1  size=512110190592 bytes  model=NVMe  serial=N1  wwn=none
Windows disk (kept, not written): /dev/sda  size=256060514304 bytes  model=WinDisk  serial=W1  wwn=none
```

To leave Windows out, set `"dual_boot": "off"` in `config/install.json`, or
write the file with `init_config.py --no-dual-boot`. With `off`, the plan
carries the warning `Windows on <disk> will not be added to the GRUB menu
(dual_boot is off).` Windows installed in BIOS mode is never added to the menu
in this version; the plan warns about it (see
[Windows in BIOS mode](../TROUBLESHOOTING.md#windows-in-bios-mode)).

Erasing a disk that holds Windows (`windows`, `windows-bios`, or `windows-data`
in `existing_os`; the rule is in the
[configuration reference](../CONFIGURATION.md#disk-erase-authorization)):

- `resolve_config` never infers it. Name it explicitly as the install disk, and
  only when you mean to erase it.
- `prepare_install.py` prints `WARNING: <disk> contains Windows (...). It will be
  erased.` and asks you to type `ERASE WINDOWS <disk>` instead of
  `ERASE <disk>`.
- The plan summary starts with that `WARNING` line, and `approve` asks for the
  phrase `APPROVE <plan_id> ERASE WINDOWS <disk>`. An approval without it does
  not start a run (`APPROVAL_REQUIRED`).

The configuration fields are described in the
[configuration reference](../CONFIGURATION.md#configinstalljson), and failures
in [Troubleshooting](../TROUBLESHOOTING.md).

## Step 8: Create a plan

```sh
gentoo-agent plan create
```

A plan freezes the validated configuration, the SSH target, the identity of the
target disk (path, size, model, serial, WWN from the latest inventory), and
every generated command into `plans/<plan_id>.json`. `plan_hash` is a SHA-256
over all of it, and `plan_id` is its first 16 characters. An approval covers
exactly that hash, so changing anything means a new plan.

Example output:

```text
Plan: c275199989470858
Plan hash: c27519998947085810ecce233153fc9272cda9e2327f63d906b168c710e8e31e
Disk erase: YES, all data on the disk will be destroyed
Destructive phases: storage, filesystems, base-system, configuration, authentication, kernel, bootloader
Target: root@192.0.2.10:22 (gentoo-live)
Created: 2026-10-16T08:36:06+00:00
Install: boot=uefi table=gpt hostname=gentoo user=gentoo init=openrc
Network: dhcp
Passwords: source=environment root_password_env=GENTOO_ROOT_PASSWORD user_password_env=GENTOO_USER_PASSWORD
Access: allow_root_password_ssh=no configure_sudo=yes user_groups=wheel
Disk (erased): /dev/sda  size=42949672960 bytes  model=VMware Virtual S  serial=6000c29a1b2c3d4e  wwn=0x6000c29a1b2c3d4e

Plan saved: c275199989470858
Approve it yourself in a terminal: gentoo-agent approve c275199989470858
```

How to read the summary:

| Line | Meaning |
|---|---|
| `Disk erase` | Always comes first. `YES` means the storage phase destroys all data on the disk. |
| `Destructive phases` | The phases that change the target. `preflight` and `validation` only read. |
| `Target` | The SSH user, address, port, and target name the run will connect to. |
| `Install` | Boot mode, partition table, hostname, regular user, init system. |
| `Network` | `dhcp`, or the static address, gateway, and DNS servers. |
| `Passwords` | The password source and, for `environment`, the variable names (never values). |
| `Access` | Root password SSH login, sudo, and the user's groups. |
| `Dual boot` | One line per Windows that gets a GRUB menu entry. Absent when there is none. |
| `Disk (erased)` | The exact disk the plan is bound to and erases. A `WARNING` line follows when the disk has no serial or WWN. |
| `Windows disk (kept, not written)` | A Windows disk with a menu entry. It is only read and drift-checked. |
| `WARNING` | Plan warnings (for example an unknown Secure Boot state) follow `Destructive phases`; a line first in the summary says the install disk holds Windows. |

`plan create` refuses when the configuration does not fully authorize erasing
the disk (`CONFIG_INVALID` with the missing flags listed), when there is no
inventory (`INVENTORY_MISSING`), and when the disk has no serial or WWN and
another disk has the same size and model (`DISK_AMBIGUOUS`).

Show a stored plan and its approval status at any time:

```sh
gentoo-agent plan show <plan_id>
```

## Step 9: Approve the plan

Approve the plan yourself, in your own interactive terminal. Never let an agent
run this command.

```sh
gentoo-agent approve <plan_id>
```

The command reads from `/dev/tty`, not from standard input. It prints the same
summary as `plan create`, then asks for the exact phrase:

```text
Plan: c275199989470858
Plan hash: c27519998947085810ecce233153fc9272cda9e2327f63d906b168c710e8e31e
Disk erase: YES, all data on the disk will be destroyed
...
Disk (erased): /dev/sda  size=42949672960 bytes  model=VMware Virtual S  serial=6000c29a1b2c3d4e  wwn=0x6000c29a1b2c3d4e

Type APPROVE c275199989470858 to approve this plan for 60 minutes:
```

Type `APPROVE <plan_id>` exactly. If the plan erases a disk that holds
Windows, the prompt asks for `APPROVE <plan_id> ERASE WINDOWS <disk>` instead
(see [Dual boot with Windows](#dual-boot-with-windows)). On success:

```text
Approved until 2026-10-16T09:41:06+00:00. Start with: gentoo-agent run start c275199989470858
```

Anything else ends with `error [APPROVAL_DECLINED]: Approval not recorded.`
Without a terminal the command fails with `error [APPROVAL_REQUIRED]: approve
needs an interactive terminal; run it yourself in your own terminal.`

The approval is written to `approvals/<plan_id>.json`, bound to the plan hash,
signed with the key in `secrets/approval.key` (created on the first approval),
and valid for 60 minutes. Approve again if it expires.

## Step 10: Start and follow the run

Start the run within the 60 minutes, in the shell that holds your password
variables (if you use them):

```sh
gentoo-agent run start <plan_id>
```

```text
Run started: 20261016T084210Z-3fa9c1
Follow it with: gentoo-agent run status 20261016T084210Z-3fa9c1  and  gentoo-agent run log 20261016T084210Z-3fa9c1
```

The run continues in a detached background worker, so you can close the
terminal. Only one run can be active at a time. Before any phase runs, the
worker re-checks the approval and reads the live disk identity again; a
different disk stops the run with `TARGET_DRIFT`.

For each phase the worker runs the phase's read-only check first. If the check
passes, the phase is already complete and is skipped. Otherwise it runs the
phase and then the check again as verification. A failure stops the run.

### run status

```sh
gentoo-agent run status <run_id>
```

Prints the run record from `runs/<run_id>/run.json`:

```json
{
  "run_id": "20261016T084210Z-3fa9c1",
  "plan_id": "c275199989470858",
  "plan_hash": "c27519998947085810ecce233153fc9272cda9e2327f63d906b168c710e8e31e",
  "status": "running",
  "pid": 48211,
  "started_at": "2026-10-16T08:42:10+00:00",
  "phase_ids": null,
  "phase_count": 9,
  "phase_index": 4,
  "current_phase": "base-system",
  "completed_phases": [
    "preflight",
    "storage",
    "filesystems"
  ],
  "last_error": null,
  "resumed_from": null,
  "updated_at": "2026-10-16T08:51:37+00:00"
}
```

`status` is one of:

| Status | Meaning |
|---|---|
| `starting` | The worker was launched and has not claimed the run yet. |
| `running` | The worker is executing phases. |
| `installed_pending_reboot` | Every phase completed. The target was not rebooted. |
| `failed` | A phase or a pre-check failed; `last_error` and `error` say why. |
| `cancelled` | Stopped at a phase boundary after `run cancel`. |
| `interrupted` | The worker died without recording a result, or was interrupted. |

### run log

```sh
gentoo-agent run log <run_id>
gentoo-agent run log <run_id> --offset 50 --limit 50
```

Pages through `runs/<run_id>/log.jsonl` (default offset 0, limit 50, maximum
500). Each entry is one check, apply, or verify step with its command, return
code, and output:

```json
{
  "entries": [
    {
      "phase": "preflight",
      "kind": "check",
      "label": "preflight-check",
      "command": "set -eu\ndisk=/dev/sda\n...",
      "returncode": 0,
      "stdout": "preflight-ok boot=uefi disk=/dev/sda\n",
      "finished_at": "2026-10-16T08:42:14+00:00"
    }
  ],
  "next_offset": 1,
  "total": 1
}
```

Pass `next_offset` as `--offset` to read the next page. The `base-system` and
`kernel` phases take a long time; their output arrives when each step ends.

### run cancel

```sh
gentoo-agent run cancel <run_id>
```

```json
{
  "run_id": "20261016T084210Z-3fa9c1",
  "cancel_requested": true,
  "status": "running"
}
```

The worker stops before the next phase; the phase in progress finishes first.
For a run that is no longer active, `cancel_requested` is `false`.

### run resume

```sh
gentoo-agent run resume <run_id>
```

```text
Run started: 20261016T093002Z-81b0d4 (resuming 20261016T084210Z-3fa9c1)
```

Starts a new run of the same plan, with the same phase selection. Completed
phases are detected by their checks and skipped. The plan's approval must
still be valid, and passwords are read again from the current environment.

## Step 11: Read generated credentials

If any password was generated, `run status` prints it once, after the run has
ended (it is not shown while the run is `starting` or `running`):

```text
Generated credentials (were applied; shown once, save them now):
  root: <generated password>
  alex: <generated password>
```

The wording in parentheses tells you whether the passwords are on the target:

| Wording | Meaning |
|---|---|
| `were applied` | The run finished with `installed_pending_reboot`; the authentication phase set these passwords. |
| `were generated but the authentication phase did not run` | They were never set on the target. |
| `may have been applied (...)` | The authentication phase was attempted, or the worker stopped early. Try them, and set new ones from the console if they fail. |

The credentials live in `runs/<run_id>/credentials.json` (mode 0600) only until
they are shown, and are deleted then. If nobody reads them, the next `run status`
call more than 24 hours after they were written deletes them. Save them in a
password manager and change them after the first login.

## Step 12: Check the result and reboot

The installer never reboots the target. When `run status` reports
`installed_pending_reboot`:

1. Read the end of the log, in particular the `validation` phase:

   ```sh
   gentoo-agent run log <run_id> --offset <total minus 5> --limit 5
   ```

2. Save any generated credentials (Step 11).
3. On the live console, look at the installed system, for example
   `findmnt -R /mnt/gentoo` and `ls /mnt/gentoo/boot`.
4. Unmount and reboot from the console, as described in the Handbook section
   "Rebooting the system". Remove the ISO from the VM's virtual drive (or
   change the boot order) so the VM boots from the disk.
5. After the reboot, the installed system has its own SSH host key. Verify it
   on the console as in Step 2, then log in as the regular user:

   ```sh
   ssh -i ~/.ssh/gentoo-live_ed25519 alex@<installed-ip>
   ```

   Root SSH login is key-only unless you set `allow_root_password_ssh`. With
   `configure_sudo`, `sudo -i` asks for the regular user's password.

## Repeating or recovering an installation

After a failure, the run's log and record stay under `runs/<run_id>/` and the
target is left running, so you can look at it on the console.

1. Read `run status <run_id>` (`last_error`, `error.code`, `error.details`) and
   the end of `run log <run_id>`.
2. Look up the error code in [Troubleshooting](../TROUBLESHOOTING.md).
3. Choose how to continue:
   - The cause was temporary (network, mirror, a fix on the console): run
     `gentoo-agent run resume <run_id>`. Approve the plan again first if the
     60 minutes have passed.
   - The configuration must change: the failed run usually left the disk
     mounted at `/mnt/gentoo` with swap on, and `plan create` refuses a
     mounted disk (`CONFIG_CONFLICT` "cannot be installed to: mounted at
     ..."). On the live system run `umount -R /mnt/gentoo` and
     `swapoff <planned swap partition>` (the `linux-swap` entry of
     `partition_layout` in `config/install.json`, for example `/dev/sda2` or
     `/dev/nvme0n1p2` with UEFI and `/dev/sda1` with BIOS), or reboot the
     live ISO (and verify the new host key, Step 2). Then run
     `inspect` again, edit `config/install.json`, and run `validate`,
     `install --dry-run`, `plan create`, `approve`, and `run start` with the
     new plan. Never edit files under `plans/` or
     `approvals/`; a modified plan is refused with `PLAN_HASH_MISMATCH`.
   - The live system was rebooted: verify the new host key (Step 2), run
     `inspect` again, and create and approve a new plan. The old plan still
     names the old disk identity and inventory.

To install again from scratch on the same VM, boot the live ISO again and
repeat the guide from Step 1. Because `inspect` saves a new inventory, the new
plan gets a new `plan_id`.

For an AI-driven installation over MCP, see [Installing with an AI
client](ai-install.md).
