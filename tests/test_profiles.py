import copy
import unittest

from gentoo_agent.core import provision_phases
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.hardware_profile import hardware_profile
from gentoo_agent.core.profiles import get_profile, profile_overview, provision_overview
from gentoo_agent.core.provision_facts import summarize_provision_inventory
from gentoo_agent.core.provision_plan import create_provision_plan, verify_provision_programs
from tests.helpers import temp_home
from tests.provision_helpers import (
    AMD_GPU_SYSFS, PROVISION_TARGET, gpu_result, provision_inventory, provision_settings, save_provision_plan,
)


class RegistryTests(unittest.TestCase):
    def test_get_profile(self):
        self.assertEqual(get_profile("nvidia-driver").id, "nvidia-driver")

    def test_unknown_profile_is_refused(self):
        for value in ("desktop-plasma", None, 5):
            with self.assertRaises(AgentError) as ctx:
                get_profile(value)
            self.assertEqual(ctx.exception.code, CONFIG_INVALID)
            self.assertEqual(ctx.exception.details["field"], "profile")

    def test_overview_for_the_nvidia_fixture(self):
        hardware = hardware_profile(summarize_provision_inventory(provision_inventory()))
        self.assertEqual([(row["id"], row["applicable"], row["reason"]) for row in profile_overview(hardware)],
                         [("nvidia-driver", True, None)])

    def test_overview_for_an_amd_only_inventory(self):
        inventory = provision_inventory(gpu=gpu_result(AMD_GPU_SYSFS))
        row = profile_overview(hardware_profile(summarize_provision_inventory(inventory)))[0]
        self.assertFalse(row["applicable"])
        self.assertEqual(row["reason"], "Expected exactly one NVIDIA GPU on the target, found 0.")

    def test_provision_overview_adds_hardware_and_profiles(self):
        data = provision_overview(provision_inventory())
        self.assertEqual(data["hardware"]["video_cards"], ["nvidia"])
        self.assertEqual([(row["id"], row["applicable"]) for row in data["profiles"]], [("nvidia-driver", True)])
        self.assertEqual(data["identity"], summarize_provision_inventory(provision_inventory())["identity"])


class DispatchTests(unittest.TestCase):
    def test_an_amd_only_inventory_creates_no_plan(self):
        with self.assertRaises(AgentError) as ctx:
            create_provision_plan(PROVISION_TARGET, provision_settings(),
                                  provision_inventory(gpu=gpu_result(AMD_GPU_SYSFS)))
        self.assertEqual(ctx.exception.code, CONFIG_INVALID)
        self.assertEqual(ctx.exception.message, "Expected exactly one NVIDIA GPU on the target, found 0.")

    def test_programs_equal_the_provision_phase_generators(self):
        plan = create_provision_plan(PROVISION_TARGET, provision_settings(), provision_inventory())
        self.assertEqual([phase["id"] for phase in plan["phases"]], list(provision_phases.PHASES))
        for phase in plan["phases"]:
            self.assertEqual(phase["apply"], provision_phases.command_for(phase["id"], PROVISION_TARGET, plan["provision"]))
            self.assertEqual(phase["check"], provision_phases.check_command_for(phase["id"], PROVISION_TARGET, plan["provision"]))
            self.assertEqual((phase["destructive"], phase["run"]), provision_phases.PHASE_FLAGS[phase["id"]])

    def test_a_plan_without_vendor_id_still_verifies(self):
        with temp_home():
            plan = copy.deepcopy(save_provision_plan())
        plan["provision"]["gpu"].pop("vendor_id", None)
        verify_provision_programs(plan)

    def test_verify_refuses_an_unknown_profile(self):
        plan = create_provision_plan(PROVISION_TARGET, provision_settings(), provision_inventory())
        plan["provision"]["profile"] = "other"
        with self.assertRaises(AgentError):
            verify_provision_programs(plan)


if __name__ == "__main__":
    unittest.main()
