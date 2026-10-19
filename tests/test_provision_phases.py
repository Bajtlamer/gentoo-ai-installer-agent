import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from gentoo_agent.core import provision_phases as phases
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError

TARGET = {"name": "gentoo-server2", "host": "192.0.2.10", "port": 22, "user": "root"}
PROVISION = {"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2", "Apache-2.0"],
             "tuning": {"jobs": 15, "emerge_jobs": 3, "load_average": 24}}
FORBIDDEN = re.compile(r"\b(wipefs|sfdisk|sgdisk|parted|mkfs\S*|mkswap|blkdiscard|dd|umount|swapoff|reboot|"
                       r"shutdown|poweroff|halt)\b")


def apply(phase):
    return phases.command_for(phase, TARGET, PROVISION)


def check(phase):
    return phases.check_command_for(phase, TARGET, PROVISION)


class PhaseListTests(unittest.TestCase):
    def test_order_and_flags(self):
        self.assertEqual(phases.PHASES, ("preflight", "portage-tuning", "portage-sync", "nvidia-config",
                                         "nvidia-drivers", "nvidia-verify", "gpu-online"))
        self.assertEqual(phases.PHASE_FLAGS["preflight"], (False, True))
        self.assertEqual(phases.PHASE_FLAGS["nvidia-drivers"], (True, True))
        self.assertEqual(phases.PHASE_FLAGS["gpu-online"], (False, False))
        self.assertEqual(set(phases.PHASE_FLAGS), set(phases.PHASES))

    def test_an_unknown_phase_is_invalid(self):
        for function in (phases.command_for, phases.check_command_for):
            with self.assertRaises(AgentError) as caught:
                function("storage", TARGET, PROVISION)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)


class SafetyTests(unittest.TestCase):
    def test_no_program_touches_a_disk_mounts_or_reboots(self):
        for phase in phases.PHASES:
            for kind, program in (("apply", apply(phase)), ("check", check(phase))):
                with self.subTest(phase=phase, kind=kind):
                    self.assertIsNone(FORBIDDEN.search(program), program)

    def test_every_program_starts_with_set_eu_and_ends_in_a_marker_or_exit(self):
        for phase in phases.PHASES:
            for program in (apply(phase), check(phase)):
                self.assertTrue(program.startswith("set -eu\n"), phase)


class TuningPhaseTests(unittest.TestCase):
    def test_the_block_is_replaced_not_appended_twice(self):
        program = apply("portage-tuning")
        self.assertIn("sed -i '/^# gentoo-agent portage-tuning begin$/,/^# gentoo-agent portage-tuning end$/d' /etc/portage/make.conf", program)
        self.assertIn("'MAKEOPTS=\"-j15\"'", program)
        self.assertIn("'EMERGE_DEFAULT_OPTS=\"--jobs=3 --load-average=24\"'", program)

    def test_the_check_asks_portage_for_the_effective_values(self):
        program = check("portage-tuning")
        self.assertIn("test \"$(portageq envvar MAKEOPTS)\" = -j15", program)
        self.assertIn("test \"$(portageq envvar EMERGE_DEFAULT_OPTS)\" = '--jobs=3 --load-average=24'", program)
        self.assertTrue(program.endswith("printf 'portage-tuning-ok\\n'"))


class NvidiaConfigPhaseTests(unittest.TestCase):
    def test_license_and_use_files_are_rewritten(self):
        program = apply("nvidia-config")
        self.assertIn("mkdir -p /etc/portage/package.license /etc/portage/package.use", program)
        self.assertIn("'*/* NVIDIA-r2 Apache-2.0' > /etc/portage/package.license/gentoo-agent-nvidia", program)
        self.assertIn("'x11-drivers/nvidia-drivers kernel-open dist-kernel' > /etc/portage/package.use/gentoo-agent-nvidia", program)

    def test_the_grub_parameter_is_a_block_the_plan_owns(self):
        program = apply("nvidia-config")
        self.assertIn("/^# gentoo-agent nvidia begin$/,/^# gentoo-agent nvidia end$/d", program)
        self.assertIn("nvidia-drm.modeset=1", program)
        self.assertIn("/etc/default/grub", program)

    def test_the_kernel_sources_link_follows_the_running_kernel(self):
        self.assertIn('eselect kernel set "linux-$(uname -r)"', apply("nvidia-config"))
        self.assertIn('test "$(readlink -f /usr/src/linux)" = "/usr/src/linux-$(uname -r)"', check("nvidia-config"))

    def test_hostile_licenses_never_reach_a_program(self):
        hostile = {**PROVISION, "accept_licenses": ["X; reboot"]}
        with self.assertRaises(AgentError):
            phases.command_for("nvidia-config", TARGET, hostile)


