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

Copy the example target configuration:

```text
config/target.example.json -> config/target.json
```

Edit `config/target.json` with the live target address, SSH user, key path, and
architecture. Keep this file private; it is ignored by Git.

Do not put a private key or password in this repository.

## Safe first run

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
