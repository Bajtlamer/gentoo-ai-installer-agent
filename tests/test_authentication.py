import unittest
from unittest.mock import patch


from gentoo_agent.core import phases as install_phases


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
            "gentoo_agent.core.phases.public_key_for_target",
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

    def test_password_records_reach_only_chpasswd(self):
        command = install_phases.authentication_command(self.target, self.install)
        lines = command.splitlines()
        capture = lines.index("records=$(cat)")
        first_chroot = next(index for index, line in enumerate(lines) if "chroot" in line)
        self.assertLess(capture, first_chroot)
        self.assertEqual(lines[capture + 1], "exec </dev/null")
        users = [line for line in lines if "records" in line]
        self.assertEqual(users, ["records=$(cat)", "printf '%s\\n' \"$records\" | chroot /mnt/gentoo chpasswd"])
        # chpasswd no longer runs inside the chroot program that does everything else.
        self.assertNotIn("chpasswd", lines[first_chroot])
        self.assertNotIn("set -x", command)
        self.assertNotIn("echo", command)
        self.assertEqual(lines[0], "set -eu")
        self.assertTrue(lines[-1].startswith("touch "))

    def test_no_password_records_without_chpasswd(self):
        configured = dict(self.install, create_user=False, set_root_password=False)
        command = install_phases.authentication_command(self.target, configured)
        self.assertNotIn("records", command)
        self.assertNotIn("chpasswd", command)

    def test_phase_order_authenticates_before_kernel(self):
        self.assertLess(
            install_phases.PHASES.index("authentication"),
            install_phases.PHASES.index("kernel"),
        )


if __name__ == "__main__":
    unittest.main()
