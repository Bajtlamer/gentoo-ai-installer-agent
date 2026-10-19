import copy
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from gentoo_agent.core import desktop_phases as dp
from gentoo_agent.core.errors import CONFIG_INVALID, PLAN_HASH_MISMATCH, AgentError
from gentoo_agent.core.profiles import PROFILES, get_profile
from gentoo_agent.core.provision_plan import verify_provision_programs

TARGET = {"name": "gentoo-server2", "host": "192.0.2.10", "port": 22, "user": "root"}
PLASMA = "default/linux/amd64/23.0/desktop/plasma"
AMD = {"profile": "desktop-plasma", "package_use": [], "video_cards": ["amdgpu", "radeonsi"], "nvidia": False,
       "plasma_profile": PLASMA, "portage_profile": "default/linux/amd64/23.0"}
NVIDIA = {**AMD, "video_cards": ["nvidia"], "nvidia": True, "package_use": ["media-libs/a Y"]}
FORBIDDEN = re.compile(r"\b(wipefs|sfdisk|sgdisk|parted|mkfs\S*|mkswap|blkdiscard|dd|mount|umount|swapoff|reboot|"
                       r"shutdown|poweroff|halt)\b|rc-service\s+\S+\s+(start|restart|stop)|\*/\*")


def apply(phase, provision=AMD):
    return dp.command_for(phase, TARGET, provision)


def check(phase, provision=AMD):
    return dp.check_command_for(phase, TARGET, provision)


class PhaseListTests(unittest.TestCase):
    def test_order_and_flags(self):
        self.assertEqual(dp.PHASES, ("preflight", "plasma-profile", "plasma-config", "plasma-update",
                                     "plasma-install", "plasma-services", "plasma-verify", "desktop-online"))
        self.assertEqual(dp.PHASE_FLAGS["preflight"], (False, True))
        self.assertEqual(dp.PHASE_FLAGS["plasma-install"], (True, True))
        self.assertEqual(dp.PHASE_FLAGS["plasma-verify"], (False, True))
        self.assertEqual(dp.PHASE_FLAGS["desktop-online"], (False, False))
        self.assertEqual(set(dp.PHASE_FLAGS), set(dp.PHASES))

    def test_the_registry_entry_uses_them(self):
        profile = get_profile("desktop-plasma")
        self.assertEqual(profile.phases, dp.PHASES)
        self.assertEqual(profile.flags, dp.PHASE_FLAGS)
        for phase in dp.PHASES:
            self.assertEqual(profile.command_for(phase, TARGET, AMD), apply(phase))
            self.assertEqual(profile.check_command_for(phase, TARGET, AMD), check(phase))

    def test_an_unknown_phase_is_invalid(self):
        for function in (dp.command_for, dp.check_command_for):
            with self.assertRaises(AgentError) as caught:
                function("portage-sync", TARGET, AMD)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)


class SafetyTests(unittest.TestCase):
    def test_every_registered_profile_is_scanned(self):
        fixtures = {"nvidia-driver": [{"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2"],
                                       "tuning": {"jobs": 15, "emerge_jobs": 3, "load_average": 24}}],
                    "desktop-plasma": [AMD, NVIDIA]}
        self.assertEqual(set(fixtures), set(PROFILES))
        for profile_id, provisions in fixtures.items():
            profile = PROFILES[profile_id]
            for provision in provisions:
                for phase in profile.phases:
                    for kind, program in (("apply", profile.command_for(phase, TARGET, provision)),
                                          ("check", profile.check_command_for(phase, TARGET, provision))):
                        with self.subTest(profile=profile_id, phase=phase, kind=kind):
                            self.assertIsNone(re.search(r"\b(wipefs|sfdisk|sgdisk|parted|mkfs\S*|mkswap|blkdiscard|"
                                                        r"umount|swapoff|reboot|shutdown|poweroff|halt)\b", program))
                            self.assertTrue(program.startswith("set -eu\n"))

    def test_no_desktop_program_touches_a_disk_mounts_starts_a_service_or_reboots(self):
        for provision in (AMD, NVIDIA):
            for phase in dp.PHASES:
                for kind, program in (("apply", apply(phase, provision)), ("check", check(phase, provision))):
                    with self.subTest(phase=phase, kind=kind):
                        self.assertIsNone(FORBIDDEN.search(program), program)

    def test_checks_never_write(self):
        for phase in dp.PHASES:
            with self.subTest(phase=phase):
                program = check(phase)
                self.assertNotIn("rc-update add", program)
                self.assertNotIn("profile set", program)
                for line in program.split("\n"):
                    if line.startswith("emerge"):
                        self.assertIn("--pretend", line)
                self.assertNotIn("mkdir", program)
                self.assertNotIn(">>", program)


