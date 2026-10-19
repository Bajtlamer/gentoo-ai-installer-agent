# Provision Plans and the NVIDIA Driver: Design

Date: 2026-10-19
Status: approved in conversation, awaiting written review

This is part 1 of the second phase of the dedicated server install (a working
Gentoo gets a desktop and an LLM stack). Parts 2 (KDE Plasma) and 3 (vLLM) get
their own specs and plans and build on the plan kind introduced here.

## Goal

Add a second kind of approved plan, `provision`, that changes a Gentoo system
that is already installed and running, through the same safety model as the
install plan (frozen commands, plan hash, human approval, worker, logs). Use it
for the first concrete provision: the NVIDIA driver for an RTX 5070 (Blackwell),
so that the GPU works for local use and, later, for vLLM.

## Operator decisions

- The second phase is built in parts: this part first (foundation and driver),
  then Plasma, then vLLM. Each part has its own spec, plan, approval, and test
  on the real server.
- CUDA is needed only to run vLLM. The system CUDA toolkit is therefore not part
  of this spec. Whether vLLM needs it (PyTorch wheels normally carry their own
  CUDA runtime) is decided in the vLLM part.
- Approach: a new plan kind in the existing framework, not extra phases at the
  end of the install plan (the system is already installed and booted) and not a
  generic "run this script" feature (it would break the rule that the agent never
  runs arbitrary root commands over SSH).
- Commands run as `root` over SSH with the installed key, directly on the
  running system, without a chroot.
- The installer never reboots. After the driver is installed the plan ends in
  `installed_pending_reboot`, and the operator reboots.

## Non-goals

- The system CUDA toolkit, KDE Plasma, vLLM, Python environments (parts 2 and 3).
- A tailored kernel (roadmap plan 4). The distribution kernel
  `gentoo-kernel-bin` stays; the driver module is built against its sources.
- Automatic rollback. Every change is a small file named `gentoo-agent-*` plus
  one `emerge`, which makes manual removal simple.
- Other GPU vendors and several GPUs. NVIDIA only, detected and reported.
- Fixing the framebuffer console distortion by kernel configuration (see Risks).

## Architecture

### The plan kind

`plans/<plan_id>.json` gets `"kind": "provision"`. Install plans are `"install"`
(missing `kind` in an older file means `install`). The plan hash, plan id,
verification on load, approval records (60 minutes, HMAC signed, bound to the
hash), launcher, worker, run records, and logs are unchanged.

| Field | Install plan | Provision plan |
|---|---|---|
| `kind` | `install` | `provision` |
| `disks` | disk identities, one erased | not present |
| `identity` | not present | host identity (below) |
| `install` | validated install config | not present |
| `provision` | not present | validated provision settings and the derived facts (GPU, licenses, packages) |
| `erases_existing_os` | possible | never |
| `phases` | frozen apply and check programs | frozen apply and check programs |

### Host identity

Read by a new read-only command at plan creation and compared before the run and
before every phase that changes the system (`TARGET_DRIFT` on a mismatch, as for
disks): the SSH host key fingerprint (ed25519), `/etc/machine-id`, and the
hostname. A plan therefore cannot run on another server, and a reinstalled
server (new host key and machine id) invalidates it.

### Modules

- `core/provision_plan.py`: creates and validates provision plans. It reuses
  `compute_plan_hash`, `save_install_plan`, and `load_install_plan` (generalized
  by `kind`), checks inventory freshness (24 hours), and fails with
  `INVENTORY_MISSING` when the identity or GPU result is missing or unusable.
- `core/provision_phases.py`: the apply and check generators of the provision
  phases. Like `phases.py`, generators take `target` and `provision` explicitly
  and quote every value with `shlex.quote`.
- `core/identity.py`: parse and compare the host identity, and
  `check_identity(target, expected)`, the counterpart of `disks.check_drift`.
- `core/runner.py`: dispatches the guard by kind (`check_drift` for install,
  `check_identity` for provision). The storage rule (always erased) is untouched
  and applies to install plans only.
- `core/approval.py`: the summary is built per kind. A provision summary lists
  the phases, the packages, and every license that the plan accepts, and states
  that no disk is changed. The ERASE WINDOWS rule applies to install plans only.
- `jobs/launcher.py`: the password handling is guarded by kind (provision plans
  carry no passwords).
- `mcp/server.py`: new tool `create_provision_plan(overrides)`. Approve, start,
  resume, status, log, and `verify_install` serve both kinds. Tool descriptions
  that say the disk is erased name the install plan only. CLI: `provision plan`
  and the existing `approve` and `run` commands.
- `core/ssh.py`: three new entries in `READ_ONLY_COMMANDS`: `host_identity`,
  `gpu` (`lspci -nnk`, no filtering on the target), and `provision_state`
  (kernel version, loaded modules, installed versions of the packages the plan
  touches). `detect_hardware` also reports the GPU and the driver state.

Each unit can be tested alone: identity parsing and comparison are pure,
generators return strings, and the runner dispatch is covered with the fake
shell that `test_execute_plan.py` already uses.

## Phases of the NVIDIA provision

All phases are idempotent (`emerge --noreplace`, rewritten files), so `run
resume` works without the storage-style problems of the install plan.

1. `preflight` (read-only): root, default route, free space on `/`, the host
   identity, the running kernel is the distribution kernel, and its sources are
   present.
