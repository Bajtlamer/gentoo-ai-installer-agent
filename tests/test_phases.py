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


if __name__ == "__main__":
    unittest.main()
