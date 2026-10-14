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
| `tools/gentoo_agent.py` | Yes | Read-only inspection and validation | Commit |
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
| `execution_enabled` | boolean | Must be `false` during review |

Optional fields:

| Field | Type | Meaning |
|---|---|---|
| `known_hosts_file` | string | Dedicated SSH known-hosts file; empty uses the default |
| `expected_target_description` | string | Human-readable target description |
| `dangerous_operations_enabled` | boolean | Additional safety flag; keep `false` until explicitly approved |

## `config/install.json`

Required fields:

| Field | Type | Example |
|---|---|---|
| `profile` | string | `gentoo-amd64-openrc` |
| `boot_mode` | string | `uefi` or `bios` |
| `partition_table` | string | `gpt` for UEFI |
| `disk` | string | `/dev/sda` |
| `disk_must_be_erased` | boolean | Explicit destructive decision |
| `partition_layout` | array | Proposed partitions and mount points |
| `root_filesystem` | string | `ext4` |
| `swap_mb` | integer | `2048` |
| `bootloader` | string | `grub` |
| `init_system` | string | `openrc` or `systemd` |
| `hostname` | string | `gentoo` |
| `timezone` | string | `Europe/Prague` |
| `network_method` | string | `dhcp` |
| `execution_enabled` | boolean | Must be `false` during review |
| `review_status` | string | `pending_operator_review` |

Optional identity fields:

| Field | Type | Meaning |
|---|---|---|
| `expected_disk_model` | string | Extra identity check for the selected disk |
| `expected_disk_size_gib` | number | Extra size check for the selected disk |
| `network_static` | object | Required only when `network_method` is `static` |
| `schema_version` | integer | Configuration schema version |

## Current proposed layout

The current local configuration proposes:

```text
/dev/sda1   512 MiB   EFI System Partition, vfat, mounted at /boot
/dev/sda2   2048 MiB  Linux swap
/dev/sda3   remaining Linux root, ext4, mounted at /
```

This is a proposal for review, not an authorization to execute. UEFI must be
enabled in the VM firmware before the live environment is inspected again.

## What is optional and what is not

The Handbook can supply command details, package names, and architecture
specific procedure. It cannot supply the user's target decisions: which disk
may be erased, the desired filesystem, swap size, hostname, timezone, or
network behavior. Those decisions must be present in `config/install.json`.

The agent may derive harmless details such as interface names or current DHCP
addresses from read-only inspection. It must not derive destructive choices
such as the disk to erase from a guessed device name.
