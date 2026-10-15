# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Read `AGENTS.md` first: its safety rules (no guessing disks or addresses, no arbitrary root commands over SSH, no execution without approval) are binding for every task in this repository, including code changes that alter what the executor sends to a target.

## Commands

Python 3.12+ standard library only for `core/` (`pyproject.toml`; the `mcp` server needs the optional `[mcp]` extra); no linter configuration. `pip install -e .` adds a `gentoo-agent` command; `python -m gentoo_agent` and `python tools/gentoo_agent.py` are equivalent.

```sh
# Tests (unittest, no network or SSH)
python -m unittest discover -s tests -v
python -m unittest tests.test_authentication.AuthenticationConfigurationTests.test_sudo_can_be_disabled_explicitly
GOLDEN_UPDATE=1 python -m unittest tests.test_golden_commands   # only after an intentional command change

# MCP tests need the extra in a project venv (they skip themselves without it)
python3 -m venv .venv && .venv/bin/pip install -e '.[mcp]'
.venv/bin/python -m unittest discover -s tests -v

# Local-only checks (no target connection)
python tools/gentoo_agent.py validate
python tools/gentoo_agent.py status
python tools/gentoo_agent.py install --dry-run            # renders every phase's shell program
python tools/gentoo_agent.py install --dry-run --from-phase kernel

# Config bootstrap (refuses to overwrite without --overwrite)
python tools/init_config.py --host <ip> --disk /dev/sda --allow-disk-erase
```

Commands that touch the target (`inspect`, `handbook`, `prepare_install.py`, `run start`, `run resume`) are operator actions; do not run them unless asked. Never run `approve`: it must be a human in an interactive terminal, and `.claude/settings.json` denies it. Never write or edit files under `approvals/` or `plans/`.

## Architecture

`gentoo_agent/core/` holds all logic and must never print, prompt, or exit (`tests/test_core_purity.py` enforces this); it returns data and raises `AgentError(code, message, details)` from `core/errors.py`. Front ends are `gentoo_agent/cli.py` (argparse subcommands), `gentoo_agent/prepare.py` (interactive beginner flow), and `gentoo_agent/init_config.py`. `tools/*.py` are shims that put the repo root on `sys.path` and call those front ends. `core/paths.py` derives every directory from the repo root, overridable with `GENTOO_AGENT_HOME` (useful for tests and dry runs against a temporary config).

- `core/phases.py`: `PHASES` order (preflight, storage, filesystems, base-system, configuration, authentication, kernel, bootloader, validation) and, per phase, an apply generator (`command_for`) and a read-only check generator (`check_command_for`). Generators take `target` and `install` explicitly and quote every value with `shlex.quote`. `config/plan.json` mirrors the phase list plus the read-only `discovery` phase; keep them in sync. `tests/test_golden_commands.py` pins the generated programs for five configurations in `tests/golden/`.
- `core/validate.py`: `validated_settings()` is the single gate for values that reach remote commands; `validate_installation()` is the broader review used by `validate`.
- `core/install_plan.py`: `create_install_plan()` freezes the validated config, SSH target, disk identity (serial/WWN from the `disk_identity` inventory command, `core/disks.py`), and every generated apply/check command into `plans/<plan_id>.json`. `plan_hash` is SHA-256 over all of it; `plan_id` is its first 16 hex characters. `load_install_plan()` rejects modified files.
- `core/approval.py`: `record_approval()` writes `approvals/<plan_id>.json` (60-minute expiry, bound to `plan_hash`, validated strictly: a malformed record counts as not approved); `require_approved_plan()` is the single execution gate. Only humans approve: the TTY-only `approve` CLI command or MCP elicitation. Records are HMAC-signed (`core/signing.py`); the signature is the barrier only when the agent cannot read `secrets/` (operator profile), otherwise the TTY requirement and deny rules are a convention.
- `jobs/launcher.py`: `start_run()` is the only way to start work. It calls `require_approved_plan()`, resolves passwords before any phase, takes the single-run lock (`core/runs.py`), and spawns `python -m gentoo_agent.jobs.worker <run_id>` detached, passing password records over stdin.
- `core/runs.py`: run records, lock, cancel flag, log, and one-time credentials. All run-record and lock changes go through an `fcntl` mutex (`runs/.mutex`). The worker calls `runs.claim_run()` before executing and refuses an invalid start payload; `execute_plan` refuses unclaimed runs.
- `core/runner.py`: `execute_plan()` (run by the worker) re-checks the live disk identity (`TARGET_DRIFT`), then per phase runs the stored check (exit 0 means done, skip), apply, and check again as verify, each as one `sh -c` program via `core/ssh.remote_shell()`. It honors `runs/<run_id>/cancel` between phases, records every outcome in `runs/<run_id>/run.json` and `log.jsonl`, and never reboots. `render_dry_run()` backs `install --dry-run`.
- `core/paths.py`: functions resolved on every call (`GENTOO_AGENT_HOME` overrides the root); tests use `tests/helpers.temp_home()`.
- `core/credentials.py`: passwords travel only over SSH stdin as `chpasswd` records; they never appear in generated commands, logs, or config. The `prompt` source takes a callback from the front end.
- `core/ssh.py` is the only module that contacts the target: `READ_ONLY_COMMANDS` allowlist for `inspect`, `remote_shell` for generated programs, strict host-key checking.

