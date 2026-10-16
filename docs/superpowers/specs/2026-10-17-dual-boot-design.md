# Dual Boot with Windows: Design

Date: 2026-10-17
Status: approved in conversation, awaiting written review

## Goal

When the target machine has Windows on another disk, the Gentoo install keeps
it working and bootable. After the install, the firmware starts Gentoo's GRUB,
whose menu lists Gentoo (default) and Windows. Nothing on the Windows disk is
mounted or written.

## Operator decisions

- The other operating system is Windows (UEFI). Other Linux systems are out of
  scope for this version.
- Default boot: the GRUB menu, with Gentoo preselected; GRUB is first in the
  firmware boot order.
- A disk that holds Windows may still be chosen as the install target, but
  only with an extra typed confirmation by the human.
- Secure Boot: detected; plan creation refuses a UEFI install while it is on.
- Approach: an explicit GRUB entry built from the read-only inventory and
  frozen in the plan (not `os-prober`), so the approved plan states exactly
  what the menu will contain.

## Non-goals

- Windows installed in BIOS (legacy) mode: detected and reported, never added
  to the menu in this version.
- Installing Gentoo into free space on the Windows disk (shared disk).
- Secure Boot support (shim, MOK enrollment).
- Detecting BitLocker precisely; a warning is shown whenever Windows is found.
- Other Linux systems in the GRUB menu (`os-prober` may come later as an
  opt-in).

## 1. Detection

### New read-only inventory commands

Added to `READ_ONLY_COMMANDS` in `core/ssh.py`:

| Label | Command | Purpose |
|---|---|---|
| `partitions` | `lsblk -J -b -e7 -o PATH,TYPE,PKNAME,PARTTYPE,PARTTYPENAME,FSTYPE,UUID,PARTUUID,SIZE,LABEL` | partition type GUID, filesystem, UUID, and parent disk of every partition |
| `secure_boot` | reads the last byte of `/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c` with `od`; prints `1`, `0`, or `unknown` when the file is missing | Secure Boot state |

Both are read-only and need no tool beyond the live ISO's `lsblk` and `od`.

### `core/foreign_os.py` (new, pure, no SSH)

`detect_foreign_os(inventory) -> list[dict]` reads the `partitions` result and
returns one record per disk that holds Windows:

```json
{"disk": "/dev/sdb", "os": "windows", "esp_uuid": "ABCD-1234",
 "evidence": ["esp vfat /dev/sdb1", "msr /dev/sdb2", "ntfs /dev/sdb3"]}
```

Windows (UEFI) means one disk with:
- an EFI system partition (type `c12a7328-f81f-11d2-ba4b-00a0c93ec93b`,
  filesystem `vfat`), and
- a Microsoft reserved partition (`e3c9e316-0b5c-4db8-817d-f92df00215ae`) or an
  `ntfs` partition with the Microsoft basic data type
  (`ebd0a0a2-b9e5-4433-87c0-68b6b72699c7`).

Windows (BIOS) means an MBR disk with an `ntfs` or `BitLocker` partition and
no ESP; it is reported with `"os": "windows-bios"` and no ESP fields.

Only the strict UEFI rule above builds menu entries. A broader rule protects
disks from an ordinary erase (review fix, 2026-10-17): any disk with a Microsoft
reserved partition or an `ntfs` or `BitLocker` partition on any partition table
is `"windows-data"` when neither rule above matches, and every disk with a vfat
ESP becomes `"windows-data"` (evidence `esp used by Windows on <disk>`) while
another disk holds Windows without an ESP. A disk whose partitions all lack a
partition type or filesystem makes the inventory unusable.

All values from the inventory are untrusted: the ESP UUID must match
`^[0-9A-F]{4}-[0-9A-F]{4}$`, paths must pass the existing device path rule, and
evidence strings must be printable; anything else makes the record invalid
(`INVENTORY_MISSING`, refresh the inventory).

`parse_secure_boot(inventory) -> "on" | "off" | "unknown"`.

