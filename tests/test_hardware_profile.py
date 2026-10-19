import unittest

from gentoo_agent.core import hardware_profile as hp
from gentoo_agent.core import provision_facts as facts
from gentoo_agent.core.errors import CONFIG_INVALID, INVENTORY_MISSING, AgentError
from tests.provision_helpers import (AMD_GPU_SYSFS, INTEL_GPU_SYSFS, arch_result, binrepos_result, gpu_result,
                                     portage_profile_result, portage_profiles_result, provision_inventory, result)


class NewCommandParserTests(unittest.TestCase):
    def assertUnusable(self, call, *args):
        with self.assertRaises(AgentError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_arch(self):
        self.assertEqual(facts.parse_arch("x86_64\n"), "amd64")
        self.assertEqual(facts.parse_arch("aarch64\n"), "arm64")
        self.assertUnusable(facts.parse_arch, "riscv64\n")
        self.assertUnusable(facts.parse_arch, None)

    def test_portage_profile(self):
        self.assertEqual(facts.parse_portage_profile(portage_profile_result()["stdout"]), "default/linux/amd64/23.0")
        self.assertUnusable(facts.parse_portage_profile, "Current /etc/portage/make.profile symlink:\n  default/linux amd64\n")
        self.assertUnusable(facts.parse_portage_profile, "default/linux/amd64/23.0\nrm -rf /")
        self.assertUnusable(facts.parse_portage_profile, "")

    def test_portage_profiles(self):
        profiles = facts.parse_portage_profiles(portage_profiles_result()["stdout"])
        self.assertEqual(len(profiles), 3)
        self.assertEqual(profiles[0], {"index": 1, "name": "default/linux/amd64/23.0", "stability": "stable",
                                       "current": True})
        self.assertFalse(profiles[2]["current"])
        self.assertEqual(profiles[2]["name"], "default/linux/amd64/23.0/desktop/plasma")

    def test_portage_profiles_rejections(self):
        self.assertUnusable(facts.parse_portage_profiles,
                            "Available profile symlink targets:\n  [1]   default/linux/amd64/23.0 *\n")
        self.assertUnusable(facts.parse_portage_profiles, "Available profile symlink targets:\n")
        self.assertUnusable(facts.parse_portage_profiles, "Available profile symlink targets:\n  junk\n")

    def test_binrepos(self):
        uri = "https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64/"
        self.assertEqual(facts.parse_binrepos(binrepos_result()["stdout"]), {"uris": [uri]})
        self.assertEqual(facts.parse_binrepos("# c\n\n[x]\npriority = 1\n"), {"uris": []})
        self.assertEqual(facts.parse_binrepos(""), {"uris": []})
        self.assertUnusable(facts.parse_binrepos, "sync-uri = http://example.org/x\n")

    def test_vendors(self):
        self.assertEqual(facts.parse_gpus(AMD_GPU_SYSFS)[0],
                         {"vendor": "amd", "pci_id": "1002:744c", "vendor_id": "1002",
                          "name": "AMD display controller"})
        self.assertEqual(facts.parse_gpus(INTEL_GPU_SYSFS)[0]["vendor"], "intel")


class SummarizeNewKeysTests(unittest.TestCase):
    def test_absent_labels_are_none(self):
        summary = facts.summarize_provision_inventory(provision_inventory())
        for key in ("architecture", "portage_profile", "portage_profiles", "binrepos"):
            self.assertIsNone(summary[key])

    def test_present_labels_are_parsed(self):
        inventory = provision_inventory(arch=arch_result(), portage_profile=portage_profile_result(),
                                        portage_profiles=portage_profiles_result(), binrepos=binrepos_result(),
                                        gpu=gpu_result(AMD_GPU_SYSFS))
        summary = facts.summarize_provision_inventory(inventory)
        self.assertEqual(summary["architecture"], "amd64")
        self.assertEqual(summary["portage_profile"], "default/linux/amd64/23.0")
        self.assertEqual(len(summary["portage_profiles"]), 3)
        self.assertEqual(len(summary["binrepos"]["uris"]), 1)

    def test_failed_label_is_unusable(self):
        with self.assertRaises(AgentError) as caught:
            facts.summarize_provision_inventory(provision_inventory(arch=result("arch", "x86_64\n", 1)))
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)


if __name__ == "__main__":
    unittest.main()


def _gpu(vendor, vendor_id, device="0001"):
    return {"vendor": vendor, "pci_id": f"{vendor_id}:{device}", "vendor_id": vendor_id,
            "name": f"{vendor} display controller"}


def _facts(gpus, threads=24):
    return {"architecture": "amd64", "cpu": {"threads": threads, "model": "x"}, "memory_gib": 62, "gpus": gpus}


class HardwareProfileTests(unittest.TestCase):
    def test_real_server(self):
        profile = hp.hardware_profile(_facts([_gpu("nvidia", "10de", "2c05")]))
        self.assertEqual(profile["video_cards"], ["nvidia"])
        self.assertEqual(profile["gpu_vendors"], ["nvidia"])
        self.assertEqual(profile["threads"], 24)
        self.assertEqual(profile["memory_gib"], 62)
        self.assertEqual(profile["architecture"], "amd64")
        self.assertEqual(profile["unknown_vendors"], [])
        self.assertFalse(profile["virtual"])

    def test_amd_intel_hybrid_and_none(self):
        self.assertEqual(hp.hardware_profile(_facts([_gpu("amd", "1002")]))["video_cards"], ["amdgpu", "radeonsi"])
        self.assertEqual(hp.hardware_profile(_facts([_gpu("intel", "8086")]))["video_cards"], ["intel"])
        hybrid = hp.hardware_profile(_facts([_gpu("amd", "1002"), _gpu("nvidia", "10de")]))
        self.assertEqual(hybrid["video_cards"], ["amdgpu", "nvidia", "radeonsi"])
        none = hp.hardware_profile(_facts([]))
        self.assertEqual(none["video_cards"], [])
        self.assertFalse(none["virtual"])

    def test_unknown_vendor(self):
        gpus = [_gpu("other", "10ec")]
        profile = hp.hardware_profile(_facts(gpus))
        self.assertIsNone(profile["video_cards"])
        self.assertEqual(profile["unknown_vendors"], ["10ec"])
        with self.assertRaises(AgentError) as caught:
            hp.video_cards(gpus)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_virtual(self):
        profile = hp.hardware_profile(_facts([_gpu("other", "1af4")]))
        self.assertTrue(profile["virtual"])
        self.assertEqual(profile["unknown_vendors"], ["1af4"])
