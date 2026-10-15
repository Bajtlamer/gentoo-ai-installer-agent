import unittest
from unittest.mock import patch

from gentoo_agent.core import credentials
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError


BASE = {
    "create_user": True,
    "username": "gentoo",
    "set_root_password": True,
    "root_password_env": "TEST_GENTOO_ROOT_PASSWORD",
    "user_password_env": "TEST_GENTOO_USER_PASSWORD",
}


class PasswordInputTests(unittest.TestCase):
    def test_environment_password_source_reads_configured_names(self):
        install = {**BASE, "password_source": "environment"}
        with patch.dict(
            credentials.os.environ,
            {"TEST_GENTOO_ROOT_PASSWORD": "root-secret", "TEST_GENTOO_USER_PASSWORD": "user-secret"},
            clear=False,
        ):
            records, generated = credentials.password_input(install)
        self.assertEqual(records, "root:root-secret\ngentoo:user-secret\n")
        self.assertEqual(generated, {})

    def test_missing_environment_passwords_are_generated(self):
        install = {**BASE, "password_source": "environment", "generate_missing_passwords": True}
        with patch.dict(credentials.os.environ, {}, clear=True), patch(
            "gentoo_agent.core.credentials.secrets.token_urlsafe",
            side_effect=["generated-root", "generated-user"],
        ):
            records, generated = credentials.password_input(install)
        self.assertEqual(records, "root:generated-root\ngentoo:generated-user\n")
        self.assertEqual(generated, {"root": "generated-root", "gentoo": "generated-user"})

    def test_missing_environment_password_without_generation_is_invalid(self):
        install = {**BASE, "password_source": "environment", "generate_missing_passwords": False}
        with patch.dict(credentials.os.environ, {}, clear=True):
            with self.assertRaises(AgentError) as caught:
                credentials.password_input(install)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(caught.exception.details, {"variable": "TEST_GENTOO_ROOT_PASSWORD"})

    def test_prompt_source_uses_the_callback(self):
        install = {**BASE, "password_source": "prompt"}
        asked = []

        def prompt(label):
            asked.append(label)
            return f"{label}-typed"

        records, generated = credentials.password_input(install, prompt)
        self.assertEqual(asked, ["root", "gentoo"])
        self.assertEqual(records, "root:root-typed\ngentoo:gentoo-typed\n")
        self.assertEqual(generated, {})

    def test_prompt_source_without_callback_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            credentials.password_input({**BASE, "password_source": "prompt"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_no_accounts_means_no_records(self):
        records, generated = credentials.password_input({"create_user": False, "set_root_password": False})
        self.assertIsNone(records)
        self.assertEqual(generated, {})

    def test_empty_environment_password_is_generated(self):
        install = {**BASE, "password_source": "environment", "generate_missing_passwords": True}
        with patch.dict(
            credentials.os.environ,
            {"TEST_GENTOO_ROOT_PASSWORD": "", "TEST_GENTOO_USER_PASSWORD": "user-secret"},
            clear=True,
        ), patch("gentoo_agent.core.credentials.secrets.token_urlsafe", return_value="generated-root"):
            records, generated = credentials.password_input(install)
        self.assertEqual(records, "root:generated-root\ngentoo:user-secret\n")
        self.assertEqual(generated, {"root": "generated-root"})


if __name__ == "__main__":
    unittest.main()
