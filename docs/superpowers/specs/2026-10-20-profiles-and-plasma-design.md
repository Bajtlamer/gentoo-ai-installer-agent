# Provision Profiles and the KDE Plasma Purpose: Design

Date: 2026-10-20
Status: approved in conversation, awaiting written review

This is part 2 of the second phase of the dedicated server install, and the
first step of the product requirement recorded in `docs/ROADMAP.md`
("hardware-driven, purpose-driven profiles"). Part 1 (provision plans and the
NVIDIA driver) is implemented and verified on a real server. Other GPU vendors
(AMD, Intel, hybrid, virtual, none), other desktops (GNOME and more), and the
vLLM stack get their own specs and plans on top of this one.

## Goal

1. Turn "what a provision plan does" into a registry of profiles that are chosen
   from the detected hardware, with an explicit refusal and a reason for every
   profile that does not fit the machine. Today's `nvidia-driver` becomes the
   first entry without any change of behavior.
2. Add the first purpose profile, `desktop-plasma`, that installs KDE Plasma
   (Wayland session) on an OpenRC system with the NVIDIA driver of part 1,
   through the same approved-plan safety model.
3. Make every setting follow the data of the hardware inventory: compile
   settings, the GPU vendor and PCI id, `VIDEO_CARDS`, the Portage profile name,
   the architecture. Nothing is assumed from one machine.

## Operator decisions

- Plasma runs a Wayland session (the default session of Plasma 6). The NVIDIA
  driver gets the `wayland` USE flag for it.
- Packages come from the official Gentoo binary host where the USE flags match
  and are compiled otherwise (`emerge --getbinpkg`).
- Each purpose is its own plan with its own approval. A purpose plan needs the
  GPU profile to be applied first, and a plan that is interrupted resumes
  without redoing finished phases.
- This spec covers the profile registry and `desktop-plasma` only. Other GPU
  profiles and other desktops follow in later specs.

## Non-goals

- Profiles for AMD, Intel, hybrid, virtual, or no GPU beyond the detection and
  the `VIDEO_CARDS` derivation. They need their own driver and firmware work.
- GNOME, other desktops, display managers other than SDDM, and the vLLM stack.
- A tailored kernel (roadmap plan 4); the distribution kernel stays.
- systemd (out of scope for this tool), and non-amd64 architectures for the
  desktop profile in this version.
- Choosing the Plasma application set interactively: the plan installs
  `kde-plasma/plasma-meta` and a short fixed list of applications.

## Architecture

### Hardware profile

A pure function over the saved provision inventory, extending
`summarize_provision_inventory` (`core/provision_facts.py`): CPU threads and
model, memory, kernel release, host identity, display-class PCI devices with
vendor and PCI id, and four new facts read by new read-only commands in
`PROVISION_READ_ONLY_COMMANDS` (`core/ssh.py`):

| Label | Command (read-only) | Used for |
|---|---|---|
| `arch` | `uname -m` | architecture gate |
| `portage_profile` | `eselect profile show` | current Portage profile |
| `portage_profiles` | `eselect profile list` | the exact name of the Plasma profile |
| `binrepos` | the `*.conf` files of `/etc/portage/binrepos.conf` (read with `cat`) | which binary hosts the system already has |

`VIDEO_CARDS` is derived from the detected vendors with a fixed map
(`10de` to `nvidia`, `1002` to `amdgpu radeonsi`, `8086` to `intel`); an unknown
vendor makes the profile refuse instead of guessing. The approval dialog shows
the detected devices and the derived value, so the human sees the mapping.

No new persisted artifact: the plan freezes the facts it used, and the inventory
is re-read (at most 24 hours old) when a plan is created. Hardware does not
change between inventories; a changed machine is caught by the host identity.

### Profile registry

`core/profiles.py` holds one record per profile: `id`, `applicable(hardware)`
returning `None` or a refusal reason, `validate(settings)`, and the phase
generators (`PHASES`, `PHASE_FLAGS`, `command_for`, `check_command_for`).
`create_provision_plan` dispatches on `settings["profile"]`; the plan keeps the
fields of part 1 (`kind: provision`, `identity`, `provision`, phases with
`destructive` and `run`), and `verify_provision_programs` regenerates through
the same registry. `detect_provision` adds `profiles`: every registered profile
with `applicable` and the reason. Plans of part 1 stay valid and unchanged.