2. `portage-sync`: refresh the Portage tree so the build does not use a stale one.
3. `nvidia-config`: write these files, each named `gentoo-agent-*`:
   `package.license` (the licenses reported by Portage for the exact package set,
   shown in the plan summary), `package.use` for `x11-drivers/nvidia-drivers`
   (`kernel-open`, `dist-kernel`), and a GRUB drop-in that adds
   `nvidia-drm.modeset=1`. If `/usr/src/linux` does not point at the running
   kernel's sources, run `eselect kernel set` for them.
4. `nvidia-drivers`: `emerge x11-drivers/nvidia-drivers`, then regenerate the GRUB
   configuration.
5. `verify` (read-only): before the reboot, the package is installed, the module
   file exists for the running kernel, the open flag is set, and nouveau is
   blacklisted (by the ebuild's own blacklist; the plan writes no blacklist of its
   own, and if this check fails the run fails and a new plan adds one). After the
   reboot, the same check also requires that
   the `nvidia` module is loaded, `nvidia-smi` lists the RTX 5070, and
   `/sys/module/nvidia_drm/parameters/modeset` is `Y`. `verify_install` runs it.

Licenses and USE flags are chosen at plan creation from read-only Portage output
(`emerge --pretend` on the live system), not hard-coded, so the approval dialog
shows what Portage will really require.

## Error handling

- Reuse the existing codes: `TARGET_DRIFT` (identity), `HOST_KEY_UNKNOWN`
  (SSH), `INVENTORY_MISSING`, `PHASE_FAILED`, `APPROVAL_*`.
- Plan creation refuses (`CONFIG_INVALID`) when no NVIDIA GPU is found, when the
  free space is below the threshold, or when Portage reports a blocker or an
  unknown license for the package set.
- A failed `emerge` or module build stops the run with the output tail in the
  record. Nothing is rolled back; the files written so far are idempotent.

## Safety

- No provision command touches a disk, a partition table, or a filesystem. A test
  scans every generated provision program and fails on `wipefs`, `sfdisk`,
  `sgdisk`, `parted`, `mkfs`, `mkswap`, `blkdiscard`, `dd`, `umount`, and
  `swapoff`.
- The human sees the licenses and packages in the approval dialog and in the plan
  summary before approving. Nothing is accepted implicitly.
- The identity guard keeps a plan on the server it was made for.
- The agent still runs no arbitrary command: only the frozen programs of an
  approved plan, and the read-only allowlist.

## Testing

- Golden files for the provision phase programs (one configuration).
- Identity: parse, compare, and `check_identity` against stub output; hostile
  values are refused like the disk values.
- Runner: dispatch by kind, drift stops a provision run before a phase, resume
  skips completed phases.
- Approval and MCP: provision summary text, no erase phrase, `create_provision_plan`
  errors, tool descriptions.
- The safety scan above, and a test that install plans are unchanged (the install
  golden files stay as they are).
- A real test on the dedicated server after the SSH host key is replaced, with a
  reboot and the post-reboot verify.

## Verified facts and their sources

Source: Gentoo Wiki, NVIDIA/nvidia-drivers, https://wiki.gentoo.org/wiki/NVIDIA/nvidia-drivers,
sections on USE flags, kernel configuration, and distribution kernels. Retrieved
2026-10-19T20:08Z.

- `kernel-open` selects the open kernel modules. They are required for Blackwell
  (RTX 50 series) and newer, and since driver version 595 building the open module
  is mandatory.
- `dist-kernel` rebuilds the driver on every distribution kernel upgrade;
  `modules` builds the kernel modules; `persistenced` is off by default.
- With `gentoo-kernel-bin` the page asks for `dist-kernel` in
  `/etc/portage/package.use`, and the ebuild finds the kernel version through the
  `/usr/src/linux` symlink (`eselect kernel` changes it).
- The ebuild blacklists nouveau itself. `nvidia_drm.modeset=1` may be needed if
  `/sys/module/nvidia_drm/parameters/modeset` reports `N`.
- The page covers neither the exact licenses, nor the `nvidia_drm` fbdev
  parameter, nor the generations between Turing and Blackwell beyond the
  statements above.

## To verify during implementation, not assumed

- The exact licenses and the stable or testing keyword of a driver version that
  supports the RTX 5070 (`emerge --pretend` on the real server).
- That `eselect kernel` is already set after the `gentoo-kernel-bin` install, and
  that the module builds against `/usr/src/linux-<version>-gentoo-dist-bin`.
- That the installed GRUB reads drop-ins from `/etc/default/grub.d/`; otherwise
  the parameter goes into a block of `/etc/default/grub` that the plan owns.
- Whether `nvidia-drm.modeset=1` is already the default for the chosen version.

## Risks

- The framebuffer console was distorted on the first server with nouveau. The wiki
  notes distorted lines at boot with the simple framebuffer drivers and suggests
  keeping only `CONFIG_FB_EFI`; that needs a custom kernel and is not in scope. The
  expected cure is `nvidia-drm` taking over the console after the driver is
  installed; if it stays distorted, the next step is the tailored kernel (plan 4).
- The compile of the open module for each kernel update, and a new driver version
  that drops support for an older card, are handled by Portage and `dist-kernel`,
  not by this tool.
- Without a reboot the new driver is not active. The installer reports
  `installed_pending_reboot` and the post-reboot `verify_install` is what proves it.
