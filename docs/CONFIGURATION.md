# Configuration reference

This page lists every configuration file, field, limit, and rule the code
enforces. It does not describe procedures. For those, see:

- [CLI installation guide](guides/cli-install.md): creating the configuration,
  inspecting the target, creating, approving, and running a plan, and supplying
  passwords.
- [AI installation guide](guides/ai-install.md): driving the same workflow from
  an AI client through the MCP server.
- [Protected mode guide](guides/protected-mode.md): running the MCP server and
  the agent so that the agent cannot approve plans itself.

All configuration files are UTF-8 JSON objects: double quotes, commas between
fields, and no comments. A file that is missing, unreadable, or not a JSON
object raises `CONFIG_INVALID`.

## Contents

- [Files and directories](#files-and-directories)
- [Creating the private configuration](#creating-the-private-configuration)
- [config/target.json](#configtargetjson)
- [config/install.json](#configinstalljson)
- [Validation rules](#validation-rules)
- [Disk erase authorization](#disk-erase-authorization)
- [Passwords](#passwords)
- [Installed login access](#installed-login-access)
- [Architecture](#architecture)
- [Static network](#static-network)
- [Plan creation requirements](#plan-creation-requirements)
- [Partition layout and swap](#partition-layout-and-swap)
- [Approval records and the signing key](#approval-records-and-the-signing-key)
- [MCP configuration resolution](#mcp-configuration-resolution)
- [Phase selection](#phase-selection)
- [What the configuration must decide](#what-the-configuration-must-decide)

## Files and directories

All paths are relative to the repository root. The environment variable
`GENTOO_AGENT_HOME` replaces that root for every path below, which is how a
protected mode setup keeps its work directory outside the checkout.

| Path | Purpose | Git policy |
|---|---|---|
| `config/target.json` | SSH connection and target identity | Private, gitignored |
| `config/install.json` | Installation decisions used by the CLI | Private, gitignored |
| `config/target.default.json`, `config/install.default.json` | Safe defaults: disk erasure disabled. `install.default.json` is also the defaults layer of the MCP `resolve_config` tool | Committed |
| `config/target.example.json`, `config/install.example.json` | Example templates | Committed |
| `config/plan.json` | Ordered phase list with descriptions, `implemented`, and `destructive` flags | Committed |
| `state/state.json` | Local workflow state written by `inspect`, `detect_hardware(refresh=true)`, and `prepare_install.py`, and shown by `status` | Private, gitignored |
| `inventory/` | Read-only SSH inventories and Handbook snapshots | Private, gitignored |
| `logs/` | Gitignored log files; the current code writes nothing here | Private, gitignored |
| `plans/<plan_id>.json` | Frozen install plans | Private, gitignored |
| `approvals/<plan_id>.json` | Signed approval records | Private, gitignored |
| `runs/<run_id>/` | Run record, log, worker errors, one-time credentials | Private, gitignored |
| `secrets/approval.key` | HMAC key that signs approval records | Private, gitignored |

Keep the private files private. Do not commit the real target configuration,
private keys, passwords, inventories, logs, plans, approvals, runs, or
secrets, and never put a private key or a password anywhere in the
repository. The committed `*.default.json` and `*.example.json` files are safe
examples: disk erasure is disabled in them.

## Creating the private configuration

`python tools/init_config.py` writes `config/target.json` and
`config/install.json` from the default files. It never connects to the target.
It requires `--host` and `--disk`, refuses to run without `--allow-disk-erase`,
and refuses to overwrite existing files unless `--overwrite` is given. With
`--allow-disk-erase` it sets `target_is_disposable` and
`disk_erase_authorized` in both files and `disk_must_be_erased` in
`install.json`. `--architecture amd64|arm64` (default `amd64`) sets
`install.architecture` and the profile; `arm64` with `--boot-mode bios` is
refused. Run `python tools/init_config.py --help` for every option.

`python tools/prepare_install.py` is the interactive alternative: it connects
read-only over SSH, asks the installation questions, and writes
`install.json`. It reads the architecture from the live system (it does not
ask), sets `architecture` and `handbook_url` in `target.json` from it, and
records the erase decision in `target.json` as well as `install.json`. It
asks for `ERASE <disk>` on every run, for the disk chosen in that run; any
other answer sets the erase flags in both files to `false`. It does
not start an installation. See the
[CLI installation guide](guides/cli-install.md) for both.

`python tools/gentoo_agent.py validate` checks the configuration locally
against the latest inventory. It exits with status 2 when it finds errors.

## config/target.json

| Field | Type | Required | Meaning |
|---|---|---|---|
| `name` | string | Yes | Stable local target name |
| `host` | string | Yes | IP address or resolvable hostname of the live system |
| `port` | integer | No | SSH port; omitted means the ssh default (22) |
| `user` | string | Yes | SSH user; plan creation and dry runs require `root` |
| `identity_file` | string | Yes | Path to the SSH private key; `~` is expanded |
| `architecture` | string | Yes | Gentoo architecture, `amd64` or `arm64`; informational, the install configuration decides (see [Architecture](#architecture)) |
| `handbook_url` | string | Yes | Official Handbook URL for that architecture, used by `handbook` |
| `install_config` | string | No | Path of the install file relative to the root; default `config/install.json` |
| `known_hosts_file` | string | No | Dedicated known-hosts file; empty uses the ssh default |
| `expected_target_description` | string | No | Human-readable description |
| `target_is_disposable` | boolean | No | Marks the target as intentionally disposable |
| `disk_erase_authorized` | boolean | No | Authorizes erasing the selected disk |

Rules:

- SSH runs with `BatchMode=yes` and `StrictHostKeyChecking=yes`. The host key
  must already be in the known-hosts file; the agent never accepts a new key.
- The public key must exist next to the private key as `<identity_file>.pub`
  (or with the private key's suffix replaced by `.pub`) and must be exactly one
  line. The `configuration` and `authentication` phases install it on the
  target.
- A plan freezes `name`, `host`, `port`, `user`, `identity_file`, and
  `known_hosts_file`. Changing them later requires a new plan.

## config/install.json

The CLI reads this file. The MCP server does not: it builds an install
configuration with `resolve_config` (see
[MCP configuration resolution](#mcp-configuration-resolution)).

Fields that `validate` reports as missing when absent:

| Field | Type | Allowed values |
|---|---|---|
| `architecture` | string | `amd64` or `arm64`; see [Architecture](#architecture) |
| `profile` | string | Descriptive label such as `gentoo-amd64-openrc` (`resolve_config` derives it as `gentoo-<architecture>-<init_system>`); not used by the generated commands |
| `boot_mode` | string | `uefi` or `bios` |
| `partition_table` | string | `gpt` (required for UEFI) or `mbr` |
| `disk` | string | Disk path such as `/dev/sda` |
| `partition_layout` | array | Partitions; see [Partition layout and swap](#partition-layout-and-swap) |
| `root_filesystem` | string | `ext4` only |
| `swap_mb` | integer | 0 to 131072; 0 means no swap |
| `bootloader` | string | `grub` |
| `init_system` | string | `openrc`; see the note below |
| `hostname` | string | Simple DNS-compatible hostname |
| `timezone` | string | Relative zoneinfo name such as `Europe/Prague` |
| `network_method` | string | `dhcp` or `static` |

Fields with a default when absent:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `disk_must_be_erased` | boolean | none | Explicit destructive decision; see [Disk erase authorization](#disk-erase-authorization) |
| `target_is_disposable` | boolean | none | Must match the target file for erasure |
| `disk_erase_authorized` | boolean | none | Must match the target file for erasure |
| `network_static` | object | none | Required when `network_method` is `static`; see [Static network](#static-network) |
| `create_user` | boolean | `true` | Create a regular login account |
| `username` | string | none | Regular account name; required when `create_user` is true |
| `user_groups` | array of strings | `["wheel"]` | Supplementary groups |
| `user_shell` | string | `/bin/bash` | Must be `/bin/bash` |
| `install_user_ssh_key` | boolean | `true` | Install the live-session public key for the regular user |
| `configure_sudo` | boolean | `true` | Install `app-admin/sudo` and add `%wheel ALL=(ALL:ALL) ALL` to `/etc/sudoers` |
| `set_root_password` | boolean | `true` | Set the root console password |
| `allow_root_password_ssh` | boolean | `false` | Allow root password login over SSH |
| `password_source` | string | `environment` | `environment`, `generate`, or `prompt`; see [Passwords](#passwords) |
| `generate_missing_passwords` | boolean | `true` | Generate a password when an environment variable is unset or empty |
| `root_password_env` | string | none | Variable holding the root password |
| `user_password_env` | string | none | Variable holding the regular-user password |
| `expected_disk_model` | string | none | When set, plan creation requires the inventory to report exactly this model, and `preflight` requires `lsblk` to report it on the live target. Must be a string or `null` |
| `expected_disk_size_gib` | number | none | When set, plan creation and `preflight` require the disk size to be within 1 GiB of this value. Must be a number greater than 0 (not a boolean) or `null`. `prepare_install.py` derives it from the exact byte count in the `disk_identity` inventory, falling back to the rounded `lsblk` size only when that is missing |
| `schema_version` | integer | none | Configuration schema version; currently `1` |

`prepare_install.py` also writes `detected_live_boot_mode`,
`inventory_source`, and `handbook_snapshot`. They are a record for review; no
code reads them.

Note on `init_system`: the generated `configuration`, `authentication`, and
`validation` commands use OpenRC (`rc-update`, `/etc/runlevels`), and the
stage 3 archive is the OpenRC one, so `openrc` is the only supported value.
`validated_settings()`, `init_config.py`, `prepare_install.py`, and the MCP
`resolve_config` tool all refuse anything else (`CONFIG_INVALID`). systemd is
out of scope for this version.

Note on `architecture`: a private `config/install.json` written before this
field existed has none, and now fails `validate`, `install --dry-run`, and
`plan create`. Add `"architecture": "amd64"` (or `"arm64"`) to it, or write the
file again with `init_config.py --overwrite` or `prepare_install.py`.
`config/install.default.json` does not carry the field, so the architecture is
always an explicit decision; `config/install.example.json` shows `"amd64"`.

## Validation rules

These rules are checked before any command is generated (`validated_settings`
in `gentoo_agent/core/validate.py`). A violation raises `CONFIG_INVALID`, or
`CONFIG_CONFLICT` for UEFI without GPT.

| Field | Rule |
|---|---|
| `disk`, partition `device` | Match `/dev/[A-Za-z0-9._+/-]+` |
| `partition_layout` | Non-empty array |
| target `user` | `root` |
| `boot_mode`, `partition_table` | UEFI requires `gpt` |
| `root_filesystem` | `ext4` |
| `hostname` | Starts with a letter or digit, then letters, digits, `.`, `_`, `-`; at most 63 characters |
| `timezone` | Letters, digits, `.`, `_`, `+`, `/`, `-`; not absolute and no `..` component |
| `init_system` | `openrc` only (see the note above) |
| `architecture` | `amd64` or `arm64`; `arm64` with `boot_mode: bios` is `CONFIG_CONFLICT` |
| `network_method` | `dhcp` or `static`; `static` requires a valid `network_static` |
| `swap_mb` | Integer from 0 to 131072 (booleans are refused) |
| Boolean flags | `create_user`, `set_root_password`, `install_user_ssh_key`, `configure_sudo`, `allow_root_password_ssh`, `generate_missing_passwords` must be JSON booleans |
| `install_user_ssh_key`, `configure_sudo` | Require `create_user: true` |
| `create_user`, `set_root_password` | At least one must be true |
| `username` | Matches `[a-z_][a-z0-9_-]{0,31}` and is not `root` |
| `user_groups` | Each entry matches `[a-z_][a-z0-9_-]{0,31}`; `configure_sudo` requires `wheel` |
| `user_shell` | `/bin/bash` |
| `password_source` | `environment`, `generate`, or `prompt` |
| `root_password_env`, `user_password_env` | Valid password variable names (see [Passwords](#passwords)); checked when `password_source` is `environment` or when the field is present |

`validate` additionally reports missing required fields, requires a
non-negative `swap_mb`, checks the erase authorization, and compares the
configuration with the latest inventory: the configured disk must appear in
it, the configured architecture must match the live system, and a UEFI install
needs a UEFI live boot (a BIOS install conflicts with a UEFI live boot).
Without an inventory it warns and skips those checks.

## Disk erase authorization

The `storage` phase always erases the target disk. `plan create` (and the MCP
`create_plan` tool) refuse with `CONFIG_INVALID` unless all five flags are
`true`:

| File | Field |
|---|---|
| `install.json` | `disk_must_be_erased` |
| `install.json` | `target_is_disposable` |
| `install.json` | `disk_erase_authorized` |
| `target.json` | `target_is_disposable` |
| `target.json` | `disk_erase_authorized` |

The error lists the missing flags. Plan creation has further requirements; see
[Plan creation requirements](#plan-creation-requirements). Before any change,
and again before every destructive phase, a run re-reads the disk path, size,
model, serial, and WWN and stops with `TARGET_DRIFT` if they differ from the
plan.

## Plan creation requirements

`plan create` and the MCP `create_plan` tool refuse to write a plan unless
all of these hold, in this order:

1. The configuration passes `validated_settings()` and the erase authorization
   above is complete (`CONFIG_INVALID`).
2. An inventory exists (`INVENTORY_MISSING`) and is younger than 24 hours. An
   undated or older inventory, or one dated more than 5 minutes in the future,
   is also `INVENTORY_MISSING`: run `inspect` again.
3. `architecture` matches the live system's `uname -m` from the inventory
   (`CONFIG_CONFLICT`).
4. The `validate` review (`validate_installation()`) reports no errors
   (`CONFIG_INVALID`; `details.errors` lists them). This includes a missing
   required field, a configured disk that is not in the inventory, and a UEFI
   install on a BIOS-booted live system.
5. The disk is in the inventory, and a disk without a serial number or WWN has
   no twin of the same size and model (`DISK_AMBIGUOUS`).
6. When set, `expected_disk_model` equals the inventory's model and
   `expected_disk_size_gib` is within 1 GiB of the inventory's size
   (`CONFIG_INVALID`).

## Passwords

Passwords never appear in configuration files, plans, generated commands, or
logs. They are resolved before any phase runs and reach the target only as
`chpasswd` records over the SSH command's standard input.

`password_source` values:

| Value | Behavior |
|---|---|
| `environment` | Reads the variables named by `root_password_env` and `user_password_env`. An unset or empty variable is replaced by a generated password when `generate_missing_passwords` is true, and is an error otherwise |
| `generate` | Generates every password |
| `prompt` | Asks in the terminal; needs an interactive terminal and is refused by the MCP server |

Password variable names:

- Must match `[A-Z][A-Z0-9_]{0,62}` followed by `_PASSWORD` or `_PW`, for
  example `GENTOO_ROOT_PASSWORD` or `ALEX_PW`.
- Are refused when they are a system variable (`PATH`, `HOME`, `USER`,
  `LOGNAME`, `SHELL`, `PWD`, `OLDPWD`, `TERM`, `LANG`, `TMPDIR`, `EDITOR`,
  `GENTOO_AGENT_HOME`), start with `SSH_`, `LD_`, `DYLD_`, `PYTHON`, or `GIT_`,
  or contain `TOKEN`, `SECRET`, `API_KEY`, `PRIVATE`, `AWS_`, `ANTHROPIC`, or
  `OPENAI`.
- A password value must not be empty or contain a newline.
- The password variables are removed from the environment of the background
  worker.

Generated passwords are generated with `secrets.token_urlsafe(24)` and kept in
`runs/<run_id>/credentials.json` (mode 0600) together with whether the
`authentication` phase applied them. `gentoo-agent run status <run_id>` shows
them once after the run has ended and deletes the file. Credentials that are
never shown are deleted 24 hours after they were written, the next time any
run status is read. The MCP server never returns them; `get_run_status` reports
only `credentials_waiting` and `credentials_applied`.

## Installed login access

The live environment and the installed system are separate. These settings
control the installed system:

- The `configuration` phase installs `net-misc/openssh`, writes the target's
  public key to `/root/.ssh/authorized_keys`, writes
  `/etc/ssh/sshd_config.d/gentoo-agent.conf`, and enables `sshd`. It also
  enables `dhcpcd`, for both network methods.
- Root SSH login is `PermitRootLogin prohibit-password` (key only) unless
  `allow_root_password_ssh` is true, which sets `PermitRootLogin yes`. The
  generated file always contains `PasswordAuthentication yes`, so the regular
  user can log in with a password.
- The `authentication` phase creates the regular user with the configured
  groups and shell, sets passwords, installs the public key for the user when
  `install_user_ssh_key` is true, and configures sudo when `configure_sudo` is
  true. `sudo` asks for the regular user's password.
- The tracked `install.default.json` creates user `bajt`; the example file and
  `init_config.py` (without `--username`) create `gentoo`.

## Architecture

`architecture` is required in `install.json` and is `amd64` or `arm64`. The
table behind it is `gentoo_agent/core/arch.py`:

| Architecture | `uname -m` | GRUB EFI target | EFI binary | BIOS |
|---|---|---|---|---|
| `amd64` | `x86_64` | `x86_64-efi` | `grubx64.efi` | supported |
| `arm64` | `aarch64` | `arm64-efi` | `grubaa64.efi` | not supported |

- The architecture selects the stage 3 archive (`releases/<arch>/autobuilds/`,
  OpenRC variant), the `grub-install` target, and the file the `bootloader`
  check looks for.
- It is inferred from the identity probe of the inventory (`uname -m`). The
  MCP `resolve_config` tool infers it, accepts an `architecture` override, and
  refuses a value that differs from the live system (`CONFIG_CONFLICT`); a
  fallback cannot set it. `detect_hardware` reports it, or `unknown` when the
  probe failed or the machine is not supported.
- The `preflight` program also checks `uname -m` on the live target.
- arm64 is UEFI only: `boot_mode: bios` with `arm64` is `CONFIG_CONFLICT`.
  `grub-install` is not run with `--removable` yet, so firmware that only boots
  `/EFI/BOOT/BOOTAA64.EFI` will not find the installed system (see the
  [roadmap](ROADMAP.md#known-limitations)).
- `target.json` `architecture` is informational and is not compared with the
  live system; `prepare_install.py` sets it from the live system, together with
  `handbook_url`.

## Static network

`network_static` is required when `network_method` is `static` and must have
exactly these keys:

| Key | Rule |
|---|---|
| `interface` | Name of the network interface on the installed system: letters, digits, `_`, `.`, `-`, at most 15 characters, first character a letter or digit, for example `enp1s0` |
| `address` | IPv4 address in CIDR form, for example `192.168.10.20/24`, with a prefix from /8 to /30 |
| `gateway` | IPv4 address inside the subnet of `address` |
| `dns` | List of 1 to 3 IP addresses |

Find the interface name in the `network` section of the latest inventory, or
run `ip -brief address` on the live system. The installed system usually names
the interface the same way as the live ISO, but check it on the target console
before you rely on it.

The configuration phase writes the settings to `/etc/dhcpcd.conf`, between the
marker lines `# gentoo-agent static network begin` and
`# gentoo-agent static network end`:

```text
# gentoo-agent static network begin
interface enp1s0
static ip_address=192.168.10.20/24
static routers=192.168.10.1
static domain_name_servers=192.168.10.1
# gentoo-agent static network end
```

A rerun removes a block it wrote earlier and appends a new one, so the file
holds exactly one. The phase check looks for the `interface` and
`static ip_address` lines. `dhcpcd` is enabled for both network methods.

## Partition layout and swap

Each `partition_layout` entry has `device`, `type` (`efi-system`,
`linux-swap`, or `linux-root`), either `size_mib` or `"size": "remaining"`,
and, where it applies, `filesystem` and `mount`.

`init_config.py`, `prepare_install.py`, and `resolve_config` derive the layout
from the disk, the boot mode, and `swap_mb`:

| Boot mode | Table | Partitions in order |
|---|---|---|
| `uefi` | `gpt` | 512 MiB EFI system partition (vfat, `/boot`), swap of `swap_mb` MiB, root ext4 (`/`) on the rest |
| `bios` | `mbr` | swap of `swap_mb` MiB, root ext4 (`/`) on the rest |

- `swap_mb: 0` leaves out the swap partition.
- `resolve_config` also requires swap to be less than half the disk size.
- Partition paths gain a `p` before the number when the disk name ends in a
  digit (`/dev/nvme0n1p1`, but `/dev/sda1`).

## Approval records and the signing key

- `approvals/<plan_id>.json` has exactly the fields `plan_id`, `plan_hash`,
  `channel` (`cli` or `elicitation`), `approved_at`, `expires_at`, and
  `signature`.
- `expires_at` must be exactly 60 minutes after `approved_at`; an
  `approved_at` more than 60 seconds in the future is refused.
- The signature is HMAC-SHA256 with the key in `secrets/approval.key`. A
  missing, malformed, unsigned, or hand-edited record counts as not approved
  (`APPROVAL_REQUIRED`); a record for another plan hash gives
  `PLAN_HASH_MISMATCH`, and an expired one `APPROVAL_EXPIRED`.
- The key is created the first time a human approves a plan (CLI `approve` or
  the MCP dialog), with directory mode 0700 and file mode 0600. Verification
  never creates a key, so approvals signed with a deleted key are invalid.
  The key must be a regular file owned by the current user with no group or
  other permissions.
- Never commit, copy, or let the agent read the key. To revoke all approvals,
  delete the key and approve again.
- `approve` needs an interactive terminal. `approve` and background runs
  (`run start`, `run resume`) need macOS or Linux; on Windows use WSL.

## MCP configuration resolution

The MCP tools `resolve_config` (preview, writes nothing) and `create_plan`
take `overrides` and `fallbacks` and build the install configuration from
these layers, highest priority first:

1. `overrides`: values the user asked for.
2. Profile: supported by `core/resolve.py`, but the MCP tools do not expose a
   profile argument, so it is empty through MCP.
3. Inferred from the latest inventory: the live boot mode, the live
   architecture, and, when exactly one disk is eligible, the disk.
4. `fallbacks`: values used only where nothing above decided.
5. Defaults from `config/install.default.json`, without its disk-specific
   fields.

The result records the source of every value (`override`, `profile`,
`inferred`, `fallback`, `default`, or `derived`) and lists warnings.

Settable keys and types:

| Key | Type |
|---|---|
| `username`, `hostname`, `timezone`, `boot_mode`, `init_system`, `disk`, `network_method`, `bootloader`, `architecture` | string |
| `swap_mb` | integer |
| `create_user`, `configure_sudo`, `set_root_password`, `allow_root_password_ssh`, `install_user_ssh_key` | boolean |
| `user_groups` | list of strings |
| `network_static` | object |
| `user_password`, `root_password` | `{"env": "NAME"}`; overrides and profile only |

Rules:

- Any other key, or a value of the wrong type, raises `CONFIG_INVALID`.
- Fallbacks may not set `disk`, `boot_mode`, `architecture`, or passwords.
- A password reference sets `root_password_env` or `user_password_env`, and
  sets `password_source: "environment"` and `generate_missing_passwords: true`.
- `bootloader` must be `grub` and `init_system` must be `openrc`
  (`CONFIG_INVALID`).
- The boot mode must be known from the inventory (`INVENTORY_MISSING`
  otherwise). A requested boot mode that differs from the live boot mode
  returns `CONFIG_CONFLICT`.
- Inferred disk: exactly one disk that is not removable, not attached over
  USB, not read-only, and has no mounted partition. An inventory without any
  disks returns `INVENTORY_MISSING`; when disks exist but none or several are
  eligible, the result is `DISK_AMBIGUOUS` with the candidates and the
  excluded disks with reasons. An inferred disk adds a warning.
- A chosen disk that is not in the inventory returns `CONFIG_INVALID`; an
  excluded disk returns `CONFIG_CONFLICT` with the reason.
- An uppercase `username` is lowercased, with a warning.
- Derived values: `profile` as `gentoo-<architecture>-<init_system>`,
  `partition_table` and `partition_layout` from the layout rules, `disk_must_be_erased: true`, and `target_is_disposable` and
  `disk_erase_authorized` copied from `target.json`.
- The result then passes the same [validation rules](#validation-rules).

## Phase selection

The phases run in this order: `preflight`, `storage`, `filesystems`,
`base-system`, `configuration`, `authentication`, `kernel`, `bootloader`,
`validation`. `config/plan.json` lists them, plus the read-only `discovery`
phase, with their descriptions and `destructive` flags.

- The CLI (`run start`) runs every phase of the plan.
- The MCP tool `run_phase` takes `plan_id` and a list of phase ids and starts
  a run of only those phases, in plan order. An unknown or empty selection
  raises `CONFIG_INVALID`. The approval gate and the disk identity check still
  apply.
- The selection is stored in the run record as `phase_ids`; `resume_run`
  reuses it.
- Each phase first runs its read-only check; a phase whose check passes is
  skipped. Runs never reboot the target.
- The MCP tool `list_phases` lists only the runnable phases.

## What the configuration must decide

The Handbook can supply command details, package names, and architecture
specific procedure. It cannot supply the target decisions: which disk may be
erased, the filesystem, swap size, hostname, timezone, or network behavior.
Those must be present in `config/install.json` or, through MCP, come from the
user, the inventory, or a reviewed fallback.

The agent may derive harmless details from read-only inspection. It must not
derive destructive choices, such as the disk to erase, from a guessed device
name.
