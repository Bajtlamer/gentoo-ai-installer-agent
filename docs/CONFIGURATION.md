# Configuration reference

The project uses UTF-8 JSON files. JSON requires double quotes, commas between
fields, and no comments. Validate files with:

```powershell
python -c "import json; [json.load(open(f,encoding='utf-8')) for f in ['config/target.json','config/install.json','config/plan.json','state/state.json']]; print('JSON validation: OK')"
```

## Minimum setup

For a new checkout, either copy the visible defaults and edit them:

```powershell
Copy-Item config/target.default.json config/target.json
Copy-Item config/install.default.json config/install.json
```

or run the local-only initializer:

```powershell
python tools/init_config.py `
  --host 192.168.225.128 `
  --disk /dev/sda `
  --identity-file '~/.ssh/id_rsa' `
  --allow-disk-erase
```

The initializer creates `config/target.json` and `config/install.json` from the
same visible defaults. It never connects to SSH, changes the target, partitions
disks, or enables execution. It refuses to overwrite existing configuration
unless `--overwrite` is explicitly supplied.

Then review the generated files and run:

```powershell
python tools/gentoo_agent.py validate
```

For the complete beginner workflow, use one command:

```powershell
python tools/prepare_install.py
```

It reads or creates `target.json`, tests SSH, collects the read-only inventory,
asks the installation questions, writes `install.json`, and prints the complete
configuration for review. It does not start installation.

## Required files

| File | Required? | Purpose | Git policy |
|---|---:|---|---|
| `AGENTS.md` | Yes | Safety and agent operating rules | Commit |
| `config/target.json` | Yes | SSH connection and target identity | Private/ignored |
| `config/install.json` | Yes | Installation decisions | Private/ignored |
| `config/plan.json` | Yes | Ordered phases and implementation status | Commit |
| `state/state.json` | Yes at runtime | Resumable local state | Private/ignored |
| `gentoo_agent/` | Yes | Installer package: `core/` logic, `cli.py`, `prepare.py` | Commit |
| `tools/*.py` | Yes | Compatibility entry points for the package | Commit |
| `roles/*.md` | Recommended | Role-specific agent instructions | Commit |
| `phases/` | Required before execution | Version-controlled phase contracts/scripts | Commit |
| `inventory/`, `logs/`, `approvals/` | Generated | Evidence, transcripts, and approvals | Private/ignored |

The `*.example.json` files are safe templates for GitHub. Do not commit the
real target configuration, private key, logs, inventory, or approvals.

The `*.default.json` files are the canonical minimal/default configuration
examples. They are intentionally safe: disk erasure and execution are false.
Copy them only after replacing the example address and confirming every target
specific value.

## `config/target.json`

Required fields:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | Stable local target name |
| `host` | string | IP address or resolvable hostname |
| `port` | integer | SSH port, normally `22` |
| `user` | string | SSH user; `root` is expected in the live image |
| `identity_file` | string | Path to the dedicated SSH private key |
| `architecture` | string | Gentoo architecture, for example `amd64` |
| `handbook_url` | string | Official Handbook URL for that architecture |
| `install_config` | string | Usually `config/install.json` |
| `execution_enabled` | boolean | `false` during review; enabled only by `approve-install` |

Optional fields:

| Field | Type | Meaning |
|---|---|---|
| `known_hosts_file` | string | Dedicated SSH known-hosts file; empty uses the default |
| `expected_target_description` | string | Human-readable target description |
| `target_is_disposable` | boolean | Marks the target as intentionally disposable |
| `disk_erase_authorized` | boolean | Pre-authorizes erasing the selected disk when disposable |
| `dangerous_operations_enabled` | boolean | Additional safety flag; keep `false` until explicitly approved |

## `config/install.json`

Required fields:

| Field | Type | Example |
|---|---|---|
| `profile` | string | `gentoo-amd64-openrc` |
| `boot_mode` | string | `uefi` or `bios` |
| `partition_table` | string | `gpt` for UEFI, `mbr` for BIOS |
| `disk` | string | `/dev/sda` |
| `disk_must_be_erased` | boolean | Explicit destructive decision |
| `target_is_disposable` | boolean | Must be true for pre-authorized disk erasure |
| `disk_erase_authorized` | boolean | Must be true for pre-authorized disk erasure |
| `partition_layout` | array | Proposed partitions and mount points |
| `root_filesystem` | string | `ext4` |
| `swap_mb` | integer | `2048` |
| `bootloader` | string | `grub` |
| `init_system` | string | `openrc` or `systemd` |
| `hostname` | string | `gentoo` |
| `timezone` | string | `Europe/Prague` |
| `network_method` | string | `dhcp` |
| `create_user` | boolean | Create a regular installed login account |
| `username` | string | Regular account name when `create_user` is true |
| `install_user_ssh_key` | boolean | Also install the live-session public key for the regular user |
| `user_groups` | array | Supplementary groups, normally `["wheel"]` |
| `configure_sudo` | boolean | Optional; defaults to `true` and enables `%wheel ALL=(ALL:ALL) ALL` |
| `user_shell` | string | Login shell, normally `/bin/bash` |
| `set_root_password` | boolean | Set the root console password; the default environment mode does not prompt |
| `allow_root_password_ssh` | boolean | Allow root password authentication over SSH |
| `password_source` | string | `environment` by default; `prompt` is an explicit interactive override |
| `generate_missing_passwords` | boolean | Generate random passwords when environment values are absent |
| `root_password_env` | string | Environment variable name for the root password |
| `user_password_env` | string | Environment variable name for the regular-user password |
| `execution_enabled` | boolean | `false` during review; enabled only by `approve-install` |
| `review_status` | string | `pending_operator_review`, `reviewed_execution_disabled`, or `approved_for_execution` |

