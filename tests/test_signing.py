import unittest

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


if __name__ == "__main__":
    unittest.main()
