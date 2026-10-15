# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Read `AGENTS.md` first: its safety rules (no guessing disks or addresses, no arbitrary root commands over SSH, no execution without approval) are binding for every task in this repository, including code changes that alter what the executor sends to a target.

## Commands

Python 3.10+ standard library only; there is no build step, dependency file, or linter configuration.

```sh
# Tests (unittest, no network or SSH; install_phases is imported directly)
python -m unittest discover -s tests -v
python -m unittest tests.test_authentication.AuthenticationConfigurationTests.test_sudo_can_be_disabled_explicitly

# Local-only checks (no target connection)
python tools/gentoo_agent.py validate
python tools/gentoo_agent.py status
python tools/gentoo_agent.py install --dry-run            # renders every phase's shell program
python tools/gentoo_agent.py install --dry-run --from-phase kernel

# Config bootstrap (refuses to overwrite without --overwrite)
python tools/init_config.py --host <ip> --disk /dev/sda --allow-disk-erase
```

Commands that touch the target (`inspect`, `handbook`, `prepare_install.py`, `install --execute`) and the approval command (`approve-install`) are operator actions. Do not run them unless the operator asks, and never run `install --execute` on your own initiative.

## Architecture

The CLI entry point is `tools/gentoo_agent.py` (argparse subcommands). It owns path constants (`CONFIG`, `STATE_DIR`, `LOG_DIR`, ...), JSON load/atomic save, `ssh_base()` (BatchMode, StrictHostKeyChecking=yes), and the fixed `READ_ONLY_COMMANDS` allowlist used by `inspect`. `install` and `approve-install` lazily import `tools/install_phases.py`, which imports helpers back from `gentoo_agent`.

`tools/install_phases.py` is the guarded executor:

- `PHASES` defines the order: preflight, storage, filesystems, base-system, configuration, authentication, kernel, bootloader, validation. `config/plan.json` mirrors this list (plus the read-only `discovery` phase) with Handbook section mappings; keep the two in sync.
- Each phase has two generators dispatched by `command_for()` and `check_command_for()`: an apply script and a read-only check script. Both are built only from validated config values (quoted with `shlex.quote`), never from free-form input. When adding or changing a phase, update both dispatchers and keep the check idempotent, per the contract in `phases/README.md`.
- `execute()` runs, per phase: check (exit 0 means already done, so skip), apply, then the same check as postcondition verify. Each step is a single `sh -c` program over SSH via `remote_shell()`, streamed and appended to `logs/install-<target>-<stamp>.json`. Progress is tracked in `state/state.json` (`completed_phases`, `status`, `last_error`), which is what makes reruns resumable. The target is never rebooted.
- `validated_settings()` is the single validation gate for install config (disk path regex, root SSH user required, ext4 only, UEFI requires GPT, username/group/sudo rules). New config fields that reach a remote command need validation here.
- Execution gate (`execution_gate()`): exact phrase `INSTALL <target.name> ON <disk>`, `review_status == "approved_for_execution"`, `execution_enabled: true` in all three of `target.json`, `install.json`, and `plan.json`, plus both disposable and erase flags in target and install when `disk_must_be_erased`. `approve-install` is the only code path that sets these flags, and it rewrites the committed `config/plan.json`.
- Passwords for the authentication phase travel only over SSH stdin as `chpasswd` records (`prompt_authentication_input()`); they must never appear in generated commands, logs, or config. Tests assert this.

`tools/prepare_install.py` is the interactive beginner flow (read-only SSH inventory, questions, writes `install.json`). `tools/init_config.py` writes the two private configs from `config/*.default.json` without connecting.

`roles/*.md` hold the Supervisor, Inspector, Executor, Verifier, and Recovery prompts used when coordinating the workflow as an agent.

## Configuration files

- Private, gitignored: `config/target.json`, `config/install.json`, `state/state.json`, and everything under `inventory/`, `logs/`, `approvals/`. Do not commit or overwrite them.
- Committed templates: `*.default.json` (safe defaults, execution and erase disabled) and `*.example.json`. Field reference: `docs/CONFIGURATION.md`. When adding a config field, update the defaults, `init_config.py` / `prepare_install.py` if they set it, and `docs/CONFIGURATION.md`.
- `target.json` may point to a different install config via `install_config`.

## Conventions

- Documentation and examples use PowerShell syntax in places (operator works on Windows as well as Unix); CLI commands themselves are platform-neutral.
- The Gentoo Handbook (`wiki.gentoo.org/wiki/Handbook:AMD64`) is the procedure source; record URL, section, and retrieval time when it informs a change.