- `core/signing.py`: HMAC-SHA256 signatures for approval records with the key in `secrets/approval.key` (directory 0700, file 0600, never created by verification). Records without a valid signature count as not approved.
- `core/resolve.py`: `resolve_config()` layers overrides, profile, inferred, fallbacks, defaults and records a source per value. All input is type-checked; passwords are `{"env": NAME}` only (names end in `_PASSWORD` or `_PW`); `boot_mode` and `disk` cannot come from fallbacks; `init_system` must be `openrc` (systemd is out of scope for version 1, although `validated_settings()` still accepts it for the golden fixtures).
- `core/layout.py`: partition layout from disk size, boot mode, and `swap_mb` (0 means no swap; swap must be under half the disk). `core/disks.py`: disk identities and inference (`disk_candidates()` excludes removable, USB, read-only, and mounted disks). `core/hardware.py`: inventory summary for discovery; the boot mode is `unknown` unless the probe succeeded cleanly.
- `core/inventory.py`: `collect_inventory()` raises `SSH_UNREACHABLE` and saves nothing when every allowlisted command failed with ssh exit 255 or a timeout.
- `gentoo_agent/mcp/server.py`: thin `MCPServer` adapter over `core/` and `jobs/launcher`. Every tool returns a `CallToolResult` with `structured_content` (no outputSchema); errors carry `{code, message, details}` with `is_error`. `approve_plan` uses elicitation and the server builds the dialog text (`approval_summary`, untrusted values escaped). Generated passwords never reach the model (`get_run_status` reports only `credentials_waiting` and `credentials_applied`). Never `print()` in server code: stdout is the protocol.
- Operator profile: `docs/operator/claude-settings.json` and `docs/OPERATING.md` stop the agent's shell and file tools from writing `approvals/`, `plans/`, `runs/`, `secrets/`, `config/`, `inventory/`, `.mcp.json`, and `.claude/`; the shell cannot read `secrets/` or `runs/` (run status and logs come through the MCP tools), and the file tools cannot read `secrets/` or run credentials. Deny rules for top-level directories must be root-anchored (`Edit(/plans/**)`), because `Edit(plans/**)` also matches `docs/superpowers/plans/`.

`roles/*.md` hold the Supervisor, Inspector, Executor, Verifier, and Recovery prompts used when coordinating the workflow as an agent. The MCP server design that builds on this package is in `docs/superpowers/specs/2026-10-15-mcp-server-design.md`.

## Configuration files

- Private, gitignored: `config/target.json`, `config/install.json`, `state/state.json`, and everything under `inventory/`, `logs/`, `plans/`, `approvals/`, `runs/`. Do not commit or overwrite them.
- Committed templates: `*.default.json` (safe defaults, execution and erase disabled) and `*.example.json`. Field reference: `docs/CONFIGURATION.md`. When adding a config field, update the defaults, `init_config.py` / `prepare_install.py` if they set it, and `docs/CONFIGURATION.md`.
- `target.json` may point to a different install config via `install_config`.

## Conventions

- Documentation and examples use PowerShell syntax in places (operator works on Windows as well as Unix); CLI commands themselves are platform-neutral.
- The Gentoo Handbook (`wiki.gentoo.org/wiki/Handbook:AMD64`) is the procedure source; record URL, section, and retrieval time when it informs a change.
