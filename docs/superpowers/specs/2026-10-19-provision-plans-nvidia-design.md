# Provision Plans and the NVIDIA Driver: Design

Date: 2026-10-19
Status: approved in conversation, awaiting written review
Status: implemented on branch provision-plans-impl, verified on a real server: not yet

This spec was updated to the implemented design after review (see the plan
`docs/superpowers/plans/2026-10-19-provision-plans-nvidia.md`, decisions and Amendment 1).
The user guide is `docs/guides/provision.md`.

This is part 1 of the second phase of the dedicated server install (a working
Gentoo gets a desktop and an LLM stack). Parts 2 (KDE Plasma) and 3 (vLLM) get
their own specs and plans and build on the plan kind introduced here.

## Goal

Add a second kind of approved plan, `provision`, that changes a Gentoo system
that is already installed and running, through the same safety model as the
install plan (frozen commands, plan hash, human approval, worker, logs). Use it
for the first concrete provision: Portage tuning for all CPU threads and the NVIDIA driver for an RTX 5070 (Blackwell),
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
| `provision` | not present | validated settings (`profile`, `accept_licenses`) and the derived facts (`gpu`, `cpu`, `memory_gib`, `tuning`) |
| `erases_existing_os` | possible | never |
| `phases` | frozen apply and check programs | frozen apply and check programs, each with `destructive` and `run` |

A phase with `run: false` is skipped by the worker and checked only by `verify_install`.

### Host identity

Read by a new read-only command at plan creation and compared before the run and
before every phase that changes the system (`TARGET_DRIFT` on a mismatch, as for
disks): the SSH host key fingerprint (ed25519), the UUID of the root filesystem (`/etc/machine-id` does not exist on OpenRC), and the
hostname. A plan therefore cannot run on another server, and a reinstalled
server (new host key and root filesystem UUID) invalidates it.

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
- `mcp/server.py`: new tools `detect_provision(refresh)` and
  `create_provision_plan(accept_licenses, profile)`. Approve, start,
  resume, status, log, and `verify_install` serve both kinds. Tool descriptions
  that say the disk is erased name the install plan only. CLI: `provision plan`
  and the existing `approve` and `run` commands.
- `core/provision.py`: validation of the provision settings and the Portage tuning rule.
- `core/provision_facts.py`: strict parsers of the provision inventory.
- `core/ssh.py`: a separate allowlist, `PROVISION_READ_ONLY_COMMANDS`, with
  `host_identity`, `cpu`, `memory`, `gpu`, `portage_conf`, and `kernel`
  (below). The MCP tool `detect_provision` and the CLI `provision inspect`
  collect and summarize it. There is no `provision_state` command.

### The provision inventory

Read-only commands, saved as `inventory/provision-inventory-*.json` (24 hour
freshness, `INVENTORY_MISSING` when a result is missing or unusable). Every value
is parsed strictly before it can reach the plan or the approval dialog.

- `host_identity`: host name, the UUID of the root filesystem, the ed25519 host key fingerprint.
- `cpu`: `nproc --all` and `lscpu` (thread count and model).
- `memory`: `MemTotal` and `SwapTotal` from `/proc/meminfo`.
- `gpu`: one line per PCI device from `/sys/bus/pci/devices`: address, class, vendor,
  device, hex as sysfs prints it (`0000:01:00.0 0x030000 0x10de 0x2f04`). `lspci`
  comes from `sys-apps/pciutils`, which a stage3 does not include, so it is not used.
  A display device is a class whose first byte is `03`. The GPU name
  (`NVIDIA display controller`) is built in code from the vendor id; no text from the
  target is shown as the name. The summary shows it as
  `NVIDIA display controller [10de:xxxx]`.
- `portage_conf`: the user's own `MAKEOPTS` and `EMERGE_DEFAULT_OPTS` outside the plan's
  block, so the plan can warn that its value replaces them.
- `kernel`: `uname -r`.

Each unit can be tested alone: identity parsing and comparison are pure,
generators return strings, and the runner dispatch is covered with the fake
shell that `test_execute_plan.py` already uses.

## Phases of the NVIDIA provision

All phases are idempotent (`emerge --noreplace`, rewritten files, replaced blocks), so
`run resume` works without the storage-style problems of the install plan. The
plan has seven phases, in this order:

1. `preflight` (read-only): refuses unless root, a default route, `emerge`, `/boot` is a
   mounted vfat filesystem, 8 GiB free on `/`, `/usr/src/linux-$(uname -r)` exists, and the
   running kernel matches `*-gentoo-dist*`. Each failing test prints why.
2. `portage-tuning`: writes `MAKEOPTS` and `EMERGE_DEFAULT_OPTS` into a block that the plan
   owns in `/etc/portage/make.conf` (markers `# gentoo-agent portage-tuning begin` and
   `end`). The installer never wrote a compile setting, so the installed system used the
   stage3 defaults. The values are a rule of thumb: jobs are the smaller of the CPU thread
   count and the RAM in whole GiB divided by 2 (at least 1); parallel emerge jobs are
   `threads // 8`, from 1 to 4; the load average is the thread count. The Handbook wording
   of this rule is not confirmed yet, so it is not cited.
