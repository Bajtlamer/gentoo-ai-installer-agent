# Gentoo Agent instructions

This repository defines a supervised agent workflow for installing or repairing
Gentoo over SSH.

Configuration is documented in `docs/CONFIGURATION.md`. For a new checkout,
use `python tools/init_config.py` to create the two private configuration files
instead of inventing their structure.

## Non-negotiable safety rules

- Treat the target as potentially destructive and production-critical.
- Read `config/target.json`, `config/plan.json`, and `state/state.json` before
  proposing or executing a phase.
- Read `config/install.json` and run the local validation command before
  proposing installation changes.
- Use `python tools/gentoo_agent.py inspect` for target discovery.
- Use the online Gentoo Handbook as the procedure source, and record the URL,
  section, and retrieval time in the logs.
- Never guess a disk, partition, filesystem, mount point, boot mode, or target
  address.
- Never run arbitrary root commands over SSH.
- Never partition, format, delete, repair, replace a bootloader, or reboot
  unless the reviewed configuration explicitly authorizes the operation. For
  this disposable VM, disk erasure is authorized by both
  `target_is_disposable: true` and `disk_erase_authorized: true`; execution is
  still separately controlled by `execution_enabled`.
- Do not start execution while `execution_enabled` is `false` or
  `review_status` is not `approved`.
- If evidence is incomplete or contradictory, stop and ask the operator.
- Preserve logs and existing user files. Do not reset or overwrite unrelated
  changes in this repository.

## Agent roles

Use the role prompts in `roles/` for Planner, Inspector, Executor, Verifier,
and Recovery behavior. A single supervisor may coordinate them, but the
Executor must remain a narrow, approval-aware tool.

## Current implementation status

The repository provides the read-only inspector, Handbook fetcher, state files,
approval records, and guarded executors in `tools/install_phases.py`. The
executor is dry-run by default. It may execute only after `approve-install`
enables all matching execution flags and the operator passes the exact
confirmation phrase. Authentication installs and configures sudo for the
configured wheel-group user by default. Test on the disposable VM before using
physical hardware.
