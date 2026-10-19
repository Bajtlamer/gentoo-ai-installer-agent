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
| Provision plans and the NVIDIA driver | A second plan kind, `provision`, for an installed and running system: same hash and approval, bound to the host identity instead of a disk, with Portage tuning and the NVIDIA driver (open kernel modules) as the first profile, and a post-reboot `gpu-online` check. Implemented on branch `provision-plans-impl`; verification on a real server is still open. See [guides/provision.md](guides/provision.md) | [provision plans](superpowers/plans/2026-10-19-provision-plans-nvidia.md) |

## Next

Next parts of the second phase of the dedicated server install, each with its own spec and plan: KDE Plasma, then
vLLM (both build on the `provision` plan kind).

### Plan 4: hardware profile and tailored kernel

- **Hardware profile.** Inventory of CPU, GPU, NICs, loaded modules, and
  hypervisor detection, added to the read-only allowlist and to
  `detect_hardware`.
- **Inferred Portage settings.** `CPU_FLAGS_X86`, `MAKEOPTS`, `VIDEO_CARDS`,
  and `-march=native`, derived from the hardware profile and reported with
  their source like other inferred values. `MAKEOPTS` and
  `EMERGE_DEFAULT_OPTS` are now derived by the provision plan (from the CPU
  thread count and the memory, as a rule of thumb that is not yet confirmed
  against the Handbook). `CPU_FLAGS_X86`, `-march=native`, and `VIDEO_CARDS`
  stay with this plan.
- **Tailored kernel.** `gentoo-sources` configured with `localmodconfig` from
  the live environment's modules, plus the required options (ext4, devtmpfs,
  EFI and VFAT for UEFI, storage and NIC drivers). `gentoo-kernel-bin` stays
  installed as a fallback GRUB entry.

### Product requirement: hardware-driven, purpose-driven profiles

Operator requirement (2026-10-20): the whole flow must work on every server and
computer, with any graphics hardware, and every setting must follow the data of
the FIRST hardware inventory of the target, never an assumption about one
machine.

Status: the framework is done. `hardware_profile.py` derives the profile from the
provision inventory, `profiles.py` holds the registry, and `detect_provision`
reports `hardware` and `profiles` with an applicability reason. `VIDEO_CARDS` is
derived from the GPU vendors by a fixed map. Only `nvidia-driver` is registered.
Remaining: profiles for AMD, Intel, hybrid, virtual, and no GPU; the desktop
purposes (`desktop-plasma` in plan 2b, GNOME and others later); vLLM; and the
`CPU_FLAGS_X86` and `-march=native` derivation.

- **One hardware profile per target, from the first inventory.** CPU threads and
  memory (compile settings), the exact GPU vendor, PCI id and, where known, the
  model, the firmware boot mode, and the architecture are recorded once and read
  by every later plan (install, provision, desktop, LLM stack). A later plan that
  needs a fact the profile lacks collects it with a read-only command, it does
  not guess.
- **Compile settings come from the profile.** `MAKEOPTS`, `EMERGE_DEFAULT_OPTS`
  and later `CPU_FLAGS_X86` and `VIDEO_CARDS` are derived from it and shown in
  the approval dialog with their source. Today only `MAKEOPTS` and
  `EMERGE_DEFAULT_OPTS` are derived (provision plan).
- **The GPU profile follows the detected hardware.** NVIDIA is the first
  profile. AMD, Intel, virtual, hybrid (an integrated GPU next to a discrete
  one), and no GPU each need a profile or an explicit refusal. A card the driver
  does not support must be refused by data, for example by checking that the
  built module's alias list contains the card's PCI id, not by a table that goes
  stale. Today's `nvidia-driver` profile needs exactly one NVIDIA GPU and refuses
  everything else.
- **The purpose is a choice combined with the hardware.** Some operators want a
  plain server, others a whole platform with a desktop (KDE Plasma, GNOME, others)
  and further stacks (vLLM). A purpose picks the package set and the USE
  settings, the hardware picks the drivers and the compile settings, and the
  approval dialog shows the resulting mapping before anything runs.
- **Tests on real inventories.** Each profile is tested with real inventories of
  several kinds of machines (the first one is the 10.0.1.17 server, kept as
  fixtures in `tests/provision_helpers.py`).

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
