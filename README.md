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
- Dry run, `inspect`, and `plan create` work on any platform. `approve` and background runs (`run start`, `run resume`) need macOS or Linux; on Windows use WSL.

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
inventory, and the dry-run output, freeze them into a plan:

```sh
python tools/gentoo_agent.py plan create
```

The plan records the generated commands and the target disk serial, and its
`plan_hash` covers both. Approve it yourself in an interactive terminal; the
command refuses to run without one. The agent is instructed never to approve
and is denied the command. With the operator profile and protected mode from
docs/OPERATING.md the agent cannot create a valid approval; without them, the
check is a convention.

```sh
python tools/gentoo_agent.py approve <plan_id>
```

The approval is valid for 60 minutes. Start the run, which continues in the
background, and follow it:

```sh
python tools/gentoo_agent.py run start <plan_id>
python tools/gentoo_agent.py run status <run_id>
python tools/gentoo_agent.py run log <run_id>
```

The phases are preflight, storage, filesystems, base system, configuration,
authentication, kernel, bootloader, and validation. A failure stops the run and preserves its log under `runs/<run_id>/`; the target is left running for console recovery.
Every phase has a read-only state check and a post-apply verification. After a failure, `run resume <run_id>` starts a new run of the same plan and skips phases whose target state is already complete.

During the authentication phase the agent installs OpenSSH and sudo, verifies
the root authorized key and enabled services, creates the configured regular user, and
reads password environment variables without prompting. It generates random
replacements when configured values are absent. Generated
credentials are kept in `runs/<run_id>/credentials.json` with mode 0600 until
`run status` shows them once (if the authentication phase was attempted but the
install did not finish, or the worker stopped early, it says they may have been
applied), are deleted
when shown, and are otherwise purged 24 hours after being written the next time
any `run status` runs. They are never stored in configuration files or phase
logs. The regular user is the normal post-reboot login. Root SSH is
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
6. Run `plan create`, approve the plan yourself with `approve <plan_id>`, then `run start <plan_id>`.
7. Verify the phase, update state, and preserve the transcript.

The Handbook provides procedure. The state machine, approval records, and
phase scripts provide repeatability and safety.

## AI-driven installation (MCP)

An AI client can drive the whole installation through a stdio MCP server.
Install the optional extra and register the server:

```sh
pip install -e '.[mcp]'
```

Point your client at `gentoo-agent-mcp` using `.mcp.json.example`. For a setup
where the agent cannot approve its own plan, follow `docs/OPERATING.md`
(protected copy of the server plus the sandbox profile in
`docs/operator/claude-settings.json`).

The tools are used in this order:

1. `detect_hardware` summarizes the target's firmware boot mode (`uefi`,
   `bios`, or `unknown`) and its disks with serial and WWN; CPU, GPU, and
   network cards come in a later version. It uses the latest saved inventory
   unless `refresh=true`, which first runs the read-only SSH inventory and
   returns `SSH_UNREACHABLE` if the target cannot be reached.
2. `resolve_config` merges the request into a validated install configuration
   and reports the source of every value (overrides, profile, inferred from
   hardware, fallbacks, defaults). It returns `DISK_AMBIGUOUS` or
   `CONFIG_CONFLICT` instead of guessing.
3. `create_plan` freezes the configuration into a plan with a `plan_hash`.
4. `approve_plan` shows the human an approval dialog (elicitation). Only an
   explicit accept writes a signed approval record. If the client cannot show
   the dialog, the tool returns `APPROVAL_REQUIRED` with the command to run in
   your own terminal.
5. `start_install` starts the background run; `get_run_status` and
   `get_run_log` follow it, and `verify_install` runs read-only checks after
   it.

Passwords are passed only as `{"env": "NAME"}`, where the variable name ends
in `_PASSWORD` or `_PW`. Plain-text passwords are rejected. Generated
passwords are never returned to the model; the status reports that credentials
are waiting, and you read them in a terminal:

```sh
gentoo-agent run status <run_id>
```

## References

- [Gentoo Handbook](https://wiki.gentoo.org/wiki/Handbook:Main_Page/en)
- [Gentoo AMD64 Handbook](https://wiki.gentoo.org/wiki/Handbook:AMD64)
