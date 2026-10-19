import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import config, phases
from gentoo_agent.core.errors import CONFIG_CONFLICT, CONFIG_INVALID, AgentError
from tests.helpers import temp_home


ROOT = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "user": "root", "identity_file": "~/.ssh/id_test"}


def fixture(name: str) -> dict:
    return json.loads((ROOT / "tests" / "golden" / "configs" / f"{name}.json").read_text(encoding="utf-8"))


GPT_TYPES = {"efi-system": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
             "linux-swap": "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f",
             "linux-root": "0fc63daf-8483-4772-8e79-3d69d8477de4"}
MBR_TYPES = {"linux-swap": "0x82", "linux-root": "0x83"}
MSR_TYPE = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
BASIC_DATA_TYPE = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
RECOVERY_TYPE = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
MIB = 1024 * 1024


class StorageCheckTests(unittest.TestCase):
    """Runs the generated storage check against stub lsblk and fdisk commands.

    The check must tell the planned layout from an old disk that merely has partitions with the
    same numbers: a Windows disk with three partitions once passed it, so the apply was skipped
    and the filesystems phase formatted the old partitions.
    """

    DISK_BYTES = 2000398934016

    def run_check(self, config_name: str, rows: list[tuple[str, str, int, str]], disk_bytes: int | None = None):
        install = fixture(config_name)
        command = phases.storage_check_command(TARGET, install)
        disk, _ = phases.validated_settings(TARGET, install)
        disk_bytes = disk_bytes or self.DISK_BYTES
        label = "dos" if install["partition_table"] == "mbr" else "gpt"
        table = "\n".join([f"{disk} disk {disk_bytes} "] + [f"{path} part {size} {kind}" for path, _t, size, kind in rows])
        with tempfile.TemporaryDirectory() as tmp:
            stubs = Path(tmp)
            (stubs / "rows").write_text(table + "\n", encoding="utf-8")
            (stubs / "lsblk").write_text(
                '#!/bin/sh\ncase "$*" in\n'
                f'  *"-o SIZE"*) echo {disk_bytes};;\n'
                '  *"-o TYPE"*) echo disk;;\n'
                f'  *) cat {shlex.quote(str(stubs / "rows"))};;\nesac\n', encoding="utf-8")
            (stubs / "fdisk").write_text(f'#!/bin/sh\necho "Disklabel type: {label}"\n', encoding="utf-8")
            for name in ("lsblk", "fdisk"):
                (stubs / name).chmod(0o755)
            program = 'test() { if [ "$1" = -b ]; then return 0; fi; command test "$@"; }\n' + command
            return subprocess.run(["sh", "-c", program], capture_output=True, text=True,
                                  env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"})

    def planned_rows(self, config_name: str, disk_bytes: int | None = None):
        install = fixture(config_name)
        _, layout = phases.validated_settings(TARGET, install)
        types = MBR_TYPES if install["partition_table"] == "mbr" else GPT_TYPES
        disk_bytes = disk_bytes or self.DISK_BYTES
        explicit = sum(item["size_mib"] * MIB for item in layout if item.get("size_mib"))
        rows = []
        for item in layout:
            size = item["size_mib"] * MIB if item.get("size_mib") else disk_bytes - explicit - MIB
            rows.append((item["device"], "part", size, types.get(item["type"], types["linux-root"])))
        return rows

    def test_the_planned_gpt_layout_passes(self):
        result = self.run_check("uefi_gpt_user_sudo", self.planned_rows("uefi_gpt_user_sudo"))
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "storage-ok"), result.stderr)

    def test_the_planned_mbr_layout_passes(self):
        result = self.run_check("bios_mbr_user_sudo", self.planned_rows("bios_mbr_user_sudo"))
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "storage-ok"), result.stderr)

    def test_an_old_windows_disk_with_three_partitions_fails(self):
        disk = phases.validated_settings(TARGET, fixture("uefi_gpt_user_sudo"))[0]
        old = [(f"{disk}p1" if disk[-1].isdigit() else f"{disk}1", "part", 16 * MIB, MSR_TYPE),
               (f"{disk}p2" if disk[-1].isdigit() else f"{disk}2", "part", 1999448834048, BASIC_DATA_TYPE),
               (f"{disk}p3" if disk[-1].isdigit() else f"{disk}3", "part", 931135488, RECOVERY_TYPE)]
        result = self.run_check("uefi_gpt_user_sudo", old)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("storage-ok", result.stdout)

    def test_a_wrong_partition_type_fails(self):
        for index, kind in ((0, MSR_TYPE), (1, BASIC_DATA_TYPE), (2, MSR_TYPE)):
            with self.subTest(index=index):
                rows = self.planned_rows("uefi_gpt_user_sudo")
                rows[index] = (*rows[index][:3], kind)
                self.assertNotEqual(self.run_check("uefi_gpt_user_sudo", rows).returncode, 0)

    def test_a_wrong_fixed_size_fails(self):
        for index in (0, 1):
            with self.subTest(index=index):
                rows = self.planned_rows("uefi_gpt_user_sudo")
                rows[index] = (rows[index][0], "part", rows[index][2] + MIB, rows[index][3])
                self.assertNotEqual(self.run_check("uefi_gpt_user_sudo", rows).returncode, 0)

    def test_a_root_partition_that_does_not_fill_the_disk_fails(self):
        rows = self.planned_rows("uefi_gpt_user_sudo")
        rows[-1] = (rows[-1][0], "part", 931135488, rows[-1][3])
        self.assertNotEqual(self.run_check("uefi_gpt_user_sudo", rows).returncode, 0)

    def test_a_wrong_mbr_type_fails(self):
        rows = self.planned_rows("bios_mbr_user_sudo")
        rows[0] = (*rows[0][:3], "0x83" if rows[0][3] == "0x82" else "0x82")
        self.assertNotEqual(self.run_check("bios_mbr_user_sudo", rows).returncode, 0)

    def test_a_missing_partition_fails(self):
        self.assertNotEqual(self.run_check("uefi_gpt_user_sudo", self.planned_rows("uefi_gpt_user_sudo")[:-1]).returncode, 0)


