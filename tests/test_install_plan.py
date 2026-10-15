import json
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import install_plan, paths, phases
from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.errors import (
    CONFIG_INVALID,
    INVENTORY_MISSING,
    PLAN_HASH_MISMATCH,
    PLAN_NOT_FOUND,
    AgentError,
)
from tests.helpers import temp_home


REPO = Path(__file__).resolve().parents[1]
PUBLIC_KEY = "ssh-ed25519 AAAAPLAN gentoo-agent-plan-test"
TARGET = {
    "name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
    "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True,
}


def install_fixture() -> dict:
    data = json.loads((REPO / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))
    data.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True})
    return data


def project_plan() -> dict:
    return json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8"))


def inventory(serial: str = "QM00001") -> dict:
    stdout = json.dumps({"blockdevices": [
        {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK", "serial": serial, "wwn": None},
    ]})
    return {"results": [{"label": "disk_identity", "returncode": 0, "stdout": stdout}]}


class CreatePlanTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.object(phases, "public_key_for_target", return_value=PUBLIC_KEY)
        self.key.start()

    def tearDown(self):
        self.key.stop()

    def create(self, **changes):
        install = {**install_fixture(), **changes.pop("install", {})}
        return install_plan.create_install_plan(TARGET, install, project_plan(), changes.pop("inventory", inventory()))

    def test_plan_is_deterministic_and_id_is_hash_prefix(self):
        first, second = self.create(), self.create()
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(first["plan_id"], first["plan_hash"][:16])
        self.assertEqual(len(first["plan_hash"]), 64)

    def test_hash_changes_with_disk_serial(self):
        self.assertNotEqual(self.create()["plan_hash"], self.create(inventory=inventory("QM00002"))["plan_hash"])

    def test_hash_changes_with_schema_version(self):
        before = self.create()["plan_hash"]
        with patch.object(install_plan, "PLAN_SCHEMA_VERSION", 999):
            self.assertNotEqual(self.create()["plan_hash"], before)

    def test_plan_pins_generated_commands_and_destructive_flags(self):
        plan = self.create()
        self.assertEqual([phase["id"] for phase in plan["phases"]], phases.PHASES)
        storage = next(phase for phase in plan["phases"] if phase["id"] == "storage")
        self.assertEqual(storage["apply"], phases.command_for("storage", TARGET, install_fixture()))
        self.assertEqual(storage["check"], phases.check_command_for("storage", TARGET, install_fixture()))
        self.assertTrue(storage["destructive"])
        preflight = next(phase for phase in plan["phases"] if phase["id"] == "preflight")
        self.assertFalse(preflight["destructive"])
        self.assertEqual(plan["disks"][0]["serial"], "QM00001")

    def test_missing_inventory_is_reported(self):
        with self.assertRaises(AgentError) as caught:
            install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), None)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_plan_requires_full_erase_authorization(self):
        with self.assertRaises(AgentError) as caught:
            self.create(install={"disk_must_be_erased": False})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        with self.assertRaises(AgentError) as second:
            install_plan.create_install_plan({**TARGET, "disk_erase_authorized": False}, install_fixture(), project_plan(), inventory())
        self.assertEqual(second.exception.code, CONFIG_INVALID)

    def test_later_mutation_of_install_does_not_change_plan(self):
        install = install_fixture()
        plan = install_plan.create_install_plan(TARGET, install, project_plan(), inventory())
        install["hostname"] = "mutated"
        install["disk"] = "/dev/other"
        self.assertEqual(plan["install"], install_fixture())
        self.assertEqual(plan["plan_hash"], install_plan.compute_plan_hash(plan))

    def test_hash_covers_target_install_and_destructive_flags(self):
        plan = self.create()
        base = install_plan.compute_plan_hash(plan)

        def changed(mutate):
            copy = json.loads(json.dumps(plan))
            mutate(copy)
            return install_plan.compute_plan_hash(copy)

        self.assertNotEqual(base, changed(lambda p: p["target"].update(host="192.0.2.99")))
        self.assertNotEqual(base, changed(lambda p: p["install"].update(hostname="other")))
        self.assertNotEqual(base, changed(lambda p: p["phases"][0].update(destructive=not p["phases"][0]["destructive"])))
        self.assertEqual(base, changed(lambda p: p.update(created_at="1999-01-01T00:00:00Z")))


class PlanStorageTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.object(phases, "public_key_for_target", return_value=PUBLIC_KEY)
        self.key.start()

    def tearDown(self):
        self.key.stop()

    def test_save_and_load_round_trip_is_idempotent(self):
        with temp_home():
            plan = install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), inventory())
            saved = install_plan.save_install_plan(plan)
            again = install_plan.save_install_plan(
                install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), inventory())
            )
            self.assertEqual(again["created_at"], saved["created_at"])
            self.assertEqual(install_plan.load_install_plan(plan["plan_id"]), saved)
            with self.assertRaises(AgentError) as caught:
                install_plan.load_install_plan("0" * 16)
            self.assertEqual(caught.exception.code, PLAN_NOT_FOUND)

    def test_modified_plan_file_is_rejected(self):
        with temp_home():
            plan = install_plan.save_install_plan(
                install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), inventory())
            )
            path = paths.plans_dir() / f"{plan['plan_id']}.json"
            tampered = load_json(path)
            tampered["phases"][1]["apply"] = "reboot"
            save_json(path, tampered)
            with self.assertRaises(AgentError) as caught:
                install_plan.load_install_plan(plan["plan_id"])
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_forged_self_consistent_plan_under_other_id_is_rejected(self):
        with temp_home():
            plan = install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), inventory())
            plan["phases"][1]["apply"] = "reboot"
            plan["plan_hash"] = install_plan.compute_plan_hash(plan)
            plan["plan_id"] = "a" * 16
            save_json(paths.plans_dir() / ("a" * 16 + ".json"), plan)
            with self.assertRaises(AgentError) as caught:
                install_plan.load_install_plan("a" * 16)
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_save_rejects_plan_edited_after_create(self):
        with temp_home():
            plan = install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), inventory())
            plan["phases"][1]["apply"] = "reboot"
            with self.assertRaises(AgentError) as caught:
                install_plan.save_install_plan(plan)
            self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)
            self.assertFalse((paths.plans_dir() / f"{plan['plan_id']}.json").exists())

    def test_malformed_plan_files_are_rejected(self):
        with temp_home():
            paths.plans_dir().mkdir(parents=True, exist_ok=True)
            for name, text in (("b" * 16, "[]"), ("c" * 16, "{not json")):
                (paths.plans_dir() / f"{name}.json").write_text(text, encoding="utf-8")
                with self.assertRaises(AgentError) as caught:
                    install_plan.load_install_plan(name)
                self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_invalid_plan_id_is_rejected_before_touching_files(self):
        with self.assertRaises(AgentError) as caught:
            install_plan.load_install_plan("../../etc/passwd")
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


if __name__ == "__main__":
    unittest.main()
