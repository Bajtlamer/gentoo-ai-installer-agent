# Provisioning an installed system

A provision plan changes a Gentoo system that is already installed and running. The `nvidia-driver` profile tunes Portage for all CPU
threads and installs the NVIDIA driver (open kernel modules). It uses the same approval as an install plan,
never touches a disk, and never reboots.

Status: implemented on branch `provision-plans-impl`. It has not been run on a real server yet; see
[Not yet verified on a real server](#not-yet-verified-on-a-real-server).

## Contents

- [Prerequisites](#prerequisites)
- [Steps with the CLI](#steps-with-the-cli)
- [Steps with the MCP tools](#steps-with-the-mcp-tools)
- [Hardware profile and profiles](#hardware-profile-and-profiles)
- [The desktop-plasma profile](#the-desktop-plasma-profile)
- [What the nvidia-driver plan changes](#what-the-nvidia-driver-plan-changes)
- [Resume](#resume)
- [Not yet verified on a real server](#not-yet-verified-on-a-real-server)
- [Undo each change by hand](#undo-each-change-by-hand)

## Prerequisites

- The system was installed by this tool, booted from its disk, and is reachable over SSH as `root` with the
  installed key. A provision plan refuses any other SSH user. `config/target.json` must point at the installed
  system, not at the live ISO.
- The installed system has a distribution kernel (`*-gentoo-dist*` in `uname -r`) with its sources in
  `/usr/src/linux-<release>`, `/boot` mounted as vfat, 8 GiB free on `/`, a default route, and `emerge`. The
  `preflight` phase refuses otherwise and prints which test failed.
- The SSH host key changed when the installed system replaced the live ISO. Until the new key is trusted,
  every command stops with `HOST_KEY_UNKNOWN`. Verify the new fingerprint on the target console
  (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`), compare it with `ssh-keyscan -t ed25519 <host>`, then
  replace the old entry:

  ```sh
  ssh-keygen -R <host>
  ssh-keyscan -t ed25519 <host> >> ~/.ssh/known_hosts
  ```

  If `known_hosts_file` in `config/target.json` names a dedicated file, use `ssh-keygen -R <host> -f <file>`
  and append to that file. Only append the key after the fingerprints match. Then check:

  ```sh
  ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -i <identity_file> root@<host> true
  ```

- If the machine dual boots Windows, Fast Boot (and on VMware `efi.quickBoot.enabled`) stays off, or GRUB
  cannot see the Windows disk. The plan regenerates `grub.cfg`; the Windows entry must still be there.
- You know the license and the `package.use` lines Portage requires for the driver. The agent never guesses
  them. On the target run:

  ```sh
  USE="dist-kernel -tools" emerge --pretend --autounmask-license=y x11-drivers/nvidia-drivers
  ```

  (the USE value is the one the plan sets). Copy the license name from the block "The following license
  changes are necessary" and every `package.use` line from the block "The following USE changes are
  necessary". Give exactly those as `accept_licenses` and `package_use`. The plan writes the licenses for the
  package `x11-drivers/nvidia-drivers` only, never for `*/*`.

  Output on the first real server for x11-drivers/nvidia-drivers-595.104.02 (the list of 13 packages that
  precedes the change blocks and the `# required by` comment lines are left out here):

  ```text
  The following USE changes are necessary to proceed:
  >=media-libs/libglvnd-1.7.0 X

  The following license changes are necessary to proceed:
  =x11-drivers/nvidia-drivers-595.104.02 NVIDIA-2025
  ```

  Here the license is `NVIDIA-2025` and the only USE change is `>=media-libs/libglvnd-1.7.0 X`. The plan does
  not enable the `tools` flag (nvidia-settings): with it Portage would also want
  `>=media-libs/freetype-2.14.3 harfbuzz` and `>=x11-libs/cairo-1.18.4-r1 X` and pull about 50 packages,
  including gtk+ and rust-bin. From driver 595 the open kernel module is the only module and there is no
  `kernel-open` flag; the plan sets `dist-kernel -tools` itself.

## Steps with the CLI

1. `gentoo-agent provision inspect`. It connects to the target and runs the read-only provision inventory.
   Check the CPU threads, the memory, the GPU, and `portage_overrides` (variables the plan's value will
   replace in `make.conf`).
2. `gentoo-agent provision plan --accept-license NVIDIA-2025 --package-use ">=media-libs/libglvnd-1.7.0 X"`.
   Repeat `--accept-license` for every license (1 to 8) and `--package-use` for every line (0 to 8). The values
   here are examples: use what Portage reported. `--profile` defaults to `nvidia-driver`, the
   only profile. This step reads the saved inventory and does not connect to the target; it refuses an
   inventory older than 24 hours.
3. Read the summary. You accept the licenses by approving, in an interactive terminal:
   `gentoo-agent approve <plan_id>`.
4. `gentoo-agent run start <plan_id>`, then `gentoo-agent run status <run_id>` and
   `gentoo-agent run log <run_id>`.
5. Reboot the machine yourself when the run ends in `installed_pending_reboot`.
6. After the reboot, prove the GPU. The `verify_install` tool (MCP only, see below) runs every check. Without
   MCP run the `gpu-online` checks by hand on the target: `lsmod | grep '^nvidia '`, `nvidia-smi -L`,
   `modinfo -F license nvidia`, and `cat /sys/module/nvidia_drm/parameters/modeset` (must print `Y`).

## Steps with the MCP tools

1. `detect_provision` with `refresh=true` (read-only inventory; without `refresh` it summarizes the latest
   saved one).
2. `create_provision_plan` with `accept_licenses` and `package_use` (the license names and `package.use` lines
   the human gave you or showed you from `emerge --pretend`; never invent them) and the default `profile`
   `nvidia-driver`.
3. `approve_plan`: a human reads the dialog and confirms. There is no erase phrase for a provision plan.
4. `start_install`, then `get_run_status` and `get_run_log` until the run ends.
5. The human reboots the target.
6. `verify_install` with the plan id. All seven phases must report `complete`, including `gpu-online`.

## Hardware profile and profiles

The provision inventory also reads the architecture (`uname -m`), the current Portage profile
(`eselect profile show`), the available Portage profiles (`eselect profile list`), and the https `sync-uri`
values of `/etc/portage/binrepos.conf/*.conf`. All four are read-only. An inventory saved before these
commands existed still works; the new facts are then empty.

From the inventory the code builds a hardware profile: architecture, CPU threads, memory, the GPUs, the GPU
vendors, `video_cards` (the `VIDEO_CARDS` values from a fixed vendor map in code: `nvidia`, `amdgpu` and
`radeonsi`, `intel`; `None` when a vendor is unknown, listed in `unknown_vendors`), and `virtual` (all GPUs
belong to a hypervisor vendor). Nothing in it is guessed.

Provision profiles live in a registry (`gentoo_agent/core/profiles.py`). A profile applies to a machine only
when its reason is `None`: each profile returns either no reason or a text that says why it does not fit.
`detect_provision` returns `hardware` and a `profiles` list with `id`, `title`, `applicable`, and `reason` for
each profile, and `create_provision_plan` refuses a profile that does not apply.

Two profiles exist: `nvidia-driver` and `desktop-plasma`. `nvidia-driver` applies to exactly one NVIDIA GPU
and refuses everything else. `desktop-plasma` is described [below](#the-desktop-plasma-profile). Driver
profiles for AMD, Intel, hybrid, virtual, and no-GPU machines are not written yet (see the
[roadmap](../ROADMAP.md)).

## The desktop-plasma profile

Installs KDE Plasma on an installed, running OpenRC system: the Portage profile `.../desktop/plasma`, the
`VIDEO_CARDS` value from the detected GPUs, `kde-plasma/plasma-meta`, `kde-apps/konsole`, `kde-apps/dolphin`,
SDDM, `gui-libs/display-manager-init`, and elogind, with the services enabled for the next start. It is its own
provision plan with its own approval. Status: implemented, not yet run on a real server (see
[What only the first real run confirms](#what-only-the-first-real-run-confirms)).

### Operator workflow

Prerequisite: if the machine has an NVIDIA GPU, the `nvidia-driver` plan was applied and the server was
restarted, so that `nvidia-smi -L` lists the card. The `preflight` phase refuses otherwise. Then:

1. `provision inspect` (MCP: `detect_provision` with `refresh=true`). The result lists the profiles and whether
   `desktop-plasma` applies; if not, it says why.
2. `provision plan --profile desktop-plasma` (MCP: `create_provision_plan` with `profile="desktop-plasma"`).
   It takes no license (`--accept-license` is refused: the NVIDIA license stays in the file of the nvidia plan).
   `--package-use` (0 to 8 lines) is only for USE changes Portage asks for. The code owns the line
   `x11-drivers/nvidia-drivers wayland` and refuses package.use lines for that package.
3. Read the approval summary: the GPUs, `VIDEO_CARDS`, the Portage profile switch (system-wide), the world
   update (can take hours), the binary host, the USE changes, the edits, and the packages. Approve it yourself
   in an interactive terminal or in the MCP dialog.
4. `run start <plan_id>`, then `run status` and `run log` until the run ends in `installed_pending_reboot`.
5. Restart the server yourself. The run never reboots and never starts a service.
6. `verify_install` with the plan id (MCP only; by hand run `rc-service display-manager status`,
   `pgrep -x sddm`, and check `/usr/share/wayland-sessions/plasma.desktop`). All eight phases must be
   `complete`, including `desktop-online`.

If `plasma-config` fails because Portage reports a missing USE change after the profile switch, take the lines
from `emerge --pretend kde-plasma/plasma-meta` on the target, make a new plan with `--package-use`, and
resume or start again. Never invent a line.

### Hardware to profile mapping

| Detected GPU vendor | `VIDEO_CARDS` | Extra |
|---------------------|---------------|-------|
| NVIDIA | `nvidia` | `x11-drivers/nvidia-drivers wayland` in `package.use`; preflight needs the driver live |
| AMD | `amdgpu radeonsi` | none |
| Intel | `intel` | none |
| Unknown vendor | refused | the plan says a display device has an unknown vendor |
| No display device | refused | "No display device was detected" |

The values are sorted and without duplicates, one list over all GPUs. The first real server has an Intel iGPU
(`8086:4680`) next to the NVIDIA GPU, so `VIDEO_CARDS` is `intel nvidia`. Only amd64 is supported. The current
Portage profile must be exactly `default/linux/amd64/<version>` (plain OpenRC; hardened, systemd, split-usr,
and llvm variants are refused with a reason) or already `<that>/desktop/plasma`, and the target
`<base>/desktop/plasma` must appear exactly once in `eselect profile list`.

### Phases

| Phase | Runs | What it does |
|-------|------|--------------|
| `preflight` | yes, read-only | root, default route, `emerge`, `eselect`, `rc-update`, 20 GiB free on `/`, and with NVIDIA `nvidia-smi -L` |
| `plasma-profile` | yes | `eselect profile set <target>` after checking that the target is still offered |
| `plasma-config` | yes | a `VIDEO_CARDS` block in `make.conf`, `/etc/portage/package.use/gentoo-agent-desktop`; the check also runs `emerge --pretend` on the packages, so it can only pass after the profile switch |
| `plasma-update` | yes | `emerge --getbinpkg --update --deep --newuse @world` |
| `plasma-install` | yes | `emerge --getbinpkg --noreplace` of the six packages |
| `plasma-services` | yes | a block in `/etc/conf.d/display-manager` (`CHECKVT=7`, `DISPLAYMANAGER="sddm"`), `rc-update add elogind boot`, `rc-update add display-manager default` |
| `plasma-verify` | yes, read-only | packages installed, the Wayland session file, files and runlevels |
| `desktop-online` | no (`run: false`) | after the restart: `rc-service display-manager status` shows started, `sddm` runs, the session file exists |

### Decision: the SDDM greeter stays on X11

SDDM offers a Wayland greeter, but on OpenRC it needs seatd and weston wrapper scripts that the Gentoo wiki
says must be reapplied after SDDM updates. A wrapper that an update silently wipes leaves a server without a
login screen, and this tool must not leave such a state. The plan therefore keeps the default X11 greeter and
still offers the `Plasma (Wayland)` session in the session list. A Wayland greeter is on the roadmap.

### Facts and sources (retrieved 2026-10-20)

- SDDM wiki, https://wiki.gentoo.org/wiki/SDDM: on OpenRC install `gui-libs/display-manager-init`, set
  `CHECKVT=7` and `DISPLAYMANAGER="sddm"` in `/etc/conf.d/display-manager`, then `rc-update add elogind boot`
  and `rc-update add display-manager default`. The Wayland greeter needs the seatd and weston wrappers above.
- Elogind wiki: `rc-update add elogind boot`. With dbus built with the `elogind` flag, starting elogind brings
  up the system dbus, so there is no separate dbus runlevel entry.
- Real server probe (read-only SSH, 2026-10-20): Portage profile `default/linux/amd64/23.0`;
  `eselect profile list` offers `default/linux/amd64/23.0/desktop/plasma` exactly once (next to the systemd and
  split-usr variants); `/etc/portage/binrepos.conf/gentoo.conf` has
  `sync-uri = https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64` and
  `verify-signature = true`; the tree has `kde-plasma/plasma-meta` 6.7.5, `kde-apps/konsole`,
  `kde-apps/dolphin`, `x11-misc/sddm`, `gui-libs/display-manager-init`, and `sys-auth/elogind`; there was no
  `/etc/conf.d/display-manager`. `emerge --pretend kde-plasma/plasma-meta` fails under the old profile (qtbase
  `wayland? ( opengl )`), which is why the resolution check runs only after the profile switch.

### What only the first real run confirms

1. The exact output of `rc-service display-manager status` after the restart (the check greps for `started`).
2. That the Plasma Wayland session starts on NVIDIA driver 595 with `nvidia-drm.modeset=1`.
3. That `pgrep -x sddm` matches the process name of the SDDM daemon.
4. That binary packages (`--getbinpkg`) match the USE flags after the profile switch, and how much is compiled.
5. That `/var/db/pkg/<category>/<name>-[0-9]*` finds every installed package (the checks use this pattern).
6. That `portageq envvar VIDEO_CARDS` prints `intel nvidia` for the plan block.

### Risks

- Hours of compilation: the world update after the profile switch can rebuild a large part of the system when
  binary packages do not match. The run keeps going phase by phase and resumes; a failed `emerge` is not rolled
  back.
- The profile switch changes the whole system, not only Plasma. It is reversible by `eselect profile set` but
  the rebuilt packages are not.
- USE changes after the profile switch come only from the human, from `emerge --pretend` output. Until they are
  supplied `plasma-config` fails, by design, before any package is built.
- The services are enabled but not started, so the first start happens at the restart. If SDDM fails, the
  console is still reachable over SSH and at the text console.
- NVIDIA with Wayland is the least proven part (see item 2 above).

## What the nvidia-driver plan changes

The approval summary lists all of this: the kind and "Disk erase: no", the phases that change the system, the
target, the host identity (host name, root filesystem UUID, SSH host key fingerprint), the GPU as
`NVIDIA display controller [10de:xxxx]` (read from `/sys/bus/pci/devices`; the name is built from the vendor id
in code, nothing from the target is shown as a name), the CPU and memory, the Portage values, the files
edited (`Edits:`), the regenerated boot menu (`Regenerates:`), the network use (`Network:`), the kernel
sources link, the packages with their dependencies, the licenses accepted (only for the package), and the
reboot note.

Phases, in order:

| Phase | Runs | What it does |
|-------|------|--------------|
| `preflight` | yes, read-only | Refuses unless: root, a default route, `emerge`, `/boot` is a mounted vfat filesystem, 8 GiB free on `/`, `/usr/src/linux-$(uname -r)` exists, and the running kernel matches `*-gentoo-dist*`. |
| `portage-tuning` | yes | Writes `MAKEOPTS` and `EMERGE_DEFAULT_OPTS` into a block in `/etc/portage/make.conf`. |
| `portage-sync` | yes | `emerge-webrsync`. Counts as done when `timestamp.chk` of the tree is under 2 days old. |
| `nvidia-config` | yes | Writes the license and USE files, checks with `emerge --pretend` that Portage can resolve the driver with them, adds a block to `/etc/default/grub`, runs `eselect kernel set linux-<running release>`. |
| `nvidia-drivers` | yes | `emerge --noreplace --verbose x11-drivers/nvidia-drivers`, then `grub-mkconfig -o /boot/grub/grub.cfg`. |
| `nvidia-verify` | yes, read-only | The module exists for the running kernel and `nouveau` is blacklisted. |
| `gpu-online` | no (`run: false`) | Checked only by `verify_install` after the reboot: the `nvidia` module is loaded, `nvidia-smi -L` lists an NVIDIA device, the module license contains GPL (open modules), and `/sys/module/nvidia_drm/parameters/modeset` is `Y`. |

Files and blocks:

- `/etc/portage/make.conf`: a block between `# gentoo-agent portage-tuning begin` and
  `# gentoo-agent portage-tuning end`, for example `MAKEOPTS="-j15"` and
  `EMERGE_DEFAULT_OPTS="--jobs=3 --load-average=24"`.
- `/etc/portage/package.license/gentoo-agent-nvidia`: `x11-drivers/nvidia-drivers <licenses>`.
- `/etc/portage/package.use/gentoo-agent-nvidia`: `x11-drivers/nvidia-drivers dist-kernel -tools` on the first
  line, then every line of `package_use`.
- `/etc/default/grub`: a block between `# gentoo-agent nvidia begin` and `# gentoo-agent nvidia end` that
  appends `nvidia-drm.modeset=1` to `GRUB_CMDLINE_LINUX`.
- `/boot/grub/grub.cfg`: regenerated.
- The `/usr/src/linux` symlink: set to the running kernel's sources.

Both blocks are edited by a portable awk program (no `sed -i`). It refuses with exit 1 and leaves the file
unchanged when the begin and end markers do not match, when there is more than one block, or when the end
marker comes before the begin marker. It adds a missing final newline first and uses a temporary file that a
trap removes. See [TROUBLESHOOTING.md](../TROUBLESHOOTING.md#the-plan-block-in-makeconf-or-grub-is-refused).

Portage values (a rule of thumb, derived at plan creation from the inventory): `MAKEOPTS` jobs are the smaller
of the CPU thread count and the RAM in whole GiB divided by 2 (at least 1); parallel emerge jobs are the
thread count divided by 8, at least 1 and at most 4; the load average equals the thread count. The wording of
this rule in the Gentoo Handbook has not been confirmed yet, so it is not cited as a source.

## Resume

A provision plan has no partitioning, so `gentoo-agent run resume <run_id>` (MCP: `resume_run`) is safe after
an interrupted or failed run: the run re-checks the host identity, then every phase's check is run first and
completed phases are skipped. A failed `emerge` is not rolled back. If a license, keyword, or USE error needs
a different input, create a new plan (see
[TROUBLESHOOTING.md](../TROUBLESHOOTING.md#a-provision-run-fails-in-emerge)).

## Not yet verified on a real server

Tests cannot check these; the first real run does. If one is wrong, the generators and tests change.

1. `portageq envvar MAKEOPTS` and `portageq envvar EMERGE_DEFAULT_OPTS` print the effective values after the
   `make.conf` block is written, exactly `-j15` and `--jobs=3 --load-average=24` for those settings.
2. `x11-drivers/nvidia-drivers <license>` is accepted syntax in `/etc/portage/package.license/` and covers
   every license the package and its dependencies need.
3. `eselect kernel set linux-<uname -r>` names the installed sources directory.
4. `modprobe -c` lists `blacklist nouveau` once the driver is installed (the ebuild blacklists it).
5. `nvidia-smi` is found under `/opt/bin` for a non-login `sh -c` session.
6. The awk block replacement in `make.conf` and `/etc/default/grub` removes only the plan's own block, and
   `grub-mkconfig` reads `/etc/default/grub`.
7. `emerge-webrsync` refreshes `/var/db/repos/gentoo/metadata/timestamp.chk`.
8. The open module builds against `/usr/src/linux-<release>-gentoo-dist-bin` with `dist-kernel` set.
9. `emerge-webrsync` on an already current tree, and the modification time of `timestamp.chk` after a sync
   (the check allows 2 days; the file's time may be the snapshot's build time).
10. `portageq` prints exactly the tuned values (quotes stripped, nothing added by the profile or overridden
    later).
11. `uname -r` matches `*-gentoo-dist*` on the installed kernel, `eselect kernel list` shows
    `linux-$(uname -r)`, and the dist tree is a usable build tree (`.config`, `Module.symvers`).
12. The driver version that supports the RTX 5070 is available without `~amd64` (otherwise `emerge` fails and
    the plan needs a keyword file), and the dependencies need no license besides the accepted ones.
13. `modinfo -F license nvidia` reports a license containing `GPL` for the open modules.
14. Nothing after the plan block in `/etc/default/grub` overrides `GRUB_CMDLINE_LINUX`; the regenerated
    `grub.cfg` contains `nvidia-drm.modeset=1` on the Gentoo entries and still has the Windows entry.
15. `/etc/portage/make.conf` is a regular file, `package.use` and `package.license` are directories or absent,
    and `ip`, `findmnt`, `emerge-webrsync`, and `grub-mkconfig` are on the PATH of a non-interactive root ssh
    shell.

Also open: the Handbook wording of the `MAKEOPTS` rule, the GPU reading on the real card (the summary shows
only the PCI id `10de:xxxx`; check it against the card you expect), and whether the framebuffer console
distortion goes away with the driver.

## Undo each change by hand

Nothing is rolled back automatically. Every change is a file named `gentoo-agent-*` or a marked block.

1. Remove the Portage tuning: delete the lines from `# gentoo-agent portage-tuning begin` to
   `# gentoo-agent portage-tuning end` in `/etc/portage/make.conf`. Check that no `MAKEOPTS` you set yourself
   was in the file before (the plan summary warned about it).
2. Remove the license and USE files:
   `rm /etc/portage/package.license/gentoo-agent-nvidia /etc/portage/package.use/gentoo-agent-nvidia`.
3. Remove the GRUB block: delete the lines from `# gentoo-agent nvidia begin` to `# gentoo-agent nvidia end` in
   `/etc/default/grub`, then `grub-mkconfig -o /boot/grub/grub.cfg`.
4. Remove the driver: `emerge --deselect x11-drivers/nvidia-drivers`, then `emerge --depclean`. Reboot.
5. The `/usr/src/linux` symlink: `eselect kernel list` and `eselect kernel set <number>` to choose another
   sources directory.