### Purpose `desktop-plasma`

Settings: `profile: desktop-plasma`, `package_use` (operator-supplied lines, as
in part 1), `extra_packages` is not offered (YAGNI). Applicability: amd64,
OpenRC, exactly the profile name `default/linux/<arch>/<version>/desktop/plasma`
present in `eselect profile list` with the same `<version>` as the current
profile (no match or more than one match refuses), and, when an NVIDIA GPU is
present, the NVIDIA profile applied (checked by a program at run time, `nvidia-smi
-L` after the reboot of part 1).

Phases (each with an idempotent apply and a read-only check, programs frozen in
the plan, `set -eu`, values quoted, plan-owned blocks edited by the existing awk
block routine):

1. `preflight`: root, route, `emerge`, free space, running distribution kernel,
   and the GPU requirement above.
2. `plasma-profile`: `eselect profile set <exact name from the inventory>`; the
   check compares `eselect profile show`.
3. `plasma-config`: a plan block in `make.conf` with `VIDEO_CARDS`; package.use
   `x11-drivers/nvidia-drivers wayland` (only with an NVIDIA GPU) and the
   operator lines; an `emerge --pretend --getbinpkg` resolution check on the
   target packages so a missing USE change fails before the long build.
4. `plasma-update`: `emerge --getbinpkg --update --deep --newuse @world` (the
   profile switch changes USE globally); the check is a pretend of the same
   command that must list nothing to do.
5. `plasma-install`: `emerge --getbinpkg --noreplace kde-plasma/plasma-meta`
   (it pulls SDDM by default) plus the terminal emulator and the file manager
   (`konsole` and `dolphin`; their exact atoms are confirmed on the server); the
   check looks in `/var/db/pkg`.
6. `plasma-services`: enable the services of the OpenRC desktop (D-Bus, elogind,
   the display manager with SDDM selected). The exact commands and the variable
   of `/etc/conf.d/display-manager` are confirmed on the real server before the
   programs are written (see below).
7. `plasma-verify` (before the reboot): packages installed, services added,
   Wayland session file present.
8. `desktop-online` (`run: false`, checked by `verify_install` after the
   reboot): the display manager is started and the Plasma session is offered.

The installer never reboots; after `plasma-services` the plan ends in
`installed_pending_reboot`.

### Binary packages

Programs call `emerge --getbinpkg` only; no global `FEATURES` change and no
binhost URL in the plan. The binhost comes from the `binrepos.conf` that the
stage3 ships (read into the inventory and shown in the dialog). Portage
verifies signatures itself and runs `getuto` automatically with `--getbinpkg`.
Packages whose USE differ from the binary are compiled. The number of binary
and source packages is not known before the profile switch, so the approval
dialog states the mechanism and the run log shows the outcome.

### Approval

The summary gets the hardware-to-profile mapping: detected display devices and
vendors, `VIDEO_CARDS`, the Wayland session, the Portage profile that will be
set (old and new name), the packages, the binary-then-source behavior, the files
edited (`make.conf` block, `package.use`, the profile symlink), and the reboot
note. Values from the target go through `_shown`; the profile name, vendor
tokens, and `package_use` lines pass strict formats before they reach a program.

### Components and boundaries

- `core/hardware_profile.py` (new): the pure derivation above, one function per
  fact, no I/O.
- `core/profiles.py` (new): the registry and its dispatch.
- `core/provision_phases.py`: unchanged programs of `nvidia-driver`, moved
  behind the registry entry without text changes (the existing golden-like tests
  and `sh -n` tests must stay green).
- `core/desktop_phases.py` (new): the phase programs of `desktop-plasma`.
- `core/provision.py`: settings validation gains the per-profile dispatch.
- `core/ssh.py`, `core/provision_facts.py`: four read-only commands and parsers.
- MCP and CLI: `detect_provision` returns `profiles`; `create_provision_plan`
  takes `profile`; the instructions text describes the desktop workflow.

## Error handling

- A profile that does not fit the hardware refuses with `CONFIG_INVALID` and a
  reason that names the missing fact (no desktop profile in the list, systemd,
  other architecture, unknown GPU vendor, NVIDIA driver not applied).
- Unusable inventory results stay `INVENTORY_MISSING`.
- A failed `emerge` stops the run with the output tail in the record; nothing is
  rolled back; every phase is idempotent so `resume_run` continues.