class ProfileAndConfigTests(unittest.TestCase):
    def test_profile_apply_verifies_the_name_then_sets_it(self):
        program = apply("plasma-profile")
        self.assertIn(f"eselect profile list | grep -Fq -- '{PLASMA} '", program)
        self.assertLess(program.index("grep -Fq"), program.index("eselect profile set"))
        self.assertIn(f"eselect profile set {PLASMA}", program)
        self.assertIn(f"test \"$(eselect profile show | tail -n 1 | tr -d ' ')\" = {PLASMA}", check("plasma-profile"))

    def test_profile_apply_refuses_an_unexpected_current_profile_before_setting_it(self):
        program = apply("plasma-profile")
        guard = ("test \"$current\" = default/linux/amd64/23.0 || "
                 f"test \"$current\" = {PLASMA} || ")
        self.assertIn("current=\"$(eselect profile show | tail -n 1 | tr -d ' ')\"", program)
        self.assertIn(guard, program)
        self.assertLess(program.index(guard), program.index("eselect profile set"))
        for current, expected in (("default/linux/amd64/23.0", 0), (PLASMA, 0), ("default/linux/amd64/17.1", 1)):
            with self.subTest(current=current):
                fake = "eselect() { case \"$1 $2\" in 'profile show') printf 'Current:\\n  %s\\n' " + current + \
                       "; return 0;; 'profile list') printf '%s \\n' " + PLASMA + "; return 0;; *) return 0;; esac; }\n"
                result = subprocess.run(["sh", "-c", fake + program], capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected:
                    self.assertIn("refusing to replace", result.stderr)

    def test_a_stored_block_without_the_current_profile_is_invalid(self):
        broken = {key: value for key, value in AMD.items() if key != "portage_profile"}
        with self.assertRaises(AgentError):
            apply("plasma-profile", broken)

    def test_config_writes_video_cards_block_and_an_empty_use_file(self):
        program = apply("plasma-config")
        self.assertIn("'VIDEO_CARDS=\"amdgpu radeonsi\"'", program)
        self.assertIn("awk -v b='# gentoo-agent desktop begin' -v e='# gentoo-agent desktop end'", program)
        self.assertIn(": > /etc/portage/package.use/gentoo-agent-desktop", program)
        self.assertNotIn("nvidia", program)

    def test_nvidia_lines_only_when_nvidia(self):
        self.assertIn("printf '%s\\n' 'x11-drivers/nvidia-drivers wayland' 'media-libs/a Y' > "
                      "/etc/portage/package.use/gentoo-agent-desktop", apply("plasma-config", NVIDIA))
        self.assertIn("grep -Fqx 'x11-drivers/nvidia-drivers wayland' /etc/portage/package.use/gentoo-agent-desktop",
                      check("plasma-config", NVIDIA))
        self.assertNotIn("nvidia-drivers", check("plasma-config"))

    def test_config_check_asks_portage_and_resolves_before_the_marker(self):
        lines = check("plasma-config").split("\n")
        self.assertIn("test \"$(portageq envvar VIDEO_CARDS)\" = 'amdgpu radeonsi'", lines)
        pretend = next(i for i, x in enumerate(lines) if x.startswith("emerge --pretend"))
        self.assertIn("--autounmask-write=n kde-plasma/plasma-meta kde-apps/konsole", lines[pretend])
        self.assertIn(dp.RESOLVE_FAILED_DESKTOP, lines[pretend])
        self.assertEqual(lines[pretend + 1], "printf 'plasma-config-ok\\n'")
        self.assertNotIn("emerge", apply("plasma-config"))


class OtherPhaseTests(unittest.TestCase):
    def test_preflight_is_its_own_check_and_nvidia_adds_the_driver_test(self):
        self.assertEqual(apply("preflight"), check("preflight"))
        self.assertIn("20971520", apply("preflight"))
        self.assertNotIn("nvidia-smi", apply("preflight"))
        program = apply("preflight", NVIDIA)
        self.assertIn('PATH="$PATH:/opt/bin"; nvidia-smi -L | grep -q NVIDIA', program)
        self.assertIn("the NVIDIA driver is not active; apply the nvidia-driver plan and restart first", program)
        self.assertTrue(program.endswith("printf 'preflight-ok\\n'"))

    def test_update_check_is_not_hidden_by_set_e_or_a_failing_emerge(self):
        program = check("plasma-update")
        self.assertNotIn("\n!", program)
        self.assertIn("--update --deep --newuse --nospinner --quiet @world", program)
        self.assertIn("exit 1", program)
        self.assertIn("emerge --getbinpkg --nospinner --verbose --update --deep --newuse @world", apply("plasma-update"))

    def test_install_is_noreplace_and_checks_every_package(self):
        self.assertIn("--noreplace kde-plasma/plasma-meta kde-apps/konsole kde-apps/dolphin x11-misc/sddm "
                      "gui-libs/display-manager-init sys-auth/elogind", apply("plasma-install"))
        program = check("plasma-install")
        for package in dp.PACKAGES:
            category, name = package.split("/")
            self.assertIn(f"ls -d /var/db/pkg/{category}/{name}-[0-9]*", program)

    def test_services_are_enabled_not_started(self):
        program = apply("plasma-services")
        self.assertIn("CHECKVT=7", program)
        self.assertIn("'DISPLAYMANAGER=\"sddm\"'", program)
        self.assertIn("rc-update show boot | grep -Eq '(^| )elogind ' || rc-update add elogind boot", program)
        self.assertIn("rc-update show default | grep -Eq '(^| )display-manager ' || rc-update add display-manager default",
                      program)
        self.assertNotIn("default || true", program)
        self.assertNotIn("boot || true", program)
        self.assertNotIn("rc-service", program)
        self.assertIn("rc-update show default | grep -Eq '(^| )display-manager '", check("plasma-services"))

    def test_preflight_requires_a_vfat_boot_like_the_nvidia_one(self):
        for provision in (AMD, NVIDIA):
            program = apply("preflight", provision)
            self.assertIn('test "$(findmnt -rn -o FSTYPE /boot)" = vfat', program)
            self.assertIn("preflight: /boot is not a mounted vfat filesystem", program)

    def test_verify_checks_nvidia_modules_for_the_newest_kernel_only_with_nvidia(self):
        needle = "find \"/lib/modules/$newest\" -name 'nvidia*.ko*' | grep -q ."
        self.assertNotIn("/lib/modules", check("plasma-verify", AMD))
        program = check("plasma-verify", NVIDIA)
        self.assertIn("newest=\"$(ls -1v /lib/modules | tail -n 1)\"", program)
        self.assertIn(needle, program)
        self.assertIn("no NVIDIA kernel module for the newest installed kernel", program)

    def test_verify_and_online(self):
        self.assertIn("exit 1", apply("plasma-verify"))
        program = check("plasma-verify")
        self.assertIn("test -f /usr/share/wayland-sessions/plasma.desktop", program)
        self.assertIn("rc-update show boot", program)
        self.assertNotIn("xsessions", program)
        online = check("desktop-online")
        self.assertIn("rc-service display-manager status | grep -q started", online)
        self.assertIn("pgrep -x sddm >/dev/null", online)
        self.assertTrue(online.endswith("printf 'desktop-online-ok\\n'"))
        self.assertIn("exit 1", apply("desktop-online"))


class ValidationTests(unittest.TestCase):
    def test_hostile_values_never_reach_a_program(self):
        hostile = {
            "package_use": {**AMD, "package_use": ["x; rm -rf /"]},
            "licenses": {**AMD, "accept_licenses": ["NVIDIA-r2"]},
            "video card": {**AMD, "video_cards": ["amdgpu; reboot"]},
            "no cards": {**AMD, "video_cards": []},
            "profile name": {**AMD, "plasma_profile": "x; reboot"},
            "nvidia flag": {**AMD, "nvidia": "yes"},
            "profile id": {**AMD, "profile": "nvidia-driver"},
        }
        for label, provision in hostile.items():
            for function in (dp.command_for, dp.check_command_for):
                with self.subTest(case=label, function=function.__name__):
                    with self.assertRaises(AgentError) as caught:
                        function("plasma-config", TARGET, provision)
                    self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_package_use_changes_the_programs(self):
        other = {**AMD, "package_use": ["media-libs/a Y"]}
        self.assertNotEqual(apply("plasma-config"), apply("plasma-config", other))
        self.assertNotEqual(check("plasma-config"), check("plasma-config", other))


class StoredPlanTests(unittest.TestCase):
    def plan(self, provision=AMD):
        profile = get_profile("desktop-plasma")
        return {"plan_id": "p", "target": TARGET, "provision": provision, "phases": [
            {"id": phase, "destructive": profile.flags[phase][0], "run": profile.flags[phase][1],
             "apply": profile.command_for(phase, TARGET, provision),
             "check": profile.check_command_for(phase, TARGET, provision)} for phase in profile.phases]}

    def test_the_generated_plan_verifies(self):
        verify_provision_programs(self.plan())

    def test_an_edited_stored_program_is_rejected(self):
        for phase in ("plasma-config", "plasma-services"):
            for kind in ("apply", "check"):
                plan = self.plan()
                stored = next(p for p in plan["phases"] if p["id"] == phase)
                stored[kind] += "\ntrue"
                with self.subTest(phase=phase, kind=kind):
                    with self.assertRaises(AgentError) as caught:
                        verify_provision_programs(plan)
                    self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_changed_settings_with_old_programs_are_rejected(self):
        plan = self.plan()
        plan["provision"] = {**AMD, "package_use": ["media-libs/a Y"]}
        with self.assertRaises(AgentError):
            verify_provision_programs(plan)


class SyntaxTests(unittest.TestCase):
    def test_every_program_passes_a_shell_syntax_check(self):
        with tempfile.TemporaryDirectory() as directory:
            for label, provision in (("amd", AMD), ("nvidia", NVIDIA)):
                for phase in dp.PHASES:
                    for kind, program in (("apply", apply(phase, provision)), ("check", check(phase, provision))):
                        path = Path(directory) / f"{label}-{phase}-{kind}.sh"
                        path.write_text(program + "\n", encoding="utf-8")
                        with self.subTest(label=label, phase=phase, kind=kind):
                            result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(result.stderr, "")


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def run_sh(self, program):
        return subprocess.run(["sh", "-c", program], capture_output=True, text=True)

    def test_the_display_manager_block_is_written_replaced_and_checked(self):
        from gentoo_agent.core.provision_phases import _block_apply, _block_check
        file = self.dir / "display-manager"
        file.write_text('X=1\n# gentoo-agent display-manager begin\nOLD=1\n# gentoo-agent display-manager end\n')
        lines = dp._DM_LINES
        self.assertNotEqual(self.run_sh("set -eu\n" + "\n".join(
            _block_check(str(file), dp.DM_BEGIN, dp.DM_END, lines))).returncode, 0)
        for _ in range(2):
            result = self.run_sh("set -eu\n" + "\n".join(_block_apply(str(file), dp.DM_BEGIN, dp.DM_END, lines)))
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(file.read_text(), 'X=1\n# gentoo-agent display-manager begin\nCHECKVT=7\n'
                                           'DISPLAYMANAGER="sddm"\n# gentoo-agent display-manager end\n')
        self.assertEqual(self.run_sh("set -eu\n" + "\n".join(
            _block_check(str(file), dp.DM_BEGIN, dp.DM_END, lines))).returncode, 0)

    def test_the_update_check_with_a_fake_emerge(self):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        emerge = bin_dir / "emerge"
        program = check("plasma-update")

        def run(script):
            emerge.write_text("#!/bin/sh\n" + script)
            emerge.chmod(0o755)
            return subprocess.run(["sh", "-c", program], capture_output=True, text=True,
                                  env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})

        self.assertEqual(run("exit 0\n").returncode, 0)
        pending = run("echo '[ebuild     U ] kde-plasma/plasma-meta-6.7.5'\n")
        self.assertEqual(pending.returncode, 1)
        self.assertIn("still to be updated", pending.stderr)
        self.assertEqual(run("exit 1\n").returncode, 1)
