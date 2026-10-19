import unittest

from gentoo_agent.core import provision_facts as facts
from gentoo_agent.core.errors import INVENTORY_MISSING, AgentError
from tests.provision_helpers import (
    GPU_SYSFS, cpu_result, kernel_result, memory_result, provision_inventory, result,
)


class ParseGpuTests(unittest.TestCase):
    def test_only_display_devices_are_listed(self):
        self.assertEqual(facts.parse_gpus(GPU_SYSFS), [
            {"vendor": "nvidia", "pci_id": "10de:2f04", "name": "NVIDIA display controller"},
        ])

    def test_other_vendors_and_3d_controllers(self):
        text = ("0000:03:00.0 0x030200 0x10de 0x1234\n"
                "0000:00:02.0 0x038000 0x8086 0x46a6\n")
        self.assertEqual([(gpu["vendor"], gpu["pci_id"], gpu["name"]) for gpu in facts.parse_gpus(text)],
                         [("nvidia", "10de:1234", "NVIDIA display controller"),
                          ("other", "8086:46a6", "Unknown vendor display controller")])

    def test_no_gpu_is_an_empty_list(self):
        self.assertEqual(facts.parse_gpus("0000:00:00.0 0x060000 0x1022 0x14d8\n"), [])
        self.assertEqual(facts.parse_gpus(""), [])

    def test_unusable_text(self):
        for value in (None, 5):
            with self.subTest(value=value), self.assertRaises(AgentError) as caught:
                facts.parse_gpus(value)
            self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_lines_that_do_not_match_the_format_are_ignored(self):
        text = ("garbage\n0000:01:00.0 0x030000 0x10DE 0x2f04\n0000:01:00.0 030000 10de 2f04\n"
                "0000:01:00.0 0x030000 0x10de 0x2f04; reboot\n")
        self.assertEqual(facts.parse_gpus(text), [])


class ParseCpuAndMemoryTests(unittest.TestCase):
    def test_cpu(self):
        self.assertEqual(facts.parse_cpu(cpu_result(24)["stdout"]),
                         {"threads": 24, "model": "Example CPU 12-Core Processor"})

    def test_cpu_without_a_model_name(self):
        self.assertEqual(facts.parse_cpu("threads=8\n"), {"threads": 8, "model": "unknown"})

    def test_cpu_without_threads_is_unusable(self):
        for value in (None, "", "threads=0\n", "threads=x\n", "Model name: A\n"):
            with self.subTest(value=value), self.assertRaises(AgentError):
                facts.parse_cpu(value)

    def test_a_newline_after_the_label_is_not_whitespace_to_skip(self):
        self.assertEqual(facts.parse_cpu("threads=8\nModel name:\n evil\n"), {"threads": 8, "model": "unknown"})
        with self.assertRaises(AgentError):
            facts.parse_memory("MemTotal:\n 5 kB\n")

    def test_hostile_cpu_model_names_are_refused(self):
        for name in ("Bad\x1b[2K", "x\u202ey", "A" * 121):
            with self.subTest(name=name), self.assertRaises(AgentError):
                facts.parse_cpu(f"threads=8\nModel name: {name}\n")

    def test_trailing_text_after_the_thread_count_is_refused(self):
        with self.assertRaises(AgentError):
            facts.parse_cpu("threads=24; reboot\n")

    def test_the_first_threads_line_wins(self):
        self.assertEqual(facts.parse_cpu("threads=24\nthreads=25\n")["threads"], 24)

    def test_trailing_text_after_the_memory_total_is_refused(self):
        with self.assertRaises(AgentError):
            facts.parse_memory("MemTotal: 5 kB; x\n")

    def test_memory_in_kib(self):
        self.assertEqual(facts.parse_memory(memory_result(32768000)["stdout"]), 32768000)

    def test_memory_without_a_total_is_unusable(self):
        for value in (None, "", "SwapTotal: 5 kB\n", "MemTotal: lots kB\n"):
            with self.subTest(value=value), self.assertRaises(AgentError):
                facts.parse_memory(value)


class MakeConfTests(unittest.TestCase):
    def test_variables_the_user_already_sets(self):
        text = 'MAKEOPTS="-j4"\nEMERGE_DEFAULT_OPTS="--quiet-build"\n'
        self.assertEqual(facts.make_conf_overrides(text), ["EMERGE_DEFAULT_OPTS", "MAKEOPTS"])

    def test_comments_and_values_do_not_count(self):
        self.assertEqual(facts.make_conf_overrides('#MAKEOPTS="-j2"\n'), [])
        self.assertEqual(facts.make_conf_overrides('FOO="MAKEOPTS=1"\n'), [])

    def test_nothing_set(self):
        self.assertEqual(facts.make_conf_overrides(""), [])
        self.assertEqual(facts.make_conf_overrides(None), [])


class SummaryTests(unittest.TestCase):
    def test_a_complete_inventory(self):
        summary = facts.summarize_provision_inventory(provision_inventory())
        self.assertEqual(summary["identity"]["hostname"], "gentoo")
        self.assertEqual(summary["cpu"]["threads"], 24)
        self.assertEqual(summary["memory_kib"], 32768000)
        self.assertEqual(summary["memory_gib"], 31)
        self.assertEqual(summary["kernel"], "6.18.57-gentoo-dist-bin")
        self.assertEqual(summary["gpus"][0]["pci_id"], "10de:2f04")
        self.assertEqual(summary["portage_overrides"], [])

    def test_a_missing_or_failed_required_result_is_refused(self):
        for label in ("host_identity", "cpu", "memory", "gpu", "kernel"):
            with self.subTest(label=label), self.assertRaises(AgentError) as caught:
                facts.summarize_provision_inventory(provision_inventory(**{label: result(label, "", 1)}))
            self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_an_absent_required_result_is_refused(self):
        for label in ("host_identity", "cpu", "memory", "gpu", "kernel"):
            inventory = provision_inventory()
            inventory["results"] = [item for item in inventory["results"] if item["label"] != label]
            with self.subTest(label=label), self.assertRaises(AgentError) as caught:
                facts.summarize_provision_inventory(inventory)
            self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_a_required_result_without_text_is_refused(self):
        for label in ("cpu", "memory", "gpu", "kernel"):
            for value in (None, 5):
                with self.subTest(label=label, value=value), self.assertRaises(AgentError) as caught:
                    facts.summarize_provision_inventory(provision_inventory(**{label: result(label, value)}))
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_a_failed_make_conf_read_only_means_no_overrides_are_known(self):
        summary = facts.summarize_provision_inventory(provision_inventory(portage_conf=result("portage_conf", "", 1)))
        self.assertEqual(summary["portage_overrides"], [])

    def test_a_hostile_kernel_release_is_unusable(self):
        with self.assertRaises(AgentError):
            facts.summarize_provision_inventory(provision_inventory(kernel=kernel_result("6.1; reboot")))
