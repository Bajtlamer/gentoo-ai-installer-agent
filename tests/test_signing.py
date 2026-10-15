import os
import unittest
from concurrent.futures import ThreadPoolExecutor

from gentoo_agent.core import approval, paths, signing
from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.errors import APPROVAL_REQUIRED, AgentError
from tests.helpers import temp_home
from tests.test_approval import START, make_plan


class SigningKeyTests(unittest.TestCase):
    def test_key_is_created_once_with_private_permissions(self):
        with temp_home():
            first = signing.load_or_create_key()
            second = signing.load_or_create_key()
            path = signing.key_path()
            self.assertEqual(first, second)
            self.assertEqual(len(first), 64)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.parent, paths.secrets_dir())

    def test_load_key_never_creates(self):
        with temp_home():
            self.assertIsNone(signing.load_key())
            self.assertFalse(signing.key_path().exists())

    def test_malformed_key_is_rejected(self):
        with temp_home():
            signing.key_path().parent.mkdir(mode=0o700, parents=True)
            signing.key_path().write_text("short", encoding="ascii")
            with self.assertRaises(AgentError) as caught:
                signing.load_key()
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_sign_and_verify(self):
        key = b"a" * 64
        fields = {"plan_id": "x", "n": 1}
        signature = signing.sign(fields, key)
        self.assertTrue(signing.verify(fields, signature, key))
        self.assertFalse(signing.verify({**fields, "n": 2}, signature, key))
        self.assertFalse(signing.verify(fields, signature, b"b" * 64))
        self.assertFalse(signing.verify(fields, None, key))


class SignedApprovalTests(unittest.TestCase):
    def test_recorded_approval_is_signed_and_accepted(self):
        with temp_home():
            plan = make_plan()
            record = approval.record_approval(plan, "cli", at=START)
            self.assertEqual(len(record["signature"]), 64)
            approval.require_approved_plan(plan["plan_id"], at=START)

    def test_tampered_record_is_rejected(self):
        with temp_home():
            plan = make_plan()
            approval.record_approval(plan, "cli", at=START)
            path = approval.approval_path(plan["plan_id"])
            record = load_json(path)
            record["channel"] = "elicitation"
            save_json(path, record)
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_forged_record_without_the_key_is_rejected(self):
        with temp_home():
            plan = make_plan()
            signing.load_or_create_key()
            unsigned = {
                "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"], "channel": "cli",
                "approved_at": START.isoformat(), "expires_at": "2026-10-15T13:00:00+00:00",
            }
            save_json(approval.approval_path(plan["plan_id"]),
                      {**unsigned, "signature": signing.sign(unsigned, b"f" * 64)})
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_missing_key_invalidates_existing_records(self):
        with temp_home():
            plan = make_plan()
            approval.record_approval(plan, "cli", at=START)
            signing.key_path().unlink()
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
            self.assertFalse(signing.key_path().exists())
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)


class HardeningTests(unittest.TestCase):
    def assert_key_refused(self):
        with self.assertRaises(AgentError) as caught:
            signing.load_key()
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_verify_rejects_non_hex_or_non_ascii_signatures(self):
        key = b"a" * 64
        fields = {"plan_id": "x"}
        self.assertFalse(signing.verify(fields, "é" * 64, key))
        self.assertFalse(signing.verify(fields, "A" * 64, key))
        self.assertFalse(signing.verify(fields, "a" * 63, key))

    def test_verify_rejects_unencodable_fields(self):
        self.assertFalse(signing.verify({"x": "\ud800"}, "a" * 64, b"a" * 64))

    def test_record_with_lone_surrogate_is_invalid_not_a_crash(self):
        with temp_home():
            plan = make_plan()
            approval.record_approval(plan, "cli", at=START)
            path = approval.approval_path(plan["plan_id"])
            text = path.read_text(encoding="utf-8").replace('"cli"', '"\\ud800"')
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
            self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)
            self.assertFalse(approval.approval_status(plan["plan_id"], at=START)["approved"])

    def test_key_path_that_is_a_directory_is_refused(self):
        with temp_home():
            signing.key_path().mkdir(parents=True)
            self.assert_key_refused()

    def test_group_readable_key_is_refused(self):
        with temp_home():
            signing.load_or_create_key()
            os.chmod(signing.key_path(), 0o644)
            self.assert_key_refused()

    def test_symlinked_key_is_refused(self):
        with temp_home():
            signing.load_or_create_key()
            real = signing.key_path().parent / "real.key"
            signing.key_path().rename(real)
            signing.key_path().symlink_to(real)
            self.assert_key_refused()

    @unittest.skipIf(hasattr(os, "getuid") and os.getuid() == 0, "root can read anything")
    def test_unreadable_key_is_refused(self):
        with temp_home():
            signing.load_or_create_key()
            os.chmod(signing.key_path(), 0o000)
            self.assert_key_refused()

    def test_secrets_path_that_is_a_file_is_refused(self):
        with temp_home():
            paths.secrets_dir().write_text("x", encoding="ascii")
            with self.assertRaises(AgentError) as caught:
                signing.load_or_create_key()
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_concurrent_creation_yields_one_key(self):
        with temp_home():
            with ThreadPoolExecutor(max_workers=8) as pool:
                keys = list(pool.map(lambda _: signing.load_or_create_key(), range(8)))
            self.assertEqual(len(set(keys)), 1)
            self.assertEqual(sorted(p.name for p in signing.key_path().parent.iterdir()), ["approval.key"])


if __name__ == "__main__":
    unittest.main()
