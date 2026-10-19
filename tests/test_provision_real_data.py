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
