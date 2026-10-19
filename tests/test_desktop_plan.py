import unittest

from gentoo_agent.core.approval import approval_summary
from gentoo_agent.core.install_plan import save_install_plan
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.profiles import PROFILES
from gentoo_agent.core.provision_plan import create_provision_plan, verify_provision_programs
from tests.helpers import temp_home
from tests.provision_helpers import AMD_GPU_SYSFS, GPU_SYSFS, PROVISION_TARGET, desktop_inventory, gpu_result

SETTINGS = {"profile": "desktop-plasma", "package_use": ["media-libs/a Y"]}


def make(settings=None, inventory=None):
    return create_provision_plan(PROVISION_TARGET, settings or SETTINGS, inventory or desktop_inventory(gpu=gpu_result(GPU_SYSFS)))


class DesktopPlanTests(unittest.TestCase):
    def test_plan_holds_kind_identity_settings_and_verifiable_phases(self):
        plan = make()
        self.assertEqual(plan["kind"], "provision")
        self.assertIn("root_uuid", plan["identity"])
        provision = plan["provision"]
        self.assertEqual(provision["profile"], "desktop-plasma")
        self.assertEqual(provision["plasma_profile"], "default/linux/amd64/23.0/desktop/plasma")
        self.assertEqual(provision["video_cards"], ["nvidia"])
        self.assertTrue(provision["nvidia"])
        self.assertEqual([phase["id"] for phase in plan["phases"]], list(PROFILES["desktop-plasma"].phases))
        verify_provision_programs(plan)

    def test_licenses_are_refused(self):
        with self.assertRaises(AgentError) as caught:
            make({**SETTINGS, "accept_licenses": ["NVIDIA-r2"]})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_summary_lines(self):
        with temp_home():
            text = approval_summary(save_install_plan(make()))
        for line in (
            "Purpose: desktop-plasma (KDE Plasma, Wayland session offered, SDDM greeter on X11)",
            "VIDEO_CARDS: nvidia",
            "Portage profile: default/linux/amd64/23.0 -> default/linux/amd64/23.0/desktop/plasma (system-wide change)",
            "World update: emerge --getbinpkg --update --deep --newuse @world (system-wide, can take hours;",
            "Binary host: https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64",
            "Kernel: the world update may install a newer distribution kernel, rebuild the NVIDIA modules and "
            "regenerate the boot menu; the check verifies the NVIDIA modules exist for the newest installed kernel",
            "USE changes: x11-drivers/nvidia-drivers wayland; media-libs/a Y",
            "runlevels elogind (boot) and display-manager (default)",
            "Packages: kde-plasma/plasma-meta",
            "Requires: the NVIDIA driver already active",
            "Services are enabled, not started: the reboot starts them",
            "Reboot: required afterwards",
        ):
            self.assertIn(line, text)
        self.assertIn("GPU: nvidia ", text)
        self.assertNotIn("\u2014", text)

    def test_summary_has_the_network_line_like_the_nvidia_one(self):
        with temp_home():
            text = approval_summary(save_install_plan(make()))
        self.assertIn("Network: Portage repository and package downloads", text)

    def test_summary_without_nvidia_has_no_driver_requirement(self):
        amd = desktop_inventory(gpu=gpu_result(AMD_GPU_SYSFS))
        plan = make({"profile": "desktop-plasma"}, amd)
        with temp_home():
            text = approval_summary(save_install_plan(plan))
        self.assertNotIn("Requires:", text)
        self.assertIn("Kernel: the world update may install a newer distribution kernel and regenerate the boot menu", text)
        self.assertNotIn("NVIDIA modules", text)
        self.assertIn("USE changes: none", text)
        self.assertIn("VIDEO_CARDS: amdgpu radeonsi", text)


if __name__ == "__main__":
    unittest.main()
