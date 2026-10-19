import re
import unittest
from unittest.mock import patch

from gentoo_agent.core import inventory, ssh
from gentoo_agent.core.errors import HOST_KEY_UNKNOWN, SSH_UNREACHABLE, AgentError
from tests.helpers import temp_home
from tests.provision_helpers import PROVISION_TARGET


def fake(target, label, command):
    return {"label": label, "command": command, "returncode": 0, "stdout": f"{label}\n", "stderr": ""}


class ProvisionCommandsTests(unittest.TestCase):
    def test_the_allowlist_has_the_expected_labels(self):
        self.assertEqual(list(ssh.PROVISION_READ_ONLY_COMMANDS),
                         ["host_identity", "cpu", "memory", "gpu", "portage_conf", "kernel"])

    def test_no_command_changes_anything(self):
        forbidden = re.compile(r">|\b(rm|mv|cp|tee|dd|emerge|rc-service|rc-update|mkdir|touch|chmod|chown|"
                               r"reboot|shutdown|kill|modprobe|sed -i)\b")
        for label, command in ssh.PROVISION_READ_ONLY_COMMANDS.items():
            with self.subTest(label=label):
                self.assertIsNone(forbidden.search(command.replace("2>/dev/null", "")), command)

    def test_the_gpu_command_reads_sysfs_and_needs_no_lspci(self):
        command = ssh.PROVISION_READ_ONLY_COMMANDS["gpu"]
        self.assertIn("/sys/bus/pci/devices", command)
        self.assertNotIn("lspci", command)


class CollectProvisionInventoryTests(unittest.TestCase):
    def test_the_inventory_is_saved_apart_from_the_install_inventory(self):
        with temp_home(), patch.object(inventory, "run_ssh_readonly", side_effect=fake):
            data, path = inventory.collect_provision_inventory(PROVISION_TARGET)
            latest = inventory.latest_provision_inventory()
            install_latest = inventory.latest_inventory()
        self.assertEqual([item["label"] for item in data["results"]], list(ssh.PROVISION_READ_ONLY_COMMANDS))
        self.assertTrue(path.name.startswith("provision-inventory-"))
        self.assertEqual(latest, data)
        self.assertIsNone(install_latest)

    def test_install_inventory_collection_is_unchanged(self):
        with temp_home(), patch.object(inventory, "run_ssh_readonly", side_effect=fake):
            data, path = inventory.collect_inventory(PROVISION_TARGET)
        self.assertEqual([item["label"] for item in data["results"]], list(ssh.READ_ONLY_COMMANDS))
        self.assertTrue(path.name.startswith("inventory-"))

    def test_nothing_is_saved_when_the_target_cannot_be_reached(self):
        down = {"label": "x", "command": "", "returncode": 255, "stdout": "", "stderr": "ssh: connect failed"}
        with temp_home(), patch.object(inventory, "run_ssh_readonly", return_value=down):
            with self.assertRaises(AgentError) as caught:
                inventory.collect_provision_inventory(PROVISION_TARGET)
            self.assertIsNone(inventory.latest_provision_inventory())
        self.assertEqual(caught.exception.code, SSH_UNREACHABLE)

    def test_a_changed_host_key_is_reported(self):
        refused = {"label": "x", "command": "", "returncode": 255, "stdout": "",
                   "stderr": "Host key verification failed.\n"}
        with temp_home(), patch.object(inventory, "run_ssh_readonly", return_value=refused):
            with self.assertRaises(AgentError) as caught:
                inventory.collect_provision_inventory(PROVISION_TARGET)
        self.assertEqual(caught.exception.code, HOST_KEY_UNKNOWN)