### Discovery output

`summarize_inventory` (and therefore `detect_hardware` and `inspect`) gains
`secure_boot` and, per disk, `existing_os` (`windows`, `windows-bios`, or
`null`).

## 2. Plan and approval

### New install setting

`dual_boot`: `"auto"` (default) or `"off"`. Settable through `resolve_config`
overrides and through `install.json`. With `auto`, every UEFI Windows on a disk
other than the target becomes a menu entry. `config/install.default.json`
gains `"dual_boot": "auto"`; `init_config.py` gains `--no-dual-boot`.

### Derived field

`create_install_plan` (used by both the CLI and the MCP `create_plan` tool)
derives `install["dual_boot_entries"]` from the inventory before it generates
the commands; `resolve_config` only previews the same list:

```json
[{"os": "windows", "disk": "/dev/sdb", "esp_uuid": "ABCD-1234",
  "loader": "/EFI/Microsoft/Boot/bootmgfw.efi"}]
```

It is a derived field like `partition_layout`: never accepted from overrides,
profiles, or fallbacks. `validated_settings()` checks every entry: the UUID
format above, the disk path rule, `os == "windows"`, and `loader` exactly equal
to the constant `/EFI/Microsoft/Boot/bootmgfw.efi`.

### What the plan freezes

- `install.dual_boot_entries` (inside the hashed install).
- The disk identity (serial or WWN) of every Windows disk with an entry joins
  `plan["disks"]`, so the existing drift check (start and before every
  destructive apply) stops the run if that disk is gone or swapped.
- `secure_boot` from the inventory.
- `erases_existing_os` when the target disk itself holds Windows (see below).

### Refusals and warnings at plan creation

| Situation | Result |
|---|---|
| UEFI target and `secure_boot == "on"` | `CONFIG_CONFLICT`: Secure Boot is on; turn it off in the firmware setup, boot the live ISO again, refresh the inventory |
| UEFI target and `secure_boot == "unknown"` | warning in the plan and the approval dialog |
| Windows in BIOS mode found on another disk | warning: it will not be in the GRUB menu |
| `dual_boot == "auto"` and no Windows found | no entries, no warning |

### Erasing a disk that holds Windows

- `resolve_config` never infers a disk with `existing_os`, even when it is the
  only eligible disk; it must be named explicitly.
- The plan records `erases_existing_os: {"disk", "os", "evidence"}`.
- The approval dialog starts with
  `WARNING: /dev/sdb contains Windows (<evidence>). It will be erased.`
- The confirmation phrase is extended everywhere a human confirms:
  - CLI `approve`: `APPROVE <plan_id> ERASE WINDOWS <disk>` instead of
    `APPROVE <plan_id>`.
  - MCP `approve_plan`: the elicitation form gets a required text field; the
    server compares it exactly with `ERASE WINDOWS <disk>`. A missing or
    different value records nothing and returns `APPROVAL_DECLINED`.
  - `prepare_install.py`: the prompt asks for `ERASE WINDOWS <disk>` instead of
    `ERASE <disk>` for such a disk.
- The approval record stores that the extended phrase was used; an approval
  without it does not satisfy `require_approved_plan()` for such a plan.

### Approval dialog lines

With entries:
`Dual boot: Windows on /dev/sdb (EFI UUID ABCD-1234) will be added to the GRUB menu. Gentoo becomes the default; Windows may ask once for its BitLocker recovery key.`

All values shown go through the existing escaping in `approval_summary`.

## 3. Bootloader phase

### Apply (UEFI, non-empty `dual_boot_entries`)

Inside the chroot, after both `grub-install` calls and before `grub-mkconfig`:

