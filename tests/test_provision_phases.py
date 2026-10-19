import os
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
        self.assertNotIn("sed -i", program)
        self.assertIn("awk -v b='# gentoo-agent portage-tuning begin' -v e='# gentoo-agent portage-tuning end'", program)
        self.assertIn("cat \"$tmp\" > /etc/portage/make.conf", program)
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
        self.assertIn("'x11-drivers/nvidia-drivers NVIDIA-r2 Apache-2.0' > /etc/portage/package.license/gentoo-agent-nvidia", program)
        self.assertIn("'x11-drivers/nvidia-drivers kernel-open dist-kernel' > /etc/portage/package.use/gentoo-agent-nvidia", program)

    def test_the_grub_parameter_is_a_block_the_plan_owns(self):
        program = apply("nvidia-config")
        self.assertNotIn("sed -i", program)
        self.assertIn("awk -v b='# gentoo-agent nvidia begin' -v e='# gentoo-agent nvidia end'", program)
        self.assertIn("nvidia-drm.modeset=1", program)
        self.assertIn("/etc/default/grub", program)

    def test_the_kernel_sources_link_follows_the_running_kernel(self):
        self.assertIn('eselect kernel set "linux-$(uname -r)"', apply("nvidia-config"))
        self.assertIn('test "$(readlink -f /usr/src/linux)" = "/usr/src/linux-$(uname -r)"', check("nvidia-config"))

    def test_hostile_licenses_never_reach_a_program(self):
        hostile = {**PROVISION, "accept_licenses": ["X; reboot"]}
        with self.assertRaises(AgentError):
            phases.command_for("nvidia-config", TARGET, hostile)


BEGIN = "# test begin"
END = "# test end"
BLOCK = f"{BEGIN}\nA=1\nB=2\n{END}\n"


class BlockBehaviorTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        self.file = self.dir / "conf"

    def run_lines(self, lines):
        program = "set -eu\n" + "\n".join(lines)
        return subprocess.run(["sh", "-c", program], capture_output=True, text=True)

    def apply_block(self):
        return self.run_lines(phases._block_apply(str(self.file), BEGIN, END, ["A=1", "B=2"]))

    def check_block(self):
        return self.run_lines(phases._block_check(str(self.file), BEGIN, END, ["A=1", "B=2"]))

    def test_missing_file_is_created(self):
        result = self.apply_block()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.file.read_text(), BLOCK)

    def test_empty_file(self):
        self.file.write_text("")
        self.assertEqual(self.apply_block().returncode, 0)
        self.assertEqual(self.file.read_text(), BLOCK)

    def test_other_content_is_kept(self):
        self.file.write_text("X=1\nY=2\n")
        self.assertEqual(self.apply_block().returncode, 0)
        self.assertEqual(self.file.read_text(), "X=1\nY=2\n" + BLOCK)

    def test_last_line_without_newline_stays_intact(self):
        self.file.write_text("X=1\nY=2")
        self.assertEqual(self.apply_block().returncode, 0)
        self.assertEqual(self.file.read_text(), "X=1\nY=2\n" + BLOCK)

    def test_idempotent(self):
        self.file.write_text("X=1\n")
        self.apply_block()
        first = self.file.read_text()
        self.assertEqual(self.apply_block().returncode, 0)
        self.assertEqual(self.file.read_text(), first)
        self.assertEqual(first.count(BEGIN), 1)

    def test_old_block_is_replaced(self):
        self.file.write_text(f"X=1\n{BEGIN}\nOLD=1\n{END}\nZ=3\n")
        self.assertEqual(self.apply_block().returncode, 0)
        self.assertEqual(self.file.read_text(), "X=1\nZ=3\n" + BLOCK)

    def assert_refused(self, content):
        self.file.write_text(content)
        result = self.apply_block()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing", result.stderr)
        self.assertEqual(self.file.read_text(), content)

    def test_begin_without_end_is_refused(self):
        self.assert_refused(f"X=1\n{BEGIN}\nOLD=1\nZ=3\n")

    def test_end_without_begin_is_refused(self):
        self.assert_refused(f"X=1\nOLD=1\n{END}\nZ=3\n")

    def test_two_complete_blocks_are_refused(self):
        self.assert_refused(f"X=1\n{BLOCK}Y=2\n{BLOCK}")

    def test_end_before_begin_is_refused(self):
        content = f"X=1\n{END}\nOLD=1\n{BEGIN}\nZ=3\n"
        self.file.write_text(content)
        result = self.apply_block()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("before", result.stderr)
        self.assertEqual(self.file.read_text(), content)

    def test_one_block_in_order_with_user_lines_around_is_replaced(self):
        self.file.write_text(f"X=1\n{BEGIN}\nOLD=1\nOLD=2\n{END}\nZ=3\nW=4\n")
        self.assertEqual(self.apply_block().returncode, 0)
        self.assertEqual(self.file.read_text(), "X=1\nZ=3\nW=4\n" + BLOCK)

    def test_the_program_removes_its_temp_file_on_any_exit(self):
        lines = phases._block_apply(str(self.file), BEGIN, END, ["A=1"])
        mktemp = next(i for i, line in enumerate(lines) if line.startswith("tmp="))
        self.assertEqual(lines[mktemp + 1], "trap 'rm -f \"$tmp\"' EXIT")
        self.assertIn('rm -f "$tmp"', lines[-1])

    def test_no_stray_temp_files(self):
        self.file.write_text("X=1\n")
        self.apply_block()
        self.assertEqual(os.listdir(self.dir), ["conf"])

    def test_check_passes_after_apply_and_fails_without_block(self):
        self.file.write_text("X=1\n")
        self.assertNotEqual(self.check_block().returncode, 0)
        self.apply_block()
        self.assertEqual(self.check_block().returncode, 0)


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

    def test_the_license_of_the_loaded_module_is_only_checked_online(self):
        line = "modinfo -F license nvidia | grep -q GPL"
        self.assertIn(line, check("gpu-online"))
        for phase in phases.PHASES:
            for kind, program in (("apply", apply(phase)), ("check", check(phase))):
                if (phase, kind) != ("gpu-online", "check"):
                    self.assertNotIn(line, program, (phase, kind))


