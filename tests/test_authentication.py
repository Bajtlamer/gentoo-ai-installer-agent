import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import install_phases  # noqa: E402


class AuthenticationConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.target = {
            "user": "root",
            "identity_file": "~/.ssh/id_test",
        }
        self.install = {
            "disk": "/dev/sda",
            "partition_layout": [
                {"device": "/dev/sda1", "type": "linux-swap", "mount": None},
                {"device": "/dev/sda2", "type": "linux-root", "mount": "/"},
            ],
            "boot_mode": "bios",
            "partition_table": "mbr",
            "root_filesystem": "ext4",
            "hostname": "gentoo",
            "timezone": "Europe/Prague",
            "create_user": True,
            "username": "gentoo",
            "install_user_ssh_key": True,
            "user_groups": ["wheel"],
            "configure_sudo": True,
            "user_shell": "/bin/bash",
            "set_root_password": True,
            "allow_root_password_ssh": True,
        }
        self.public_key_patch = patch(
            "install_phases.public_key_for_target",
            return_value="ssh-ed25519 AAAATEST gentoo-agent-test",
        )
        self.public_key_patch.start()

    def tearDown(self):
        self.public_key_patch.stop()

    def test_configuration_uses_valid_gentoo_openssh_atom(self):
        configured = dict(self.install)
        configured["allow_root_password_ssh"] = False
        command = install_phases.configuration_command(self.target, configured)
        self.assertIn("emerge --noreplace --verbose net-misc/openssh", command)
        self.assertNotIn("app-admin/openssh", command)
        self.assertIn("PermitRootLogin prohibit-password", command)
        self.assertIn("PasswordAuthentication yes", command)
        self.assertIn("PermitRootLogin prohibit-password", command)

    def test_configuration_check_verifies_ssh_contract(self):
        command = install_phases.configuration_check_command(self.target, self.install)
        for expected in (
            "/usr/sbin/sshd",
            "/root/.ssh/authorized_keys",
            "/etc/runlevels/default/sshd",
            "/etc/runlevels/default/dhcpcd",
            "PasswordAuthentication yes",
            "PermitRootLogin yes",
        ):
            self.assertIn(expected, command)

    def test_authentication_command_does_not_contain_passwords(self):
        command = install_phases.authentication_command(self.target, self.install)
        self.assertIn("useradd", command)
        self.assertIn("chpasswd", command)
        self.assertIn("authorized_keys", command)
        self.assertIn("chown gentoo:gentoo", command)
        self.assertIn("app-admin/sudo", command)
        self.assertIn("%wheel ALL=(ALL:ALL) ALL", command)
        self.assertIn("visudo -cf /etc/sudoers", command)
        self.assertNotIn("password", command.lower())

    def test_authentication_check_verifies_sudo_access(self):
        command = install_phases.authentication_check_command(self.target, self.install)
        self.assertIn("/usr/bin/sudo", command)
        self.assertIn("%wheel ALL=(ALL:ALL) ALL", command)
        self.assertIn("/etc/sudoers", command)

    def test_sudo_can_be_disabled_explicitly(self):
        configured = dict(self.install)
        configured["configure_sudo"] = False
        command = install_phases.authentication_command(self.target, configured)
        check = install_phases.authentication_check_command(self.target, configured)
        self.assertNotIn("app-admin/sudo", command)
        self.assertNotIn("/usr/bin/sudo", check)

    def test_password_input_is_only_records_for_chpasswd(self):
        # The function is deliberately tested without calling getpass. The
        # command itself contains no credential values; execute() supplies
        # them through SSH stdin at runtime.
        self.assertNotIn("root-secret", install_phases.authentication_command(self.target, self.install))
        self.assertNotIn("user-secret", install_phases.authentication_command(self.target, self.install))

    def test_environment_password_source_reads_configured_names(self):
        configured = dict(self.install)
        configured.update({
            "password_source": "environment",
            "root_password_env": "TEST_GENTOO_ROOT_PASSWORD",
            "user_password_env": "TEST_GENTOO_USER_PASSWORD",
        })
        with patch.dict(
            install_phases.os.environ,
            {
                "TEST_GENTOO_ROOT_PASSWORD": "root-secret",
                "TEST_GENTOO_USER_PASSWORD": "user-secret",
            },
            clear=False,
        ):
            input_data, generated = install_phases.prompt_authentication_input(configured)
        self.assertEqual(input_data, "root:root-secret\ngentoo:user-secret\n")
        self.assertEqual(generated, {})

    def test_missing_environment_passwords_are_generated(self):
        configured = dict(self.install)
        configured.update({
            "password_source": "environment",
            "generate_missing_passwords": True,
            "root_password_env": "TEST_MISSING_ROOT_PASSWORD",
            "user_password_env": "TEST_MISSING_USER_PASSWORD",
        })
        with patch.dict(install_phases.os.environ, {}, clear=True), patch(
            "install_phases.secrets.token_urlsafe",
            side_effect=["generated-root", "generated-user"],
        ):
            input_data, generated = install_phases.prompt_authentication_input(configured)
        self.assertEqual(input_data, "root:generated-root\ngentoo:generated-user\n")
        self.assertEqual(generated, {"root": "generated-root", "gentoo": "generated-user"})

    def test_phase_order_authenticates_before_kernel(self):
        self.assertLess(
            install_phases.PHASES.index("authentication"),
            install_phases.PHASES.index("kernel"),
        )


if __name__ == "__main__":
    unittest.main()