1. For each entry: `blkid -U <uuid>` must resolve to a block device, else
   print `Windows EFI partition <uuid> not found; refresh the inventory` and
   exit non-zero. Its parent disk (`/dev/` plus `lsblk -no PKNAME`) must be the
   entry's disk, else print `Windows EFI partition <uuid> is not on <disk>;
   refresh the inventory` and exit 1.
2. Write `/etc/grub.d/45_gentoo_agent_dualboot`, mode 755, every line quoted
   with `q()`:

   ```
   #!/bin/sh
   exec tail -n +3 "$0"
   menuentry 'Windows Boot Manager (on /dev/sdb)' --class windows {
       insmod part_gpt
       insmod fat
       search --no-floppy --fs-uuid --set=root ABCD-1234
       chainloader /EFI/Microsoft/Boot/bootmgfw.efi
   }
   ```

   The file is rewritten on every run, so the phase stays idempotent.
   `/etc/grub.d/40_custom` is not touched.
3. `grub-mkconfig -o /boot/grub/grub.cfg`. Gentoo stays entry 0, the default.

`grub-install` already puts Gentoo first in the firmware boot order, and both
installs (`--bootloader-id=Gentoo` and `--removable`) write only to Gentoo's
own ESP (`--efi-directory=/boot`). Nothing mounts or writes the Windows disk.

With empty `dual_boot_entries` (no Windows, `dual_boot == "off"`, or BIOS
target) the program is byte-identical to today's.

### Checks

The bootloader check and the `validation` phase add, per entry,
`grep -Fq 'search --no-floppy --fs-uuid --set=root <uuid>' /mnt/gentoo/boot/grub/grub.cfg`.
A missing entry fails the verify step (`PHASE_FAILED` with the output tail); a
missing Windows entry never counts as success.

## Error handling summary

- Untrusted inventory values: strict format checks, `INVENTORY_MISSING` on
  anything unexpected.
- Secure Boot on: refused before any approval.
- Windows disk gone or swapped during the run: `TARGET_DRIFT` before the next
  destructive apply.
- Windows ESP UUID gone at the bootloader phase: the apply stops with a clear
  line; `PHASE_FAILED`.
- Wrong or missing extended confirmation: nothing is recorded.

## Testing

Unit and golden tests, no host contacted:

- `foreign_os`: lsblk fixtures for Windows on GPT, Windows in BIOS mode, a
  Linux-only disk, an empty disk, and hostile values (bad UUID, control
  characters).
- `parse_secure_boot`: `1`, `0`, missing file, garbage.
- Plan creation: entries derived for another disk; none with `dual_boot: off`;
  Secure Boot on refused; unknown warns; a Windows target gives
  `erases_existing_os`; `resolve_config` does not infer a Windows disk; the
  Windows disk identity is in `plan["disks"]`.
- Approval: CLI extended phrase required and checked; MCP extra field missing
  or wrong records nothing; dialog warning and dual-boot lines; an ordinary
  approval does not satisfy a plan with `erases_existing_os`.
- Validation: hostile `dual_boot_entries` refused (UUID, path, loader, os).
- Golden: a seventh configuration `uefi_gpt_dualboot_windows` (amd64, target
  `/dev/nvme0n1`, Windows on `/dev/sda`); the existing six stay unchanged.

Manual test: the amd64 VMware VM on Windows 11 with a second virtual disk
holding Windows (UEFI, Secure Boot off). Install Gentoo on the other disk,
then check that GRUB lists Gentoo (default) and Windows, and that both boot.

## Documentation

- `docs/CONFIGURATION.md`: `dual_boot`, `dual_boot_entries` (derived), the new
  plan-creation refusals.
- `docs/guides/cli-install.md` and `docs/guides/ai-install.md`: a dual-boot
  section (prerequisites: Windows in UEFI mode, Secure Boot off; what the menu
  looks like; the extended confirmation when erasing a Windows disk).
- `docs/TROUBLESHOOTING.md`: Secure Boot refusal, missing Windows ESP,
  BitLocker recovery prompt, Windows in BIOS mode.
- `docs/ARCHITECTURE.md`: `core/foreign_os.py`, the new inventory commands,
  the plan fields.
- `CLAUDE.md`: module list.
