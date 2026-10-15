# Gentoo Agent

A supervised, resumable agent workflow for installing or repairing Gentoo over
SSH from a live environment.

The repository provides a supervised installer whose default mode is still
read-only. It can:

- fetch the official Gentoo Handbook;
- inspect a live target using an allowlisted SSH command set;
- record inventory and source evidence;
- keep a phase plan and resumable state;
- create explicit approval records;
- execute the reviewed installation phases with a guarded command allowlist.

The installer is intended for disposable VM testing first. It never reboots a
target automatically, and execution remains disabled until the operator
explicitly approves the rendered configuration.

## Architecture

```text
local agent / supervisor
        |
        +-- Handbook reader (HTTPS)
        +-- SSH inspector (read-only)
        +-- approval and state store
        +-- phase executor (allowlisted, dry-run by default)
        |
        +---- OpenSSH ----> Gentoo live environment
```

The local machine owns the plan, logs, approvals, and private SSH key. The
target only needs a booted live environment, networking, and a temporary SSH
server.

## Requirements

- Python 3.12 or newer on the operator machine.
- Optional: `pip install -e .` provides the `gentoo-agent` command; every
  `python tools/...` command below keeps working without installation.
- OpenSSH client available as `ssh` on `PATH`.
- A Gentoo live environment reachable over SSH.
- A verified host key in the operator's SSH `known_hosts` file.
- A dedicated temporary SSH key for the live session.

## First setup

For a new checkout, create the minimum local configuration with:

```text
python tools/init_config.py --host <live-ip> --disk /dev/sda --allow-disk-erase
```

The command creates `config/target.json` and `config/install.json`, applies
safe defaults, and does not connect to the target. It refuses to overwrite
existing files unless `--overwrite` is explicitly supplied. See
`docs/CONFIGURATION.md` for every required and optional field.

The same defaults are directly visible in
`config/target.default.json` and `config/install.default.json`. Those files
are safe examples: disk erasure and execution are disabled.

Edit the generated files with the live target address, SSH user, key path,
architecture, and installation choices. Keep them private; they are ignored by
Git.

Do not put a private key or password in this repository.

The installation choices are kept separately in `config/install.json`. This
file is ignored by Git because it contains target-specific decisions. Review it
before enabling execution. The active private configuration describes a BIOS,
ext4, OpenRC, 2 GiB-swap installation on `/dev/sda`, with execution disabled.

## Safe first run

For the beginner-friendly workflow, run one command:

```powershell
python tools/prepare_install.py
```

It reads or creates `target.json`, connects over SSH using read-only checks,
collects inventory, asks for installation choices, writes `install.json`, and
prints the entire configuration for review. It never installs anything.

Fetch the current Handbook source:

```sh
python tools/gentoo_agent.py handbook
```

Inspect the target using only the built-in read-only command set:

```sh
python tools/gentoo_agent.py inspect
```

Review the generated files under `inventory/` before doing anything else:

```sh
python tools/gentoo_agent.py status
python tools/gentoo_agent.py validate
```

Render every installation command without connecting to the target:

```powershell
python tools/gentoo_agent.py install --dry-run
```

After reviewing `config/target.json`, `config/install.json`, the latest
inventory, and the dry-run output, record the explicit execution approval:

```powershell
python tools/gentoo_agent.py approve-install
```

Only then can the installer be started. The approval command prints the exact
confirmation phrase required by the executor:

```powershell
python tools/gentoo_agent.py install --execute --confirm "INSTALL gentoo ON /dev/sda"
```

The phases are preflight, storage, filesystems, base system, configuration,
authentication, kernel, bootloader, and validation. A failure stops the workflow and preserves
the local log under `logs/`; the target is left running for console recovery.
Every phase has a read-only state check and a post-apply verification. If
`--from-phase` is omitted, a run starts at the beginning, checks every phase,
and skips work that is already complete.

During the authentication phase the agent installs OpenSSH and sudo, verifies
the root authorized key and enabled services, creates the configured regular user, and
reads password environment variables without prompting. It generates random
replacements when configured values are absent. Generated
credentials are printed once after successful completion but never saved in
configuration files or phase logs. The regular user is the normal post-reboot login. Root SSH is
key-only by default; the reviewed configuration may explicitly allow root
password SSH login while retaining the root public key as a fallback.

When `configure_sudo` is enabled, the regular user must be in `wheel`; the
authentication phase enables the standard `%wheel` sudoers rule. The regular
user's password is the password requested by `sudo -i`.

The commands use strict SSH host-key checking. If the host key is not already
trusted, stop and verify it through the live console before reconnecting.

## Workflow

1. Boot the target into a live environment using a physical, hypervisor, or
   management console.
2. Start SSH and verify the connection manually.
3. Configure `config/target.json`.
4. Run `handbook` and `inspect`.
5. Run `install --dry-run` and review the generated commands.
6. Run `approve-install`, then start `install --execute` with its exact phrase.
7. Verify the phase, update state, and preserve the transcript.

The Handbook provides procedure. The state machine, approval records, and
phase scripts provide repeatability and safety.

## References

- [Gentoo Handbook](https://wiki.gentoo.org/wiki/Handbook:Main_Page/en)
- [Gentoo AMD64 Handbook](https://wiki.gentoo.org/wiki/Handbook:AMD64)
