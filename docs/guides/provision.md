# Provisioning an installed system

A provision plan changes a Gentoo system that is already installed and running: it tunes Portage for all CPU
threads and installs the NVIDIA driver (open kernel modules). It uses the same approval as an install plan,
never touches a disk, and never reboots.

Status: implemented on branch `provision-plans-impl`. It has not been run on a real server yet; see
[Not yet verified on a real server](#not-yet-verified-on-a-real-server).

## Contents

- [Prerequisites](#prerequisites)
- [Steps with the CLI](#steps-with-the-cli)
- [Steps with the MCP tools](#steps-with-the-mcp-tools)
- [What the plan changes](#what-the-plan-changes)
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

## What the plan changes

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
