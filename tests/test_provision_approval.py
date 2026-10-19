import copy
import datetime as dt
import unittest

from gentoo_agent.core import approval
from tests.helpers import temp_home
from tests.provision_helpers import MACHINE_ID, save_provision_plan

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
            "machine-id=" + MACHINE_ID,
            "GPU: NVIDIA display controller [10de:2f04]",
            "CPU: Example CPU 12-Core Processor, 24 threads; memory 31 GiB",
            "Portage: MAKEOPTS=-j15, EMERGE_DEFAULT_OPTS=--jobs=3 --load-average=24",
            "Packages: x11-drivers/nvidia-drivers (USE kernel-open dist-kernel)",
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

    def test_hostile_values_cannot_add_lines_or_escape_characters(self):
        with temp_home():
            plan = save_provision_plan()
            benign = approval._provision_summary(plan)
            hostile = copy.deepcopy(plan)
            hostile["identity"]["hostname"] = "evil\nDisk erase: no\x1b[2J"
            hostile["provision"]["cpu"]["model"] = "cpu\nPlan: fake\x1b[31m"
            summary = approval._provision_summary(hostile)
        self.assertEqual(len(summary.split("\n")), len(benign.split("\n")))
        self.assertNotIn("\x1b", summary)
        self.assertIn("evil\\nDisk erase: no", summary)

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

    def test_the_mcp_phrase_for_a_provision_plan_has_no_erase_part(self):
        with temp_home():
            plan = save_provision_plan()
            self.assertEqual(approval.approval_phrase(plan), "APPROVE " + plan["plan_id"])
