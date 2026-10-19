"""Provision parsers and planning against the real output of the first real server (Gentoo, OpenRC)."""

import unittest
from fnmatch import fnmatch

from gentoo_agent.core.errors import INVENTORY_MISSING, AgentError
from gentoo_agent.core.identity import parse_identity
from gentoo_agent.core.provision_facts import parse_cpu, parse_gpus, parse_memory
from gentoo_agent.core.provision_plan import create_provision_plan
from tests.helpers import temp_home
from tests.provision_helpers import (
    PROVISION_TARGET, REAL_BROKEN_IDENTITY_STDOUT, REAL_CPU_STDOUT, REAL_GPU_STDOUT, REAL_KERNEL_STDOUT,
    REAL_MEMORY_STDOUT, provision_inventory, provision_settings, result,
)


class RealDataTests(unittest.TestCase):
    def test_the_real_cpu(self):
        cpu = parse_cpu(REAL_CPU_STDOUT)
        self.assertEqual(cpu["threads"], 20)
        self.assertEqual(cpu["model"], "13th Gen Intel(R) Core(TM) i5-13500")

    def test_the_real_memory(self):
        self.assertEqual(parse_memory(REAL_MEMORY_STDOUT), 32598200)

    def test_the_real_gpu_list(self):
        gpus = parse_gpus(REAL_GPU_STDOUT)
        self.assertEqual(len(gpus), 2)
        self.assertEqual([g["vendor"] for g in gpus].count("nvidia"), 1)
        nvidia = next(g for g in gpus if g["vendor"] == "nvidia")
        self.assertEqual(nvidia["pci_id"], "10de:2c05")
        self.assertEqual([g["vendor"] for g in gpus if g["vendor"] != "nvidia"], ["other"])

    def test_the_real_kernel_passes_the_preflight_pattern(self):
        self.assertTrue(fnmatch(REAL_KERNEL_STDOUT.strip(), "*-gentoo-dist*"))

    def test_the_real_broken_identity_is_refused(self):
        with self.assertRaises(AgentError) as caught:
            parse_identity(REAL_BROKEN_IDENTITY_STDOUT)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_a_plan_from_the_real_inventory(self):
        inventory = provision_inventory(
            cpu=result("cpu", REAL_CPU_STDOUT), memory=result("memory", REAL_MEMORY_STDOUT),
            gpu=result("gpu", REAL_GPU_STDOUT), kernel=result("kernel", REAL_KERNEL_STDOUT))
        with temp_home():
            plan = create_provision_plan(PROVISION_TARGET, provision_settings(), inventory)
        self.assertEqual(plan["provision"]["gpu"]["pci_id"], "10de:2c05")
        self.assertEqual(plan["provision"]["tuning"], {"jobs": 15, "emerge_jobs": 2, "load_average": 20})

    def test_the_real_licenses_and_use_changes_reach_the_programs_and_the_summary(self):
        from gentoo_agent.core.approval import approval_summary
        from gentoo_agent.core.install_plan import save_install_plan
        inventory = provision_inventory(
            cpu=result("cpu", REAL_CPU_STDOUT), memory=result("memory", REAL_MEMORY_STDOUT),
            gpu=result("gpu", REAL_GPU_STDOUT), kernel=result("kernel", REAL_KERNEL_STDOUT))
        settings = provision_settings(accept_licenses=["NVIDIA-2025"], package_use=[">=media-libs/libglvnd-1.7.0 X"])
        with temp_home():
            plan = save_install_plan(create_provision_plan(PROVISION_TARGET, settings, inventory))
            summary = approval_summary(plan)
        apply = next(p["apply"] for p in plan["phases"] if p["id"] == "nvidia-config")
        self.assertIn("'x11-drivers/nvidia-drivers NVIDIA-2025' > /etc/portage/package.license/gentoo-agent-nvidia", apply)
        self.assertIn("'x11-drivers/nvidia-drivers dist-kernel -tools' '>=media-libs/libglvnd-1.7.0 X' "
                      "> /etc/portage/package.use/gentoo-agent-nvidia", apply)
        self.assertEqual(plan["provision"]["package_use"], [">=media-libs/libglvnd-1.7.0 X"])
        self.assertIn("USE changes: >=media-libs/libglvnd-1.7.0 X", summary)