- A mismatch between the stored programs and the settings stays
  `PLAN_HASH_MISMATCH` (part 1).

## Safety

- Same approval model, hash, identity guard, and no disk command in any program
  (the safety scan of part 1 is extended to the new programs).
- The profile switch and the world update are system-wide changes; the dialog
  says so in its own line.
- The agent never guesses: the Plasma profile name comes from the target's own
  list, license names and `package_use` lines come from the human or from
  `emerge --pretend` output the human shows.

## Testing

- Unit tests for each derivation with real fixtures: the inventory of the
  10.0.1.17 server (already stored as fixtures) plus synthetic inventories for
  AMD, Intel, hybrid, virtual, and no GPU, each expected to give a defined
  result (an applicable profile or a refusal with a reason).
- `sh -n` and the safety scan for every new program; behavior tests of the block
  routine on temporary files stay.
- The registry keeps part 1 unchanged: its tests and the plan-id sensitivity
  tests pass without edits.
- A real run on 10.0.1.17 as the first verification, the same way as part 1.

## Verified facts and their sources

Source: Gentoo Wiki, KDE, https://wiki.gentoo.org/wiki/KDE (sections
Prerequisites > Profile, Plasma > Installation, Plasma > Starting Plasma).
Retrieved 2026-10-19T22:28Z.

- The profile for OpenRC is `desktop/plasma` (listed as
  `default/linux/amd64/23.0/desktop/plasma`), the systemd one is
  `desktop/plasma/systemd`; selection is `eselect profile list` and `eselect
  profile set`.
- `kde-plasma/plasma-meta` is the full Plasma desktop and pulls SDDM by default
  (`+sddm`, `+elogind`); `kde-plasma/plasma-desktop` is a basic desktop with
  limited support; `kdecore-meta` is the KDE Gear basics (file browser, editor,
  terminal emulator).
- With both `+wayland` and `X` in `plasma-login-sessions` the Wayland session is
  the default. `VIDEO_CARDS` must be set for Mesa.

Source: Gentoo Wiki, Binary package guide,
https://wiki.gentoo.org/wiki/Binary_package_guide (sections on binrepos.conf,
signatures, USE flag matching, pitfalls). Retrieved 2026-10-19T22:28Z.

- `/etc/portage/binrepos.conf` configures binary hosts; many stages ship one.
  `--getbinpkg` downloads binaries and falls back to source.
- Portage calls `getuto` automatically with `--getbinpkg`; remote binhosts
  require valid signatures by default since 2026-05.
- Binaries whose USE differ are not used (`--binpkg-respect-use=y`).
- The official amd64 packages are built with `-march=x86-64 -mtune=generic`.
- From the earlier `emerge --pretend` on the real server: `x11-drivers/nvidia-drivers-595.104.02`
  has a `wayland` USE flag (shown as `-wayland`).

## To verify on the real server, not assumed

- The commands and files of the display manager on OpenRC (`rc-update` for dbus,
  elogind, display-manager; the variable in `/etc/conf.d/display-manager`); the
  KDE page does not give them.
- That the stage3 of this system ships a `binrepos.conf`, and its content.
- The output of `eselect profile list` and `eselect profile show` on this system,
  and that exactly one `desktop/plasma` entry exists.
- That `emerge --pretend --getbinpkg` works without extra signature setup and
  how many packages it would take from binaries after the profile switch.
- NVIDIA with a Wayland session: that Plasma starts a Wayland session with
  driver 595 and `nvidia-drm.modeset=1` (the KDE page says nothing about it);
  the NVIDIA wiki is consulted when the programs are written.
- The exact package atoms of the terminal emulator and the file manager.
- The size of the world update after the profile switch, and the USE changes
  Portage asks for.

## Risks

- A long run without binaries (Qt WebEngine and the world rebuild after the
  profile switch can take hours with 20 threads); the binary-then-source
  behavior is the mitigation, and the run is resumable.
- Portage may require USE changes after the profile switch; they are supplied by
  the human from `emerge --pretend`, as in part 1, and the pretend check fails
  early instead of mid-build.
- A Wayland session on NVIDIA may need driver or environment settings that the
  documents above do not cover; the first real run decides, and a failing
  `desktop-online` check shows it after the reboot without changing anything.
