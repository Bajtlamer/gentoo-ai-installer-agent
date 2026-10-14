# Gentoo Agent

A supervised, resumable agent workflow for installing or repairing Gentoo over
SSH from a live environment.

The current repository is deliberately read-only against the target. It can:

- fetch the official Gentoo Handbook;
- inspect a live target using an allowlisted SSH command set;
- record inventory and source evidence;
- keep a phase plan and resumable state;
- create explicit approval records.

Destructive installation and repair phases are not enabled yet. Implement and
test them on a disposable VM before using real hardware.

## Architecture

```text
local agent / supervisor
        |
        +-- Handbook reader (HTTPS)
        +-- SSH inspector (read-only)
        +-- approval and state store
        +-- phase executor (future, allowlisted)
        |
        +---- OpenSSH ----> Gentoo live environment
```

The local machine owns the plan, logs, approvals, and private SSH key. The
target only needs a booted live environment, networking, and a temporary SSH
server.

## Requirements

- Python 3.10 or newer on the operator machine.
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
before enabling execution. It currently describes a UEFI, ext4, OpenRC,
2 GiB-swap installation on `/dev/sda`, with execution disabled.

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

The commands use strict SSH host-key checking. If the host key is not already
trusted, stop and verify it through the live console before reconnecting.

## Workflow

1. Boot the target into a live environment using a physical, hypervisor, or
   management console.
2. Start SSH and verify the connection manually.
3. Configure `config/target.json`.
4. Run `handbook` and `inspect`.
5. Have the Planner agent create or update `plan.json` from the evidence.
6. Review the proposed phase and create an approval only for the exact target,
   phase, and disk.
7. Add and test one phase executor at a time.
8. Verify the phase, update state, and preserve the transcript.

The Handbook provides procedure. The state machine, approval records, and
phase scripts provide repeatability and safety.

## References

- [Gentoo Handbook](https://wiki.gentoo.org/wiki/Handbook:Main_Page/en)
- [Gentoo AMD64 Handbook](https://wiki.gentoo.org/wiki/Handbook:AMD64)
