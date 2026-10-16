# Roadmap

This page lists what is done, what comes next, and what needs a decision
first. The design behind it is in
[the MCP server design spec](superpowers/specs/2026-10-15-mcp-server-design.md).
The internals are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Done

| Plan | Result | Plan file |
|------|--------|-----------|
| 1. Core package | Code moved into `gentoo_agent/` with a pure `core/`, typed error codes, and golden snapshots of the generated commands | [core package refactor](superpowers/plans/2026-10-15-core-package-refactor.md) |
| 2. Approved plans and worker | Plans bound by `plan_hash` and disk identity, human-only approval records, a detached background worker, and the `run` commands | [approved plans and worker](superpowers/plans/2026-10-15-approved-plans-and-worker.md) |
| 3. MCP server and approval barrier | stdio MCP server with elicitation approval, HMAC-signed approval records, layered config resolution, and the operator profile | [MCP server](superpowers/plans/2026-10-15-mcp-server.md) |
| Hardening | `set -eu` in every apply program, OpenRC only (systemd refused everywhere), static networking in `/etc/dhcpcd.conf` for a named interface, the erase authorization saved in `target.json` by `prepare_install.py`, plan-time review of the configuration, inventory age, and expected disk, a drift check before every destructive phase, a per-run worker flock with worker reaping, a stdio smoke test of the MCP server, `HOST_KEY_UNKNOWN`, and amd64 and arm64 from one architecture table | [hardening plan](superpowers/plans/2026-10-16-hardening.md) |

## Next

### Plan 4: hardware profile and tailored kernel

- **Hardware profile.** Inventory of CPU, GPU, NICs, loaded modules, and
  hypervisor detection, added to the read-only allowlist and to
  `detect_hardware`.
- **Inferred Portage settings.** `CPU_FLAGS_X86`, `MAKEOPTS`, `VIDEO_CARDS`,
  and `-march=native`, derived from the hardware profile and reported with
  their source like other inferred values.
- **Tailored kernel.** `gentoo-sources` configured with `localmodconfig` from
  the live environment's modules, plus the required options (ext4, devtmpfs,
  EFI and VFAT for UEFI, storage and NIC drivers). `gentoo-kernel-bin` stays
  installed as a fallback GRUB entry.

## Later, needs a decision

These are out of scope for version 1. Each needs a design decision first.

- systemd as the init system (refused by validation in version 1).
- RAID.
- LUKS disk encryption.
- Filesystems other than ext4.
- Repair mode for an existing installation.
- HTTP transport for the MCP server.

## Known limitations

- Only OpenRC, ext4, GRUB, and one disk are supported, on amd64 and arm64.
  `validated_settings()`, the resolver, and the front ends refuse
  `init_system` values other than `openrc`.
- arm64 is UEFI only and untested on real hardware.
- Cancel cannot interrupt a phase that is running, or the drift read; it takes
  effect between phases.
- `PHASE_PRECONDITION_FAILED` is reserved and never raised.
- Argument errors raised by the MCP SDK itself (a missing or mistyped tool
  argument) are unstructured text, not `{code, message, details}`.
- The installer never reboots the target. You reboot it yourself after a run
  ends with `installed_pending_reboot`.
- There is no automatic rollback. A failed run leaves the target running, and
  `run resume` re-runs the plan and skips phases whose check passes.
- The kernel is the distribution kernel `gentoo-kernel-bin` until plan 4.
- `detect_hardware` reports only the boot mode, architecture, and disks. CPU, GPU, and NICs
  come with plan 4.
- One run at a time per working directory, and one plan per run.
- Background runs and `approve` need macOS or Linux. On Windows, use WSL.
- The approval is a real barrier only with the operator profile and a
  protected server copy. Without them it is a convention. See
  [guides/protected-mode.md](guides/protected-mode.md).
- `localmodconfig` in plan 4 will only see modules the live environment
  loaded. The fallback kernel covers the rest.
