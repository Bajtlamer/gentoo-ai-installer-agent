# Gentoo Agent

Supervised Gentoo installation over SSH from a live environment. You drive it
from a command line, or let an AI client drive it through an MCP server. In
both cases a human approves the exact plan before anything changes on the
target.

Status: version 0.2.0. The installer covers a complete OpenRC, ext4, GRUB
installation on one disk (BIOS/MBR or UEFI/GPT) on amd64 and arm64 (arm64 is UEFI
only). Dual boot with Windows on another disk is supported (UEFI, Secure Boot
off): GRUB gets a Windows menu entry and the Windows disk is never written.
Fast Boot must be off too (on VMware `efi.quickBoot.enabled = "FALSE"`), or
GRUB cannot see the Windows disk.
It is meant for a disposable
VM first. Test there before you use physical hardware. See
[docs/ROADMAP.md](docs/ROADMAP.md) for what is done and what comes next.

## How it works

```text
operator machine                                  live target
+------------------------------------------+      +----------------------+
| gentoo-agent CLI      or   MCP server    |      | Gentoo live          |
|        |                  (AI client)    |      | environment          |
|        v                                 |      |                      |
| inspect -> plan (plan_hash)              | SSH  |  read-only inventory |
|        -> human approval (signed record) |----->|  phase programs:     |
|        -> background worker              |      |   check -> apply     |
|        -> phases, run log under runs/    |      |   -> verify          |
+------------------------------------------+      +----------------------+
```

The operator machine holds the configuration, plans, approvals, run logs, and
the SSH key. The target only needs a booted live environment, networking, and
an SSH server. Each phase first runs a read-only check: if it passes, the phase
is skipped. Otherwise it applies the change and runs the check again as
verification. The exception is `storage`: a new run always partitions the
disk, even when an old layout matches the plan, so nothing of an old install
survives; only a resumed run may skip it. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains the
internals.

## Choose your path

**Install with the CLI.** You run every step yourself: create the
configuration, inspect the target, review a dry run, create a plan, approve
it in your terminal, and start the run. Read
[docs/guides/cli-install.md](docs/guides/cli-install.md).

**Install with an AI client over MCP.** An AI client such as Claude Code calls
typed tools to detect hardware, resolve the configuration, and create a plan.
You approve the plan in a dialog the server builds, and the AI starts and
follows the run. Read [docs/guides/ai-install.md](docs/guides/ai-install.md).
To make the approval a barrier the agent cannot bypass, follow
[docs/guides/protected-mode.md](docs/guides/protected-mode.md).

**Contribute.** Set up a development environment, run the tests, and learn
the rules for changing generated commands in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Quickstart

Requirements:

- Python 3.12 or newer and the OpenSSH client (`ssh` on `PATH`).
- macOS or Linux for `approve` and for runs. On Windows, use WSL. Dry runs,
  `inspect`, and `plan create` work on any platform.
- A target with an amd64 or arm64 CPU and a Gentoo live environment reachable over SSH, with its host key verified in
  your `known_hosts` file and a dedicated temporary SSH key.

Install the package, or use the `tools/` shims without installing:

```sh
pip install -e .                 # adds the gentoo-agent command
pip install -e '.[mcp]'          # also adds gentoo-agent-mcp
python tools/gentoo_agent.py -h  # same commands, no installation
```

The minimal CLI sequence:

```sh
python tools/init_config.py --host <live-ip> --disk /dev/sda --allow-disk-erase
gentoo-agent inspect
gentoo-agent install --dry-run
gentoo-agent plan create
gentoo-agent approve <plan_id>      # you, in an interactive terminal
gentoo-agent run start <plan_id>
gentoo-agent run status <run_id>
```

Review the generated files, the inventory, and the dry run before you create
the plan. The guides explain each step.

## Safety model

Every command can be rendered as a dry run without connecting to the target.
A plan freezes the validated configuration, the exact generated commands, and
the target disk identity (serial or WWN), and its `plan_hash` covers all of
them. Only a human approves a plan, in a TTY or an MCP dialog, and the
approval is an HMAC-signed record bound to the `plan_hash` and valid for 60
minutes. One gate checks that approval before any run starts, and the worker
re-reads the live disk identity and stops with `TARGET_DRIFT` if it changed.
The installer never reboots the target, and passwords never appear in
commands, logs, or MCP results.

The approval is a real barrier only with the operator profile and the
protected server copy from
[docs/guides/protected-mode.md](docs/guides/protected-mode.md). Without them,
an agent that can edit the repository could bypass it, so the rule is a
convention.

## Documentation map

| Document | What it covers |
|----------|----------------|
| [README.md](README.md) | This entry point |
| [docs/guides/cli-install.md](docs/guides/cli-install.md) | Full installation with the CLI |
| [docs/guides/ai-install.md](docs/guides/ai-install.md) | Installation driven by an AI client over MCP |
| [docs/guides/protected-mode.md](docs/guides/protected-mode.md) | Protected server copy and operator profile |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every configuration field and file |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Error codes, failed runs, and recovery |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Internals: flow, files on disk, safety layers, modules |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Done, next, and known limitations |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, tests, and change rules |
| [AGENTS.md](AGENTS.md) | Binding safety rules for any agent working here |
| [CLAUDE.md](CLAUDE.md) | Guidance for Claude Code in this repository |
| [docs/superpowers/specs/](docs/superpowers/specs/) | Design specification of the MCP server |
| [docs/superpowers/plans/](docs/superpowers/plans/) | Implementation plans 1 to 3 |
| [docs/background/original-brief.md](docs/background/original-brief.md) | Original problem statement and research |

Procedure source: the
[Gentoo AMD64 Handbook](https://wiki.gentoo.org/wiki/Handbook:AMD64).

## License

MIT. See [LICENSE](LICENSE).
