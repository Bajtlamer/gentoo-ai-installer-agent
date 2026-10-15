# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Read `AGENTS.md` first: its safety rules (no guessing disks or addresses, no arbitrary root commands over SSH, no execution without approval) are binding for every task in this repository, including code changes that alter what the executor sends to a target.

## Commands

Python 3.12+ standard library only (`pyproject.toml`, no runtime dependencies yet); no linter configuration. `pip install -e .` adds a `gentoo-agent` command; `python -m gentoo_agent` and `python tools/gentoo_agent.py` are equivalent.

```sh
# Tests (unittest, no network or SSH)
python -m unittest discover -s tests -v
python -m unittest tests.test_authentication.AuthenticationConfigurationTests.test_sudo_can_be_disabled_explicitly
GOLDEN_UPDATE=1 python -m unittest tests.test_golden_commands   # only after an intentional command change

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

`gentoo_agent/core/` holds all logic and must never print, prompt, or exit (`tests/test_core_purity.py` enforces this); it returns data and raises `AgentError(code, message, details)` from `core/errors.py`. Front ends are `gentoo_agent/cli.py` (argparse subcommands), `gentoo_agent/prepare.py` (interactive beginner flow), and `gentoo_agent/init_config.py`. `tools/*.py` are shims that put the repo root on `sys.path` and call those front ends. `core/paths.py` derives every directory from the repo root, overridable with `GENTOO_AGENT_HOME` (useful for tests and dry runs against a temporary config).

- `core/phases.py`: `PHASES` order (preflight, storage, filesystems, base-system, configuration, authentication, kernel, bootloader, validation) and, per phase, an apply generator (`command_for`) and a read-only check generator (`check_command_for`). Generators take `target` and `install` explicitly and quote every value with `shlex.quote`. `config/plan.json` mirrors the phase list plus the read-only `discovery` phase; keep them in sync. `tests/test_golden_commands.py` pins the generated programs for five configurations in `tests/golden/`.
- `core/validate.py`: `validated_settings()` is the single gate for values that reach remote commands; `validate_installation()` is the broader review used by `validate`.
- `core/runner.py`: `execute()` runs check (exit 0 means done, skip), apply, then the same check as verify, each as one `sh -c` program via `core/ssh.remote_shell()`. Output goes through `on_event`/`on_output` callbacks; progress is saved in `state/state.json` (`core/state.py`) so reruns resume; logs go to `logs/install-<target>-<stamp>.json`. The target is never rebooted. `execution_gate()` requires the exact phrase `INSTALL <target.name> ON <disk>`, `review_status == "approved_for_execution"`, `execution_enabled: true` in `target.json`, `install.json`, and `plan.json`, and both disposable and erase flags when `disk_must_be_erased`. `core/approval.enable_execution()` (behind `approve-install`) is the only code that sets these flags, and it rewrites the committed `config/plan.json`.
- `core/credentials.py`: passwords travel only over SSH stdin as `chpasswd` records; they never appear in generated commands, logs, or config. The `prompt` source takes a callback from the front end.
- `core/ssh.py` is the only module that contacts the target: `READ_ONLY_COMMANDS` allowlist for `inspect`, `remote_shell` for generated programs, strict host-key checking.

`roles/*.md` hold the Supervisor, Inspector, Executor, Verifier, and Recovery prompts used when coordinating the workflow as an agent. The MCP server design that builds on this package is in `docs/superpowers/specs/2026-10-15-mcp-server-design.md`.

## Configuration files

- Private, gitignored: `config/target.json`, `config/install.json`, `state/state.json`, and everything under `inventory/`, `logs/`, `approvals/`. Do not commit or overwrite them.
- Committed templates: `*.default.json` (safe defaults, execution and erase disabled) and `*.example.json`. Field reference: `docs/CONFIGURATION.md`. When adding a config field, update the defaults, `init_config.py` / `prepare_install.py` if they set it, and `docs/CONFIGURATION.md`.
- `target.json` may point to a different install config via `install_config`.

## Conventions

- Documentation and examples use PowerShell syntax in places (operator works on Windows as well as Unix); CLI commands themselves are platform-neutral.
- The Gentoo Handbook (`wiki.gentoo.org/wiki/Handbook:AMD64`) is the procedure source; record URL, section, and retrieval time when it informs a change.
