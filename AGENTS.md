# Gentoo Agent instructions

This repository defines a supervised agent workflow for installing or repairing
Gentoo over SSH.

Configuration is documented in `docs/CONFIGURATION.md`. For a new checkout,
use `python tools/init_config.py` to create the two private configuration files
instead of inventing their structure.

## Non-negotiable safety rules

- Treat the target as potentially destructive and production-critical.
- Read `config/target.json`, `config/plan.json`, and `state/state.json` before
  proposing or executing a phase. Run progress lives in
  `runs/<run_id>/run.json`; read it with `run status <run_id>`.
- Read `config/install.json` and run the local validation command before
  proposing installation changes.
- Use `python tools/gentoo_agent.py inspect` for target discovery.
- For an installed, running system use `python tools/gentoo_agent.py provision inspect` (a separate read-only
  allowlist), then `provision plan`. It changes nothing on the target.
- Use the online Gentoo Handbook as the procedure source, and record the URL,
  section, and retrieval time in the logs.
- Never guess a disk, partition, filesystem, mount point, boot mode, or target
  address.
- Never run arbitrary root commands over SSH.
- Never partition, format, delete, repair, replace a bootloader, or reboot
  unless the reviewed configuration explicitly authorizes the operation. Disk
  erasure requires `disk_must_be_erased: true` in `config/install.json`, and
  `target_is_disposable` and `disk_erase_authorized` set to `true` in both
  `config/target.json` and `config/install.json`; `plan create` refuses
  otherwise.
- Execution happens only through an approved plan: `plan create`, then a human
  runs `approve <plan_id>` in their own terminal, then `run start <plan_id>`.
  Never run `approve` yourself and never ask to have a deny rule removed.
  Never write, edit, or delete files under `approvals/` or `plans/`, by any
  tool. An approval is bound to the plan hash, expires after 60 minutes, and is
  void when the plan changes; a run also stops with `TARGET_DRIFT` if the disk
  identity no longer matches. This holds for provision plans too: `provision plan`,
  human `approve`, then `run start`; the run stops with `TARGET_DRIFT` if the
  host identity no longer matches. Never guess license names for a provision
  plan: they come from the human.
- When the `gentoo-agent` MCP server is available, use its tools instead of
  the CLI. Never pass a password as text; use `{"env": "NAME"}` references
  only. Never try to approve except through `approve_plan`. If `approve_plan`
  returns `APPROVAL_REQUIRED`, ask the human to run the given command in their
  own terminal. Never read `secrets/` or run credentials.
- If evidence is incomplete or contradictory, stop and ask the operator.
- Preserve logs and existing user files. Do not reset or overwrite unrelated
  changes in this repository.

## Agent roles

Use the role prompts in `roles/` for Planner, Inspector, Executor, Verifier,
and Recovery behavior. A single supervisor may coordinate them, but the
Executor must remain a narrow, approval-aware tool.

## Current implementation status

The repository provides the read-only inspector, Handbook fetcher, plans bound
by `plan_hash`, human approval records, and a background worker. Code lives in
`gentoo_agent/core/` (phase generators in `phases.py`, plans in
`install_plan.py`, approvals in `approval.py`, runs in `runs.py`, the executor
in `runner.py`) and `gentoo_agent/jobs/` (launcher and worker). `install
--dry-run` prints commands without connecting. A run starts only from an
approved plan, re-checks the disk serial before any change, and never reboots
the target. Authentication installs and configures sudo for the configured
wheel-group user by default. Test on the disposable VM before using physical
hardware.
