import unittest

from gentoo_agent.core import arch
from gentoo_agent.core.errors import CONFIG_INVALID, INVENTORY_MISSING, AgentError
from tests.helpers import identity_result


def inventory(*results):
    return {"results": list(results)}


class ArchitectureTests(unittest.TestCase):
    def test_live_architecture_is_read_from_the_identity_probe(self):
        self.assertEqual(arch.live_architecture(inventory(identity_result("aarch64"))), "arm64")
        self.assertEqual(arch.live_architecture(inventory(identity_result("x86_64"))), "amd64")

    def test_missing_or_failed_probe_is_inventory_missing(self):
        failed = {**identity_result("x86_64"), "returncode": 1}
        no_line = {"label": "identity", "returncode": 0, "stdout": "hostname=livecd\n"}
        for data in (inventory(), inventory(failed), inventory(no_line)):
            with self.subTest(data=data):
                with self.assertRaises(AgentError) as caught:
                    arch.live_architecture(data)
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_unsupported_machine_is_refused(self):
        with self.assertRaises(AgentError) as caught:
            arch.live_architecture(inventory(identity_result("riscv64")))
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(caught.exception.details, {"machine": "riscv64"})

    def test_spec_rejects_unknown_names(self):
        for name in ("x86", "", None, "AMD64"):
            with self.subTest(name=name):
                with self.assertRaises(AgentError) as caught:
                    arch.architecture_spec(name)
                self.assertEqual(caught.exception.details, {"field": "architecture"})

    def test_every_architecture_has_the_fields_the_generators_use(self):
        for name, spec in arch.ARCHITECTURES.items():
            with self.subTest(name=name):
                self.assertEqual(set(spec), {"machine", "grub_efi_target", "grub_efi_binary", "grub_efi_fallback", "bios"})
        self.assertFalse(arch.ARCHITECTURES["arm64"]["bios"])