3. `portage-sync`: `emerge-webrsync`, so the build does not use a stale tree. Counts as done
   when `timestamp.chk` is under 2 days old.
4. `nvidia-config`: writes `/etc/portage/package.license/gentoo-agent-nvidia`
   (`x11-drivers/nvidia-drivers <licenses>`, the explicit `accept_licenses` list, limited to
   that package, not `*/*`), `/etc/portage/package.use/gentoo-agent-nvidia`
   (`x11-drivers/nvidia-drivers kernel-open dist-kernel`), and a block in
   `/etc/default/grub` (markers `# gentoo-agent nvidia begin` and `end`) that appends
   `nvidia-drm.modeset=1` to `GRUB_CMDLINE_LINUX`. It runs
   `eselect kernel set linux-<running release>`.
5. `nvidia-drivers`: `emerge --noreplace --verbose x11-drivers/nvidia-drivers`, then
   `grub-mkconfig -o /boot/grub/grub.cfg`.
6. `nvidia-verify` (read-only, before the reboot): the module file exists for the running
   kernel and `nouveau` is blacklisted (by the ebuild's own blacklist; the plan writes none
   of its own, and if this check fails the run fails and a new plan adds one).
7. `gpu-online` (`run: false`, checked only by `verify_install`, after the reboot): the
   `nvidia` module is loaded, `nvidia-smi -L` lists an NVIDIA device, the module license
   contains GPL (the open modules), and `/sys/module/nvidia_drm/parameters/modeset` is `Y`.
   Because the worker skips it, the run ends in `installed_pending_reboot`.

The blocks in `make.conf` and `/etc/default/grub` are edited by a portable awk program (no
`sed -i`). It refuses (exit 1, file unchanged) when the begin and end markers do not match,
when there is more than one block, or when the end marker comes before the begin marker. It
adds a missing final newline first and uses a temporary file that a trap removes. The GRUB
parameter goes into a block of `/etc/default/grub`, not a drop-in, because the installer's
GRUB does not create `/etc/default/grub.d/`.

Licenses are an explicit setting, not parsed from `emerge` output. The operator learns the
names from `emerge --pretend --autounmask-license=y x11-drivers/nvidia-drivers` on the
target and supplies them; the agent never guesses them. The human sees them in the approval
dialog, and an incomplete list makes `emerge` fail loudly (the fix is a new plan with a
longer list). This avoids a fragile parser of Portage's text.

## Error handling

- Reuse the existing codes: `TARGET_DRIFT` (identity), `HOST_KEY_UNKNOWN`
  (SSH), `INVENTORY_MISSING`, `PHASE_FAILED`, `APPROVAL_*`.
- Plan creation refuses (`CONFIG_INVALID`) when the SSH user is not `root`, when the settings
  are invalid (unknown profile, `accept_licenses` not 1 to 8 distinct names), or when the
  inventory does not show exactly one NVIDIA GPU. Free space, the kernel, and `/boot` are
  checked by the `preflight` phase at run time, not at plan creation. A missing license, a
  blocker, or a keyword problem shows up as a failed `emerge` in the run.
- A failed `emerge` or module build stops the run with the output tail in the
  record. Nothing is rolled back; the files written so far are idempotent.

## Safety

- No provision command touches a disk, a partition table, or a filesystem. A test
  scans every generated provision program and fails on `wipefs`, `sfdisk`,
  `sgdisk`, `parted`, `mkfs`, `mkswap`, `blkdiscard`, `dd`, `umount`, and
  `swapoff`.
- The approval summary lists the kind, "Disk erase: no", the phases that change the system,
  the target, the host identity, the GPU, the CPU and memory, the Portage values, `Edits:`
  (files), `Regenerates:` (`/boot/grub/grub.cfg`), `Network:`, the kernel sources link, the
  packages with their dependencies, the licenses accepted (only for the package), and the
  reboot note.
- The human sees the licenses and packages in the approval dialog and in the plan
  summary before approving. Nothing is accepted implicitly.
- The identity guard keeps a plan on the server it was made for.
- The agent still runs no arbitrary command: only the frozen programs of an
  approved plan, and the read-only allowlist.

## Testing

- Tests of the provision phase programs: structure, quoting of hostile values, the awk block
  guard against unbalanced markers, and the safety scan below.
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

- The licenses (supplied by the operator from `emerge --pretend`) and the stable or testing
  keyword of a driver version that supports the RTX 5070.
- That `eselect kernel` is already set after the `gentoo-kernel-bin` install, and
  that the module builds against `/usr/src/linux-<version>-gentoo-dist-bin`.
- That `grub-mkconfig` reads the plan's block in `/etc/default/grub` and nothing after it
  overrides `GRUB_CMDLINE_LINUX` (the plan owns a block, no drop-in).
- The remaining items are listed in Task 13 of the implementation plan and in
  `docs/guides/provision.md` (not yet verified on a real server).
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
