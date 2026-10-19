import copy
import json
import unittest
from unittest.mock import patch

from gentoo_agent.core import approval, runs
from gentoo_agent.core.errors import PLAN_HASH_MISMATCH, AgentError
from gentoo_agent.core.install_plan import PLAN_ID_LENGTH, compute_plan_hash, plan_path
from gentoo_agent.core.provision_plan import verify_provision_programs
from gentoo_agent.jobs import worker
from tests.helpers import temp_home
from tests.provision_helpers import save_provision_plan
from tests.test_worker import WorkerTests


def crafted(plan, change):
    """A self-consistent copy (new hash and id) after change(copy) altered it, stored in the plan directory."""
    other = copy.deepcopy(plan)
    change(other)
    other["plan_hash"] = compute_plan_hash(other)
    other["plan_id"] = other["plan_hash"][:PLAN_ID_LENGTH]
    path = plan_path(other["plan_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(other), encoding="utf-8")
    return other


def change_apply(plan):
    plan["phases"][0]["apply"] += "\necho extra\n"


def change_flag(plan):
    phase = next(item for item in plan["phases"] if item["id"] == "gpu-online")
    phase["run"] = True


class ProvisionProgramTests(unittest.TestCase):
    def test_a_saved_plan_passes(self):
        with temp_home():
            verify_provision_programs(save_provision_plan())

    def test_a_changed_apply_text_is_refused(self):
        with temp_home():
            other = crafted(save_provision_plan(), change_apply)
            with self.assertRaises(AgentError) as caught:
                verify_provision_programs(other)
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)
        self.assertEqual(caught.exception.details["phase"], other["phases"][0]["id"])

    def test_a_changed_check_text_is_refused(self):
        def change(plan):
            plan["phases"][1]["check"] += "\ntrue\n"

        with temp_home():
            other = crafted(save_provision_plan(), change)
            with self.assertRaises(AgentError) as caught:
                verify_provision_programs(other)
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_a_changed_phase_flag_is_refused(self):
        with temp_home():
            other = crafted(save_provision_plan(), change_flag)
            with self.assertRaises(AgentError) as caught:
                verify_provision_programs(other)
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_a_changed_phase_order_is_refused(self):
        with temp_home():
            other = crafted(save_provision_plan(), lambda plan: plan["phases"].reverse())
            with self.assertRaises(AgentError) as caught:
                verify_provision_programs(other)
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_the_summary_refuses_a_mismatching_plan(self):
        with temp_home():
            other = crafted(save_provision_plan(), change_apply)
            with self.assertRaises(AgentError) as caught:
                approval.approval_summary(other)
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_the_worker_fails_the_run_before_any_command(self):
        helper = WorkerTests()
        with temp_home():
            other = crafted(save_provision_plan(), change_apply)
            approval.record_approval(other, "cli")
            run_id = helper.new_run(other)
            with patch.object(worker, "execute_plan") as execute:
                result = worker.run(run_id, {"password_records": None, "generated_credentials": {}})
            record = runs.read_run(run_id)
        execute.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], PLAN_HASH_MISMATCH)
        self.assertEqual(record["status"], "failed")


if __name__ == "__main__":
    unittest.main()