class PreflightPhaseTests(unittest.TestCase):
    def test_preflight_is_its_own_check(self):
        self.assertEqual(apply("preflight"), check("preflight"))
        program = check("preflight")
        for fragment in ('test "$(id -u)" -eq 0', "ip route | grep -q default", "command -v emerge",
                         'test -d "/usr/src/linux-$(uname -r)"', "8388608"):
            self.assertIn(fragment, program)
        self.assertTrue(program.endswith("printf 'preflight-ok\\n'"))

    def test_every_failing_test_says_why(self):
        lines = check("preflight").split("\n")[1:-1]
        reasons = ("must run as root", "no default route", "emerge not found",
                   "/boot is not a mounted vfat filesystem", "less than 8 GiB free on /",
                   "the kernel sources /usr/src/linux-<release> are missing")
        self.assertEqual(len(lines), len(reasons) + 1)
        for reason in reasons:
            matching = [line for line in lines if f"'preflight: {reason}'" in line]
            with self.subTest(reason=reason):
                self.assertEqual(len(matching), 1)
                self.assertTrue(matching[0].endswith(f"|| {{ printf '%s\\n' 'preflight: {reason}' >&2; exit 1; }}"),
                                matching[0])

    def test_the_running_kernel_must_be_a_distribution_kernel(self):
        self.assertIn('case "$(uname -r)" in *-gentoo-dist*) ;; *) printf \'%s\\n\' '
                      "'preflight: the running kernel is not a Gentoo distribution kernel' >&2; exit 1;; esac",
                      check("preflight"))


class SyncPhaseTests(unittest.TestCase):
    def test_sync_refreshes_the_tree_and_the_check_wants_a_recent_timestamp(self):
        self.assertIn("emerge-webrsync", apply("portage-sync"))
        self.assertIn("-name timestamp.chk -mtime -2", check("portage-sync"))
        self.assertNotIn("-mtime -1", check("portage-sync"))


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