Optional identity fields:

| Field | Type | Meaning |
|---|---|---|
| `expected_disk_model` | string | Extra identity check for the selected disk |
| `expected_disk_size_gib` | number | Extra size check for the selected disk |
| `network_static` | object | Required only when `network_method` is `static` |
| `schema_version` | integer | Configuration schema version |

### Installed login credentials

The live environment and the installed system are separate. The live
environment is accessed as the configured SSH user, normally `root`, with the
private key in `target.json`. During the `configuration` phase the agent
installs `net-misc/openssh`, copies the corresponding public key to
`/root/.ssh/authorized_keys`, enables `sshd` and `dhcpcd`, and configures root
SSH access as key-only.

The `authentication` phase then installs sudo when requested, creates the configured regular user and sets
passwords without interaction by default. Passwords are never stored in JSON, command logs, or
the Git repository. The agent sends them only through the SSH command's
standard input to `chpasswd`. The example configuration creates user `gentoo`,
while the tracked default configuration creates user `bajt`; either account is
added to `wheel`. When `configure_sudo` is true, the phase also enables the
standard `%wheel ALL=(ALL:ALL) ALL` sudoers rule. The configured regular-user
password and a root console password are set without prompting during installation.

For unattended execution, which is the default, the agent reads the variable
names configured by `root_password_env` and
`user_password_env`; the password values themselves remain outside the JSON
configuration and are never written to logs. In PowerShell, explicitly
provided values can be supplied as follows:

```powershell
$env:GENTOO_ROOT_PASSWORD = "choose-a-strong-root-password"
$env:GENTOO_USER_PASSWORD = "choose-a-different-user-password"
python tools/gentoo_agent.py install --execute --confirm "INSTALL gentoo ON /dev/sda"
Remove-Item Env:GENTOO_ROOT_PASSWORD, Env:GENTOO_USER_PASSWORD
```

With `generate_missing_passwords: true`, missing environment values are
replaced by random passwords. The generated credentials are printed once after
all phases complete and are not saved by the agent. Save them and change them
after first login. Anyone who can see the terminal or captured console output
can read them, so this fallback is intended for disposable or controlled
targets.

Use `password_source: "prompt"` only when manually entering passwords is
deliberately required.

The regular user is the normal post-reboot SSH login:

```powershell
ssh -i "$env:USERPROFILE\.ssh\id_rsa" gentoo@<installed-ip>
```

`sudo -i` asks for the regular user's password, not the root password. SSH key
authentication does not replace the password used by sudo.

When `install_user_ssh_key` is true, the same public key is copied to the
regular user's `~/.ssh/authorized_keys` with the correct ownership and mode.
Set it to `false` if the regular user should use password authentication only.

Root SSH login remains key-only because the generated configuration contains
`PermitRootLogin prohibit-password` by default. Set
`allow_root_password_ssh` to `true` when root password login should be the
primary SSH method; the root public key remains installed as a fallback. This
is less secure than key-only root SSH and should be limited to disposable or
otherwise controlled targets.

## Current proposed layout

The tracked default proposes a UEFI layout:

```text
/dev/sda1   512 MiB   EFI System Partition, vfat, mounted at /boot
/dev/sda2   2048 MiB  Linux swap
/dev/sda3   remaining Linux root, ext4, mounted at /
```

The active disposable VM currently uses BIOS/MBR instead:

```text
/dev/sda1   2048 MiB  Linux swap
/dev/sda2   remaining Linux root, ext4, mounted at /
```

This is a proposal for review. For the active disposable VM, disk erasure is
explicitly pre-authorized, but execution remains disabled. The executor checks
the live boot mode and exact disk model/size again immediately before storage
changes.

## Starting the guarded executor

The default command is read-only and prints generated commands:

```powershell
python tools/gentoo_agent.py install --dry-run
```

After reviewing the files and dry-run output, type the exact phrase requested
by:

```powershell
python tools/gentoo_agent.py approve-install
```

Then start with the confirmation printed by that command. The executor logs
each phase under `logs/`, stops on failure, and never reboots automatically.
Every phase is checked against the actual target before it is applied and
verified again afterward. After a failure, rerun the same command: it starts
at the beginning and skips phases whose target state is already complete. Use
`--from-phase` only when an explicit restart point is required.

## What is optional and what is not

The Handbook can supply command details, package names, and architecture
specific procedure. It cannot supply the user's target decisions: which disk
may be erased, the desired filesystem, swap size, hostname, timezone, or
network behavior. Those decisions must be present in `config/install.json`.

The agent may derive harmless details such as interface names or current DHCP
addresses from read-only inspection. It must not derive destructive choices
such as the disk to erase from a guessed device name.