class DriverPhaseTests(unittest.TestCase):
    def test_apply_installs_the_package_and_rebuilds_the_grub_configuration(self):
        program = apply("nvidia-drivers")
        self.assertIn("emerge --noreplace --verbose x11-drivers/nvidia-drivers", program)
        self.assertLess(program.index("emerge"), program.index("grub-mkconfig -o /boot/grub/grub.cfg"))

    def test_check_needs_the_package_the_module_and_the_boot_parameter(self):
        program = check("nvidia-drivers")
        self.assertIn("ls -d /var/db/pkg/x11-drivers/nvidia-drivers-*", program)
        self.assertIn("-name 'nvidia*.ko*'", program)
        self.assertIn("grep -q 'nvidia-drm.modeset=1' /boot/grub/grub.cfg", program)


class VerifyPhasesTests(unittest.TestCase):
    def test_verify_checks_the_module_and_the_nouveau_blacklist(self):
        program = check("nvidia-verify")
        self.assertIn('modinfo -k "$(uname -r)" nvidia', program)
        self.assertIn("modprobe -c | grep -Eq '^blacklist nouveau$'", program)

    def test_a_verify_apply_only_reports_that_the_check_failed(self):
        self.assertIn("exit 1", apply("nvidia-verify"))

    def test_gpu_online_needs_the_driver_loaded_and_the_card_listed(self):
        program = check("gpu-online")
        self.assertIn("lsmod | grep -q '^nvidia '", program)
        self.assertIn("nvidia-smi -L | grep -q NVIDIA", program)
        self.assertIn("/sys/module/nvidia_drm/parameters/modeset", program)
        self.assertIn("/opt/bin", program)


class PreflightPhaseTests(unittest.TestCase):
    def test_preflight_is_its_own_check(self):
        self.assertEqual(apply("preflight"), check("preflight"))
        program = check("preflight")
        for fragment in ('test "$(id -u)" -eq 0', "ip route | grep -q default", "command -v emerge",
                         'test -d "/usr/src/linux-$(uname -r)"', "8388608"):
            self.assertIn(fragment, program)
        self.assertTrue(program.endswith("printf 'preflight-ok\\n'"))


class SyncPhaseTests(unittest.TestCase):
    def test_sync_refreshes_the_tree_and_the_check_wants_a_recent_timestamp(self):
        self.assertIn("emerge-webrsync", apply("portage-sync"))
        self.assertIn("-name timestamp.chk -mtime -1", check("portage-sync"))


class TuningValidationTests(unittest.TestCase):
    def test_bad_tuning_is_refused_before_any_text_is_produced(self):
        good = PROVISION["tuning"]
        bad_values = {
            "string value": {**good, "jobs": "15; reboot"},
            "zero": {**good, "emerge_jobs": 0},
            "missing key": {"jobs": 15, "emerge_jobs": 3},
        }
        for label, tuning in bad_values.items():
            provision = {**PROVISION, "tuning": tuning}
            for name, function in (("apply", phases.command_for), ("check", phases.check_command_for)):
                with self.subTest(case=label, kind=name):
                    with self.assertRaises(AgentError):
                        function("portage-tuning", TARGET, provision)


class SyntaxTests(unittest.TestCase):
    def test_every_program_passes_a_shell_syntax_check(self):
        with tempfile.TemporaryDirectory() as directory:
            for phase in phases.PHASES:
                for kind, program in (("apply", apply(phase)), ("check", check(phase))):
                    path = Path(directory) / f"{phase}-{kind}.sh"
                    path.write_text(program + "\n", encoding="utf-8")
                    with self.subTest(phase=phase, kind=kind):
                        result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, "")
