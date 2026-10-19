import subprocess
import unittest
from unittest.mock import patch

from gentoo_agent.core import identity
from gentoo_agent.core.errors import HOST_KEY_UNKNOWN, INVENTORY_MISSING, SSH_UNREACHABLE, TARGET_DRIFT, AgentError
from tests.provision_helpers import HOSTKEY, ROOT_UUID, PROVISION_TARGET, host_identity_result

EXPECTED = {"hostname": "gentoo", "root_uuid": ROOT_UUID, "hostkey": HOSTKEY}


class ParseIdentityTests(unittest.TestCase):
    def test_a_complete_result_is_parsed(self):
        self.assertEqual(identity.parse_identity(host_identity_result()["stdout"]), EXPECTED)

    def test_anything_else_is_unusable(self):
        good = host_identity_result()["stdout"]
        cases = {
            "not text": None,
            "empty": "",
            "hostname with shell text": good.replace("hostname=gentoo", "hostname=gentoo;reboot"),
            "hostname with a control sequence": good.replace("hostname=gentoo\n", "hostname=gentoo\x1b[2K\n"),
            "short root uuid": good.replace(ROOT_UUID, ROOT_UUID[:-1]),
            "root uuid with a missing group": good.replace(ROOT_UUID, ROOT_UUID.replace("-4e5f", "")),
            "uppercase root uuid": good.replace(ROOT_UUID, ROOT_UUID.upper()),
            "md5 fingerprint": good.replace(HOSTKEY, "MD5:aa:bb"),
            "no host key line": "\n".join(line for line in good.splitlines() if not line.startswith("hostkey=")),
        }
        for name, stdout in cases.items():
            with self.subTest(name), self.assertRaises(AgentError) as caught:
                identity.parse_identity(stdout)
            self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_a_missing_root_uuid_runs_into_the_host_key_line(self):
        stdout = "hostname=gentoo\nroot_uuid=hostkey=256 " + HOSTKEY + " root@gentoo (ED25519)\n"
        with self.assertRaises(AgentError) as caught:
            identity.parse_identity(stdout)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_crlf_and_trailing_whitespace_give_the_same_identity(self):
        clean = host_identity_result()["stdout"]
        messy = "".join(line + "  \t\r\n" for line in clean.splitlines())
        self.assertEqual(identity.parse_identity(messy), identity.parse_identity(clean))

    def test_the_key_type_word_is_not_compared(self):
        stdout = host_identity_result()["stdout"].replace("(ED25519)", "(RSA)")
        self.assertEqual(identity.parse_identity(stdout), EXPECTED)

    def test_a_second_field_that_is_not_a_sha256_fingerprint_is_refused(self):
        good = host_identity_result()["stdout"]
        for name, field in {"short": "SHA256:" + "A" * 42, "long": "SHA256:" + "A" * 44,
                            "bad characters": "SHA256:" + "A" * 42 + "!", "no prefix": "A" * 43}.items():
            with self.subTest(name), self.assertRaises(AgentError) as caught:
                identity.parse_identity(good.replace(HOSTKEY, field))
            self.assertEqual(caught.exception.code, INVENTORY_MISSING)


class CheckIdentityTests(unittest.TestCase):
    def check(self, *, result=None, error=None, expected=EXPECTED):
        with patch.object(identity, "run_ssh_readonly", return_value=result, side_effect=error):
            identity.check_identity(PROVISION_TARGET, expected)

    def test_the_same_host_passes(self):
        self.check(result=host_identity_result())

    def test_a_different_root_filesystem_is_drift(self):
        with self.assertRaises(AgentError) as caught:
            self.check(result=host_identity_result(root_uuid="ffffffff-ffff-4fff-8fff-ffffffffffff"))
        self.assertEqual(caught.exception.code, TARGET_DRIFT)
        self.assertEqual(caught.exception.details["expected"], EXPECTED)
        self.assertEqual(caught.exception.details["actual"]["root_uuid"], "ffffffff-ffff-4fff-8fff-ffffffffffff")

    def test_a_different_host_key_is_drift(self):
        with self.assertRaises(AgentError) as caught:
            self.check(result=host_identity_result(hostkey="SHA256:" + "B" * 43))
        self.assertEqual(caught.exception.code, TARGET_DRIFT)

    def test_a_timeout_or_launch_failure_is_unreachable(self):
        for error in (subprocess.TimeoutExpired("ssh", 45), FileNotFoundError("ssh")):
            with self.subTest(error=type(error).__name__), self.assertRaises(AgentError) as caught:
                self.check(error=error)
            self.assertEqual(caught.exception.code, SSH_UNREACHABLE)

    def test_a_host_key_failure_is_reported_as_such(self):
        refused = {"label": "host_identity", "command": "", "returncode": 255, "stdout": "",
                   "stderr": "Host key verification failed.\n"}
        with self.assertRaises(AgentError) as caught:
            self.check(result=refused)
        self.assertEqual(caught.exception.code, HOST_KEY_UNKNOWN)

    def test_a_failed_read_is_unreachable(self):
        failed = {"label": "host_identity", "command": "", "returncode": 1, "stdout": "", "stderr": "boom"}
        with self.assertRaises(AgentError) as caught:
            self.check(result=failed)
        self.assertEqual(caught.exception.code, SSH_UNREACHABLE)

    def test_a_different_hostname_alone_is_drift(self):
        with self.assertRaises(AgentError) as caught:
            self.check(result=host_identity_result(hostname="other"))
        self.assertEqual(caught.exception.code, TARGET_DRIFT)
        self.assertEqual(caught.exception.details["actual"]["hostname"], "other")

    def test_a_failed_read_carries_the_return_code(self):
        failed = {"label": "host_identity", "command": "", "returncode": 7, "stdout": "", "stderr": "boom"}
        with self.assertRaises(AgentError) as caught:
            self.check(result=failed)
        self.assertEqual(caught.exception.details["returncode"], 7)
