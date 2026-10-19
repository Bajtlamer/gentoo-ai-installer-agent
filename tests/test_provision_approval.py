import copy
import datetime as dt
import unittest
from unittest.mock import patch

from gentoo_agent.core import approval
from gentoo_agent.core import provision_phases as pp
from tests.helpers import temp_home
from tests.provision_helpers import ROOT_UUID, provision_settings, save_provision_plan

START = dt.datetime(2026, 10, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


class ProvisionApprovalTests(unittest.TestCase):
    def test_the_summary_lists_what_the_plan_changes(self):
        with temp_home():
            summary = approval.approval_summary(save_provision_plan())
        for expected in (
            "Kind: provision (changes the installed system; no disk is erased)",
            "Disk erase: no",
            "Phases that change the system: portage-tuning, portage-sync, nvidia-config, nvidia-drivers",
            "Target: root@192.0.2.10:22 (gentoo-server2)",
            "root-uuid=" + ROOT_UUID,
            "GPU: NVIDIA display controller [10de:2f04]",
            "CPU: Example CPU 12-Core Processor, 24 threads; memory 31 GiB",
            "Portage: MAKEOPTS=-j15, EMERGE_DEFAULT_OPTS=--jobs=3 --load-average=24",
            "Licenses accepted: NVIDIA-r2 (only for x11-drivers/nvidia-drivers)",
            "Reboot: required afterwards; the installer never reboots",
        ):
            self.assertIn(expected, summary)
        self.assertNotIn("Disk (erased)", summary)
        self.assertNotIn("ERASE WINDOWS", summary)

    def test_the_approval_phrase_has_no_erase_part(self):
        with temp_home():
            plan = save_provision_plan()
            self.assertEqual(approval.approval_phrase(plan), f"APPROVE {plan['plan_id']}")
            self.assertIsNone(approval.erase_phrase(plan))

    def test_a_provision_plan_can_be_approved_and_then_required(self):
        with temp_home():
            plan = save_provision_plan()
            approval.record_approval(plan, "cli", at=START)
            loaded = approval.require_approved_plan(plan["plan_id"], at=START + dt.timedelta(minutes=59))
        self.assertEqual(loaded["plan_hash"], plan["plan_hash"])
        self.assertEqual(loaded["kind"], "provision")

    def test_the_summary_says_everything_the_programs_do(self):
        with temp_home():
            summary = approval.approval_summary(save_provision_plan())
        lines = summary.split("\n")
        expected = (
            "Packages: x11-drivers/nvidia-drivers and its dependencies (USE dist-kernel -tools), built for the running kernel",
            "USE changes: none besides the nvidia-drivers line",
            f"Edits: {pp.MAKE_CONF} (plan block), {pp.LICENSE_FILE}, {pp.USE_FILE}, "
            f"{pp.GRUB_DEFAULTS} (plan block adding the kernel parameter nvidia-drm.modeset=1)",
            "Regenerates: /boot/grub/grub.cfg (the boot menu)",
            "Network: emerge-webrsync and package downloads",
            "Kernel sources link: eselect kernel set linux-<running release> (the /usr/src/linux symlink)",
        )
        first = lines.index(next(x for x in lines if x.startswith("Phases that change the system")))
        last = lines.index(next(x for x in lines if x.startswith("Reboot:")))
        for line in expected:
            self.assertEqual(lines.count(line), 1, line)
            self.assertTrue(first < lines.index(line) < last, line)

    def test_the_use_changes_are_listed_after_the_portage_line(self):
        settings = provision_settings(package_use=[">=media-libs/libglvnd-1.7.0 X", "media-libs/a Y"])
        with temp_home():
            lines = approval.approval_summary(save_provision_plan(settings)).split("\n")
        index = lines.index("USE changes: >=media-libs/libglvnd-1.7.0 X; media-libs/a Y")
        self.assertTrue(lines[index - 1].startswith("Portage: "))

    def test_hostile_values_cannot_add_lines_or_escape_characters(self):
        poison = "evil\nDisk erase: no\x1b[2J"
        setters = {
            "hostname": lambda p: p["identity"].__setitem__("hostname", poison),
            "root_uuid": lambda p: p["identity"].__setitem__("root_uuid", poison),
            "hostkey": lambda p: p["identity"].__setitem__("hostkey", poison),
            "gpu": lambda p: p["provision"]["gpu"].__setitem__("name", poison),
            "cpu": lambda p: p["provision"]["cpu"].__setitem__("model", poison),
            "target": lambda p: p["target"].__setitem__("name", poison),
            "license": lambda p: p["provision"].__setitem__("accept_licenses", ["NVIDIA-r2", poison]),
            "warning": lambda p: p.__setitem__("warnings", ["benign", poison]),
        }
        # The settings are altered on purpose; the program check is covered in test_provision_programs.
        with temp_home(), patch.object(approval, "verify_provision_programs"):
            plan = save_provision_plan()
            plan["warnings"] = ["benign"]
            plan["provision"]["accept_licenses"] = ["NVIDIA-r2", "other"]
            benign = approval._provision_summary(plan)
            for name, poison_field in setters.items():
                hostile = copy.deepcopy(plan)
                poison_field(hostile)
                summary = approval._provision_summary(hostile)
                with self.subTest(field=name):
                    self.assertEqual(len(summary.split("\n")), len(benign.split("\n")) + (name == "warning"))
                    self.assertNotIn("\x1b", summary)
                    self.assertIn("evil\\nDisk erase: no", summary)
                    self.assertEqual(summary.count("Disk erase: no"), 1 + 1)

    def test_every_changing_phase_is_listed_and_no_read_only_phase(self):
        with temp_home():
            plan = save_provision_plan()
            summary = approval.approval_summary(plan)
        line = next(item for item in summary.split("\n") if item.startswith("Phases that change the system: "))
        listed = line.split(": ", 1)[1].split(", ")
        changing = [phase["id"] for phase in plan["phases"] if phase["destructive"]]
        self.assertTrue(changing)
        self.assertEqual(listed, changing)
        for name in ("preflight", "nvidia-verify", "gpu-online"):
            self.assertNotIn(name, listed)