class StorageApplyTests(unittest.TestCase):
    """Runs the generated storage program against stub commands that only record their calls.

    sfdisk does not clear the signatures inside new partitions. A new partition that starts where an
    old one did keeps the old filesystem signature, and the filesystems phase then skips mkfs because
    it sees a filesystem already: the 512 MB EFI partition kept a 16 MB vfat from a failed run.
    """

    def run_apply(self, config_name: str, mounted: bool) -> list[str]:
        install = fixture(config_name)
        command = phases.storage_command(TARGET, install)
        disk, layout = phases.validated_settings(TARGET, install)
        root = next(item["device"] for item in layout if item.get("mount") == "/")
        swap = next((item["device"] for item in layout if item.get("type") == "linux-swap"), "")
        with tempfile.TemporaryDirectory() as tmp:
            stubs = Path(tmp)
            log = stubs / "calls"
            log.write_text("", encoding="utf-8")
            outputs = {"lsblk": "/mnt/gentoo" if mounted else "", "findmnt": root if mounted else "",
                       "swapon": swap if mounted else ""}
            for name in ("wipefs", "sfdisk", "partprobe", "udevadm", "lsblk", "findmnt", "swapon"):
                (stubs / name).write_text(
                    f'#!/bin/sh\necho "{name} $*" >> {shlex.quote(str(log))}\n'
                    + ('cat >/dev/null\n' if name == "sfdisk" else "")
                    + (f"printf '%s\\n' {shlex.quote(outputs[name])}\n" if name in outputs and outputs[name] else ""),
                    encoding="utf-8")
                (stubs / name).chmod(0o755)
            program = 'test() { if [ "$1" = -b ]; then return 0; fi; command test "$@"; }\n' + command
            result = subprocess.run(["sh", "-c", program], capture_output=True, text=True,
                                    env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.layout = [item["device"] for item in layout]
            return log.read_text(encoding="utf-8").splitlines()

    def test_a_fresh_table_clears_the_signatures_of_every_new_partition(self):
        for name in ("uefi_gpt_user_sudo", "bios_mbr_user_sudo"):
            with self.subTest(config=name):
                calls = self.run_apply(name, mounted=False)
                sfdisk = next(i for i, c in enumerate(calls) if c.startswith("sfdisk --wipe always"))
                wiped = [dev for c in calls[sfdisk + 1:] if c.startswith("wipefs -a ") for dev in c.split()[2:]]
                self.assertEqual(sorted(wiped), sorted(self.layout))
                self.assertTrue(any(c.startswith("udevadm settle") for c in calls[sfdisk + 1:calls.index(
                    next(c for c in calls[sfdisk + 1:] if c.startswith("wipefs -a ")))]),
                    "the partition nodes must exist before they are wiped")

    def test_a_disk_in_use_keeps_its_filesystems(self):
        calls = self.run_apply("uefi_gpt_user_sudo", mounted=True)
        self.assertTrue(any(c.startswith("sfdisk --no-reread --force") for c in calls))
        self.assertEqual([c for c in calls if c.startswith("wipefs")], [])


class PhaseGeneratorTests(unittest.TestCase):
    def test_phases_module_does_not_load_target_config_itself(self):
        self.assertFalse(hasattr(phases, "target_config"))

    def test_storage_command_uses_the_given_target(self):
        command = phases.storage_command(TARGET, fixture("bios_mbr_user_sudo"))
        self.assertIn("sfdisk", command)
        self.assertIn("disk=/dev/sda", command)

    def test_efi_mount_checks_the_exact_mount_point(self):
        # findmnt -T resolves to the root filesystem once /mnt/gentoo is mounted,
        # which skipped the EFI mount on every fresh UEFI install.
        command = phases.filesystem_command(TARGET, fixture("uefi_gpt_user_sudo"))
        self.assertIn('if ! findmnt -rn -M "$mount_root/boot" >/dev/null; then mount', command)
        self.assertNotIn('findmnt -rn -T "$mount_root/boot"', command)

    def test_uefi_with_mbr_is_a_conflict(self):
        install = fixture("uefi_gpt_user_sudo")
        install["partition_table"] = "mbr"
        with self.assertRaises(AgentError) as caught:
            phases.validated_settings(TARGET, install)
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)

    def test_unsafe_disk_path_is_invalid(self):
        install = fixture("bios_mbr_user_sudo")
        install["disk"] = "/dev/sda;reboot"
        with self.assertRaises(AgentError) as caught:
            phases.validated_settings(TARGET, install)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_missing_identity_file_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            phases.public_key_for_target({"user": "root"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_unknown_phase_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            phases.command_for("reboot", TARGET, fixture("bios_mbr_user_sudo"))
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


def chroot_lines(command: str) -> list[str]:
    """Return the lines of the bash program passed to chroot."""
    tokens = shlex.split(command)
    program = tokens[tokens.index("-c") + 1]
    return program.splitlines()


class ConfigurationQuotingTests(unittest.TestCase):
    def configuration(self, **changes) -> list[str]:
        install = fixture("uefi_gpt_root_only_static")
        install.update(changes)
        with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
            return chroot_lines(phases.configuration_command(TARGET, install))

    def network_tokens(self, lines: list[str]) -> list[str]:
        line = next(line for line in lines if line.startswith("printf") and line.endswith(">> /etc/dhcpcd.conf"))
        return shlex.split(line)

    def test_static_network_lines_are_quoted_once(self):
        tokens = self.network_tokens(self.configuration())
        self.assertEqual(tokens[2:8], [
            "# gentoo-agent static network begin",
            "interface enp1s0",
            "static ip_address=192.168.10.20/24",
            "static routers=192.168.10.1",
            "static domain_name_servers=1.1.1.1 9.9.9.9",
            "# gentoo-agent static network end",
        ])

    def test_previous_block_is_removed_before_the_new_one_is_written(self):
        lines = self.configuration()
        sed = next(index for index, line in enumerate(lines) if line.startswith("sed -i"))
        write = next(index for index, line in enumerate(lines) if line.endswith(">> /etc/dhcpcd.conf"))
        self.assertLess(sed, write)
        self.assertEqual(shlex.split(lines[sed]), [
            "sed", "-i", "/^# gentoo-agent static network begin$/,/^# gentoo-agent static network end$/d",
            "/etc/dhcpcd.conf",
        ])
        self.assertFalse(any("dhcpcd.conf.d" in line for line in lines))

    def test_hostile_static_values_stay_single_arguments(self):
        hostile = "10.0.0.2/24 x; touch /PWNED"
        base = {"interface": "enp1s0", "address": "10.0.0.2/24", "gateway": "10.0.0.1", "dns": ["1.1.1.1"]}
        for change in (
            {"address": hostile},
            {"gateway": "a'b; touch /PWNED"},
            {"dns": ["1.1.1.1'; touch /PWNED; '"]},
            {"interface": "eth0'; touch /PWNED; '"},
        ):
            with self.subTest(change=change):
                tokens = self.network_tokens(self.configuration(network_static={**base, **change}))
                self.assertNotIn("touch", tokens)
                self.assertEqual(len(tokens), 10)
        tokens = self.network_tokens(self.configuration(network_static={**base, "address": hostile}))
        self.assertIn("static ip_address=" + hostile, tokens)

    def test_static_check_reads_dhcpcd_conf(self):
        install = fixture("uefi_gpt_root_only_static")
        with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
            check = phases.configuration_check_command(TARGET, install)
        self.assertIn("grep -Fqx 'interface enp1s0' /mnt/gentoo/etc/dhcpcd.conf", check)
        self.assertIn("grep -Fqx 'static ip_address=192.168.10.20/24' /mnt/gentoo/etc/dhcpcd.conf", check)

    def test_openrc_user_services_are_disabled_once_on_rerun(self):
        # pam_openrc starts user.<name> on login; without elogind there is no
        # XDG_RUNTIME_DIR, so every login printed "ERROR: user.<name> failed to start".
        lines = [line for line in self.configuration() if "/etc/rc.conf" in line]
        self.assertTrue(lines)
        with tempfile.TemporaryDirectory() as directory:
            rc_conf = Path(directory) / "rc.conf"
            rc_conf.write_text('#rc_autostart_user="YES"\nrc_parallel="NO"\nrc_autostart_user="YES"\n', encoding="utf-8")
            program = "\n".join(lines).replace("/etc/rc.conf", shlex.quote(str(rc_conf)))
            for _ in range(2):
                subprocess.run(["sh", "-eu", "-c", program], check=True)
            self.assertEqual(rc_conf.read_text(encoding="utf-8"),
                             '#rc_autostart_user="YES"\nrc_parallel="NO"\nrc_autostart_user="NO"\n')

    def test_configuration_check_requires_user_services_off(self):
        with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
            check = phases.configuration_check_command(TARGET, fixture("uefi_gpt_user_sudo"))
        self.assertIn("grep -Fqx 'rc_autostart_user=\"NO\"' /mnt/gentoo/etc/rc.conf", check)

    def render_fstab(self, name: str) -> str:
        """Run the generated fstab lines with a fake blkid that prints UUID-<device name>."""
        install = fixture(name)
        with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
            lines = chroot_lines(phases.configuration_command(TARGET, install))
        lines = [line for line in lines if "fstab" in line or "_uuid=" in line or line.startswith("test -n")]
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            blkid = bin_dir / "blkid"
            blkid.write_text('#!/bin/sh\nfor arg; do last=$arg; done\nprintf "UUID-%s\\n" "${last##*/}"\n', encoding="utf-8")
            blkid.chmod(0o755)
            fstab = Path(directory) / "fstab"
            program = "\n".join(lines).replace("/etc/fstab", shlex.quote(str(fstab)))
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            subprocess.run(["sh", "-eu", "-c", program], check=True, env=env)
            return fstab.read_text(encoding="utf-8")

    def test_uefi_fstab_mounts_the_efi_partition_at_boot(self):
        # The kernel and grub.cfg live on the EFI partition; without this line the
        # installed system left /boot empty and kernel updates went to the root filesystem.
        self.assertEqual(self.render_fstab("uefi_gpt_user_sudo"),
                         "UUID=UUID-nvme0n1p3 / ext4 noatime 0 1\n"
                         "UUID=UUID-nvme0n1p2 none swap sw 0 0\n"
                         "UUID=UUID-nvme0n1p1 /boot vfat umask=0077,tz=UTC 0 2\n")
        self.assertNotIn("/boot", self.render_fstab("bios_mbr_user_sudo"))

    def test_uefi_installs_dosfstools_for_the_boot_check(self):
        with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
            uefi = phases.configuration_command(TARGET, fixture("uefi_gpt_user_sudo"))
            bios = phases.configuration_command(TARGET, fixture("bios_mbr_user_sudo"))
            check = phases.configuration_check_command(TARGET, fixture("uefi_gpt_user_sudo"))
            bios_check = phases.configuration_check_command(TARGET, fixture("bios_mbr_user_sudo"))
        self.assertIn("sys-fs/dosfstools", uefi)
        self.assertNotIn("sys-fs/dosfstools", bios)
        self.assertIn("grep -Eq '^UUID=[^ ]+ /boot vfat ' /mnt/gentoo/etc/fstab", check)
        self.assertNotIn("/boot vfat", bios_check)

    def test_zoneinfo_path_is_quoted(self):
        lines = self.configuration(timezone="UTC; touch /PWNED")
        line = next(line for line in lines if line.startswith("ln -snf"))
        self.assertEqual(shlex.split(line), ["ln", "-snf", "/usr/share/zoneinfo/UTC; touch /PWNED", "/etc/localtime"])


class ConfigLoadingTests(unittest.TestCase):
    def test_missing_target_config_is_invalid(self):
        with temp_home():
            with self.assertRaises(AgentError) as caught:
                config.target_config()
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


class ApplyProgramStrictnessTests(unittest.TestCase):
    def test_every_apply_program_starts_with_set_eu(self):
        for config_path in sorted((ROOT / "tests" / "golden" / "configs").glob("*.json")):
            install = json.loads(config_path.read_text(encoding="utf-8"))
            for phase in phases.PHASES:
                with self.subTest(config=config_path.stem, phase=phase):
                    with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
                        program = phases.command_for(phase, TARGET, install)
                    self.assertEqual(program.splitlines()[0], "set -eu")

    def test_failed_chroot_does_not_write_the_configuration_marker(self):
        with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
            program = phases.configuration_command(TARGET, fixture("bios_mbr_user_sudo"))
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            touched = bin_dir / "touched"
            (bin_dir / "chroot").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (bin_dir / "touch").write_text(f"#!/bin/sh\necho \"$@\" >> {shlex.quote(str(touched))}\n", encoding="utf-8")
            for name in ("chroot", "touch"):
                os.chmod(bin_dir / name, 0o755)
            result = subprocess.run(["/bin/sh", "-c", program], env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
                                    capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(touched.exists())


class ArchitectureProgramTests(unittest.TestCase):
    def render(self, name: str, phase: str, check: bool = False) -> str:
        install = fixture(name)
        with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
            generate = phases.check_command_for if check else phases.command_for
            return generate(phase, TARGET, install)

    def test_preflight_checks_the_live_machine(self):
        self.assertIn('test "$(uname -m)" = x86_64', self.render("uefi_gpt_user_sudo", "preflight"))
        self.assertIn('test "$(uname -m)" = aarch64', self.render("uefi_gpt_arm64_user_sudo", "preflight"))

    def test_arm64_stage_and_bootloader(self):
        stage = self.render("uefi_gpt_arm64_user_sudo", "base-system")
        self.assertIn("https://distfiles.gentoo.org/releases/arm64/autobuilds/latest-stage3-arm64-openrc.txt", stage)
        self.assertNotIn("amd64", stage)
        self.assertIn("--target=arm64-efi", self.render("uefi_gpt_arm64_user_sudo", "bootloader"))
        self.assertIn("/mnt/gentoo/boot/EFI/Gentoo/grubaa64.efi", self.render("uefi_gpt_arm64_user_sudo", "bootloader", check=True))

    def test_uefi_bootloader_installs_efibootmgr_and_a_removable_fallback(self):
        # grub-install needs efibootmgr to write the firmware boot entry; some
        # firmware (VMware included) only loads /EFI/BOOT.
        for name, fallback in (("uefi_gpt_user_sudo", "BOOTX64.EFI"), ("uefi_gpt_arm64_user_sudo", "BOOTAA64.EFI")):
            with self.subTest(name=name):
                apply = self.render(name, "bootloader")
                self.assertIn("emerge --noreplace --verbose sys-boot/grub sys-boot/efibootmgr", apply)
                self.assertIn("--efi-directory=/boot --removable", apply)
                self.assertIn(f"/mnt/gentoo/boot/EFI/BOOT/{fallback}", self.render(name, "bootloader", check=True))
        self.assertNotIn("efibootmgr", self.render("bios_mbr_user_sudo", "bootloader"))

    def test_amd64_programs_are_unchanged_apart_from_the_machine_check(self):
        self.assertIn("--target=x86_64-efi", self.render("uefi_gpt_user_sudo", "bootloader"))
        self.assertIn("grubx64.efi", self.render("uefi_gpt_user_sudo", "bootloader", check=True))


class InitSystemTests(unittest.TestCase):
    def test_base_system_always_uses_the_openrc_stage(self):
        program = phases.base_system_command(fixture("uefi_gpt_user_sudo"))
        self.assertIn("latest-stage3-amd64-openrc.txt", program)
        self.assertNotIn("systemd", program)

    def test_systemd_is_refused_by_the_gate(self):
        install = fixture("uefi_gpt_user_sudo")
        install["init_system"] = "systemd"
        with self.assertRaises(AgentError) as caught:
            phases.validated_settings(TARGET, install)
        self.assertEqual(caught.exception.details, {"field": "init_system"})


EXPECTED_DUAL_BOOT_SCRIPT = """#!/bin/sh
exec tail -n +3 "$0"
menuentry 'Windows Boot Manager (on /dev/sda)' --class windows {
    insmod part_gpt
    insmod fat
    search --no-floppy --fs-uuid --set=root ABCD-1234
    chainloader /EFI/Microsoft/Boot/bootmgfw.efi
}
"""


FAKE_ESP = "/dev/fake-windows-esp"


def stub(directory: Path, name: str, output: str | None) -> None:
    """A fake command that prints output, or fails with status 2 when output is None."""
    body = f"printf '%s\\n' {shlex.quote(output)}" if output is not None else "exit 2"
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    os.chmod(path, 0o755)


class DualBootProgramTests(unittest.TestCase):
    def run_lines(self, blkid_output: str | None, pkname: str | None = "sda") -> tuple[subprocess.CompletedProcess, Path]:
        """Run the rendered lines under bash (as in the chroot) with stubbed blkid and lsblk.

        test -b accepts only FAKE_ESP, so no real block device is needed.
        """
        install = fixture("uefi_gpt_dualboot_windows")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        tmp = Path(directory.name)
        script = tmp / "45_gentoo_agent_dualboot"
        stub(tmp, "blkid", blkid_output)
        stub(tmp, "lsblk", pkname)
        fake_test = f'test() {{ if [ "$1" = -b ]; then [ "$2" = {FAKE_ESP} ]; else builtin test "$@"; fi; }}'
        program = "\n".join(phases.dual_boot_lines(install["dual_boot_entries"]))
        program = program.replace(shlex.quote(phases.DUAL_BOOT_SCRIPT), shlex.quote(str(script)))
        result = subprocess.run(["bash", "-c", "set -eu\n" + fake_test + "\n" + program],
                                env={"PATH": f"{tmp}:/usr/bin:/bin"}, capture_output=True, text=True, check=False)
        return result, script

    def test_script_has_exactly_the_windows_entry_and_prints_it_for_grub_mkconfig(self):
        result, script = self.run_lines(FAKE_ESP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(script.read_text(encoding="utf-8"), EXPECTED_DUAL_BOOT_SCRIPT)
        self.assertEqual(os.stat(script).st_mode & 0o777, 0o755)
        printed = subprocess.run([str(script)], capture_output=True, text=True, check=True).stdout
        self.assertEqual(printed, "".join(EXPECTED_DUAL_BOOT_SCRIPT.splitlines(keepends=True)[2:]))

    def test_missing_windows_esp_stops_before_the_script_is_written(self):
        result, script = self.run_lines(None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Windows EFI partition ABCD-1234 not found; refresh the inventory", result.stderr)
        self.assertFalse(script.exists())

    def test_windows_esp_on_another_disk_stops_before_the_script_is_written(self):
        for pkname in ("sdb", "", None):
            with self.subTest(pkname=pkname):
                result, script = self.run_lines(FAKE_ESP, pkname=pkname)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("Windows EFI partition ABCD-1234 is not on /dev/sda; refresh the inventory",
                              result.stderr)
                self.assertFalse(script.exists())

    def test_entries_go_between_grub_install_and_grub_mkconfig(self):
        program = phases.bootloader_command(TARGET, fixture("uefi_gpt_dualboot_windows"))
        inside = chroot_lines(program)
        removable = next(i for i, line in enumerate(inside) if "--removable" in line)
        written = next(i for i, line in enumerate(inside) if "45_gentoo_agent_dualboot" in line)
        mkconfig = next(i for i, line in enumerate(inside) if line.startswith("grub-mkconfig"))
        self.assertLess(removable, written)
        self.assertLess(written, mkconfig)
        self.assertNotIn("40_custom", program)
        self.assertNotIn("os-prober", program)
        self.assertNotIn("mount", program)

    def test_bootloader_and_validation_checks_require_the_windows_entry(self):
        install = fixture("uefi_gpt_dualboot_windows")
        line = "grep -Fq 'search --no-floppy --fs-uuid --set=root ABCD-1234' /mnt/gentoo/boot/grub/grub.cfg"
        self.assertIn(line, phases.bootloader_check_command(install).splitlines())
        self.assertIn(line, phases.validation_command(TARGET, install).splitlines())
        for program in (phases.bootloader_check_command(install), phases.validation_command(TARGET, install)):
            self.assertEqual(program.splitlines()[-1][:6], "printf")

    def test_a_grub_cfg_without_the_entry_fails_the_check(self):
        install = fixture("uefi_gpt_dualboot_windows")
        line = phases.dual_boot_checks(install)[0]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "grub.cfg"
            cfg.write_text("menuentry 'Gentoo GNU/Linux' {\n}\n", encoding="utf-8")
            check = line.replace(shlex.quote("/mnt/gentoo/boot/grub/grub.cfg"), shlex.quote(str(cfg)))
            self.assertNotEqual(subprocess.run(["/bin/sh", "-c", check], check=False).returncode, 0)
            cfg.write_text("    search --no-floppy --fs-uuid --set=root ABCD-1234\n", encoding="utf-8")
            self.assertEqual(subprocess.run(["/bin/sh", "-c", check], check=False).returncode, 0)

    def test_hostile_entries_never_reach_a_program(self):
        install = fixture("uefi_gpt_dualboot_windows")
        install["dual_boot_entries"][0]["esp_uuid"] = "ABCD-1234'; reboot; '"
        for generate in (lambda: phases.bootloader_command(TARGET, install),
                         lambda: phases.bootloader_check_command(install),
                         lambda: phases.validation_command(TARGET, install)):
            with self.assertRaises(AgentError) as caught:
                generate()
            self.assertEqual(caught.exception.details, {"field": "dual_boot_entries"})

    def test_bios_and_no_entries_add_nothing(self):
        for name in ("bios_mbr_user_sudo", "uefi_gpt_user_sudo"):
            install = fixture(name)
            self.assertEqual(phases.dual_boot_checks(install), [])
            self.assertNotIn("45_gentoo_agent_dualboot", phases.bootloader_command(TARGET, install))


if __name__ == "__main__":
    unittest.main()
