import unittest

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.hardware_profile import hardware_profile
from gentoo_agent.core.profiles import desktop_applicable, desktop_build, plasma_profile
from gentoo_agent.core.provision import validated_desktop
from gentoo_agent.core.provision_facts import summarize_provision_inventory
from tests.provision_helpers import (AMD_GPU_SYSFS, REAL_PORTAGE_PROFILES_STDOUT, arch_result, desktop_inventory,
                                     gpu_result, portage_profile_result, result)

TARGET = "default/linux/amd64/23.0/desktop/plasma"


def facts_of(**replace):
    return summarize_provision_inventory(desktop_inventory(**replace))


def hardware_of(**replace):
    return hardware_profile(facts_of(**replace))


class PlasmaProfileTests(unittest.TestCase):
    def test_real_data_gives_the_target(self):
        hardware = hardware_of()
        self.assertEqual(hardware["portage_profile"], "default/linux/amd64/23.0")
        self.assertEqual(hardware["binrepos"], {"uris": [
            "https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64"]})
        self.assertEqual(plasma_profile(hardware), (TARGET, None))
        self.assertIsNone(desktop_applicable(hardware))

    def test_already_on_plasma(self):
        hardware = hardware_of(portage_profile=portage_profile_result(TARGET))
        self.assertEqual(plasma_profile(hardware), (TARGET, None))
        self.assertIsNone(desktop_applicable(hardware))

    def test_variants_refuse(self):
        for name in ("default/linux/amd64/23.0/systemd", "default/linux/amd64/23.0/hardened",
                     "default/linux/amd64/23.0/split-usr", "default/linux/amd64/23.0/desktop/plasma/systemd"):
            target, reason = plasma_profile(hardware_of(portage_profile=portage_profile_result(name)))
            self.assertIsNone(target, name)
            self.assertIn("Only the plain OpenRC profile", reason)
            self.assertIn(name, reason)

    def test_an_overlay_profile_keeps_the_inventory_usable_but_is_refused_by_name(self):
        name = "guru:default/linux/amd64/23.0/desktop/plasma"
        hardware = hardware_of(portage_profile=portage_profile_result(name))
        self.assertEqual(hardware["portage_profile"], name)
        target, reason = plasma_profile(hardware)
        self.assertIsNone(target)
        self.assertIn("Only the plain OpenRC profile", reason)
        self.assertIn(name, reason)

    def test_arm64_refuses(self):
        target, reason = plasma_profile(hardware_of(arch=arch_result("aarch64")))
        self.assertIsNone(target)
        self.assertIn("amd64", reason)

    def test_missing_profile_facts_refuse(self):
        inventory = desktop_inventory()
        inventory["results"] = [r for r in inventory["results"] if r["label"] != "portage_profile"]
        hardware = hardware_profile(summarize_provision_inventory(inventory))
        self.assertIsNone(hardware["portage_profile"])
        target, reason = plasma_profile(hardware)
        self.assertIsNone(target)
        self.assertIn("refresh=true", reason)

    def test_plasma_entry_missing_or_duplicated(self):
        lines = REAL_PORTAGE_PROFILES_STDOUT.splitlines(keepends=True)
        plasma = next(line for line in lines if line.split()[1] == TARGET)
        missing = "".join(line for line in lines if line is not plasma)
        target, reason = plasma_profile(hardware_of(portage_profiles=result("portage_profiles", missing)))
        self.assertIsNone(target)
        self.assertIn("is not offered", reason)
        twice = REAL_PORTAGE_PROFILES_STDOUT + plasma.replace("[3]", "[9]")
        target, reason = plasma_profile(hardware_of(portage_profiles=result("portage_profiles", twice)))
        self.assertIsNone(target)
        self.assertIn("more than once", reason)

    def test_amd_only(self):
        hardware = hardware_of(gpu=gpu_result(AMD_GPU_SYSFS))
        self.assertIsNone(desktop_applicable(hardware))
        self.assertEqual(hardware["video_cards"], ["amdgpu", "radeonsi"])
        built = desktop_build({"profile": "desktop-plasma", "package_use": []},
                              facts_of(gpu=gpu_result(AMD_GPU_SYSFS)), hardware)
        self.assertFalse(built["nvidia"])

    def test_no_gpu_and_unknown_vendor_refuse(self):
        self.assertIn("No display device", desktop_applicable(hardware_of(gpu=gpu_result("0000:00:00.0 0x060000 0x8086 0x4640\n"))))
        self.assertIn("unknown vendor", desktop_applicable(hardware_of(gpu=gpu_result("0000:00:02.0 0x030000 0x1234 0x1111\n"))))


class ValidatedDesktopTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validated_desktop({"profile": "desktop-plasma", "extra": 1}),
                         {"profile": "desktop-plasma", "package_use": []})
        self.assertEqual(validated_desktop({"profile": "desktop-plasma", "accept_licenses": [],
                                            "package_use": ["kde-plasma/plasma-meta -sddm"]})["package_use"],
                         ["kde-plasma/plasma-meta -sddm"])

    def test_refusals(self):
        for settings in ({"profile": "nvidia-driver"}, {"profile": "desktop-plasma", "accept_licenses": ["NVIDIA-r2"]},
                         {"profile": "desktop-plasma", "package_use": ["x11-drivers/nvidia-drivers kernel-open"]},
                         "x"):
            with self.assertRaises(AgentError) as caught:
                validated_desktop(settings)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)
        with self.assertRaises(AgentError) as caught:
            validated_desktop({"profile": "desktop-plasma", "accept_licenses": ["NVIDIA-r2"]})
        self.assertEqual(caught.exception.details["field"], "accept_licenses")


class DesktopBuildTests(unittest.TestCase):
    def test_block(self):
        facts = facts_of()
        built = desktop_build({"profile": "desktop-plasma", "package_use": []}, facts, hardware_profile(facts))
        self.assertEqual(built["plasma_profile"], TARGET)
        self.assertEqual(built["portage_profile"], "default/linux/amd64/23.0")
        self.assertTrue(built["nvidia"])
        self.assertEqual(built["video_cards"], ["intel", "nvidia"])
        self.assertEqual(built["gpus"], facts["gpus"])
        self.assertEqual(built["cpu"], facts["cpu"])
        self.assertEqual(built["memory_gib"], facts["memory_gib"])
        self.assertNotIn("tuning", built)
        self.assertEqual(built["binrepos"]["uris"][0], "https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64")

    def test_empty_binrepos(self):
        facts = facts_of(binrepos=result("binrepos", ""))
        built = desktop_build({"profile": "desktop-plasma", "package_use": []}, facts, hardware_profile(facts))
        self.assertEqual(built["binrepos"], {"uris": []})


if __name__ == "__main__":
    unittest.main()
