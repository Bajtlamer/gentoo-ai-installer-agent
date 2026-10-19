import copy
import unittest
from unittest import mock

from gentoo_agent.core import install_plan
from gentoo_agent.core.errors import CONFIG_INVALID, INVENTORY_MISSING, PLAN_HASH_MISMATCH, AgentError
from gentoo_agent.core.provision_plan import create_provision_plan
from tests.helpers import temp_home
from tests.provision_helpers import (
    GPU_SYSFS, ROOT_UUID, host_identity_result, PROVISION_TARGET, cpu_result, gpu_result, memory_result, portage_conf_result,
    provision_inventory, provision_settings, result, save_provision_plan,
)


def create(inventory=None, settings=None, target=PROVISION_TARGET):
    return create_provision_plan(target, settings or provision_settings(),
                                 provision_inventory() if inventory is None else inventory)


class PlanKindTests(unittest.TestCase):
    def test_a_plan_without_kind_is_an_install_plan(self):
        self.assertEqual(install_plan.plan_kind({"phases": []}), "install")
        self.assertEqual(install_plan.plan_kind({"kind": "provision"}), "provision")


class CreateProvisionPlanTests(unittest.TestCase):
    def test_the_plan_freezes_identity_settings_and_every_program(self):
        plan = create()
        self.assertEqual(plan["kind"], "provision")
        self.assertEqual(plan["identity"], {"hostname": "gentoo", "root_uuid": ROOT_UUID,
                                            "hostkey": "SHA256:" + "A" * 43})
        self.assertEqual(plan["provision"]["tuning"], {"jobs": 15, "emerge_jobs": 3, "load_average": 24})
        self.assertEqual(plan["provision"]["gpu"]["pci_id"], "10de:2f04")
        self.assertEqual(plan["provision"]["accept_licenses"], ["NVIDIA-r2"])
        self.assertEqual(plan["provision"]["memory_gib"], 31)
        self.assertEqual([phase["id"] for phase in plan["phases"]],
                         ["preflight", "portage-tuning", "portage-sync", "nvidia-config", "nvidia-drivers",
                          "nvidia-verify", "gpu-online"])
        self.assertEqual([phase["run"] for phase in plan["phases"]], [True] * 6 + [False])
        self.assertTrue(all(phase["apply"] and phase["check"] for phase in plan["phases"]))
        self.assertNotIn("disks", plan)
        self.assertNotIn("install", plan)
        self.assertEqual(plan["plan_id"], plan["plan_hash"][:16])

    def test_the_same_inputs_give_the_same_plan_id(self):
        inventory = provision_inventory()
        self.assertEqual(create(inventory)["plan_id"], create(inventory)["plan_id"])

    def plan_id(self, inventory=None, **kwargs):
        return create(inventory, **kwargs)["plan_id"]

    def test_the_plan_id_changes_with_the_gpu_pci_id(self):
        other = GPU_SYSFS.replace("0x2f04", "0x2f05")
        self.assertNotEqual(self.plan_id(), self.plan_id(provision_inventory(gpu=gpu_result(other))))

    def test_the_plan_id_changes_with_the_cpu_thread_count(self):
        self.assertNotEqual(self.plan_id(), self.plan_id(provision_inventory(cpu=cpu_result(16))))

    def test_the_plan_id_changes_with_the_root_uuid(self):
        other = provision_inventory(host_identity=host_identity_result(root_uuid="ffffffff-ffff-4fff-8fff-ffffffffffff"))
        self.assertNotEqual(self.plan_id(), self.plan_id(other))

    def test_the_plan_id_changes_with_the_host_key(self):
        other = provision_inventory(host_identity=host_identity_result(hostkey="SHA256:" + "B" * 43))
        self.assertNotEqual(self.plan_id(), self.plan_id(other))

    def test_the_plan_id_changes_with_the_hostname(self):
        other = provision_inventory(host_identity=host_identity_result(hostname="other"))
        self.assertNotEqual(self.plan_id(), self.plan_id(other))

    def test_the_plan_id_changes_with_the_ssh_target_host(self):
        self.assertNotEqual(self.plan_id(), self.plan_id(target={**PROVISION_TARGET, "host": "192.0.2.11"}))

    def test_the_plan_id_changes_with_the_memory_size(self):
        self.assertNotEqual(self.plan_id(), self.plan_id(provision_inventory(memory=memory_result(65536000))))

    def test_different_licenses_give_a_different_plan(self):
        inventory = provision_inventory()
        other = create(inventory, provision_settings(accept_licenses=["NVIDIA-r2", "Apache-2.0"]))
        self.assertNotEqual(create(inventory)["plan_id"], other["plan_id"])

    def test_different_package_use_gives_a_different_plan_that_holds_it(self):
        inventory = provision_inventory()
        other = create(inventory, provision_settings(package_use=[">=media-libs/libglvnd-1.7.0 X"]))
        self.assertNotEqual(create(inventory)["plan_id"], other["plan_id"])
        self.assertEqual(other["provision"]["package_use"], [">=media-libs/libglvnd-1.7.0 X"])
        self.assertEqual(create(inventory)["provision"]["package_use"], [])

    def test_the_plan_can_be_saved_and_loaded(self):
        with temp_home():
            saved = save_provision_plan()
            loaded = install_plan.load_install_plan(saved["plan_id"])
        self.assertEqual(loaded, saved)

    def test_a_modified_file_is_refused(self):
        with temp_home() as home:
            saved = save_provision_plan()
            path = home / "plans" / f"{saved['plan_id']}.json"
            path.write_text(path.read_text(encoding="utf-8").replace("NVIDIA-r2", "Other-1"), encoding="utf-8")
            with self.assertRaises(AgentError) as caught:
                install_plan.load_install_plan(saved["plan_id"])
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_the_users_own_portage_settings_become_a_warning(self):
        plan = create(provision_inventory(portage_conf=portage_conf_result('MAKEOPTS="-j2"\n')))
        self.assertEqual(plan["warnings"],
                         ["/etc/portage/make.conf already sets MAKEOPTS; the plan's value replaces it."])

    def test_a_video_cards_assignment_is_no_nvidia_plan_warning(self):
        plan = create(provision_inventory(portage_conf=portage_conf_result('VIDEO_CARDS="intel"\n')))
        self.assertEqual(plan["warnings"], [])


class RefusalTests(unittest.TestCase):
    def refused(self, **kwargs):
        with self.assertRaises(AgentError) as caught:
            create(**kwargs)
        return caught.exception

    def test_no_inventory(self):
        with self.assertRaises(AgentError) as caught:
            create_provision_plan(PROVISION_TARGET, provision_settings(), None)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_a_stale_inventory(self):
        inventory = provision_inventory()
        inventory["collected_at"] = "2020-01-01T00:00:00+00:00"
        self.assertEqual(self.refused(inventory=inventory).code, INVENTORY_MISSING)

    def test_no_nvidia_gpu(self):
        text = "0000:00:00.0 0x060000 0x1022 0x14d8\n"
        error = self.refused(inventory=provision_inventory(gpu=gpu_result(text)))
        self.assertEqual(error.code, CONFIG_INVALID)
        self.assertEqual(error.details["nvidia_gpus"], 0)

    def test_two_nvidia_gpus_are_ambiguous(self):
        text = GPU_SYSFS + "0000:02:00.0 0x030000 0x10de 0x2f05\n"
        error = self.refused(inventory=provision_inventory(gpu=gpu_result(text)))
        self.assertEqual(error.code, CONFIG_INVALID)
        self.assertEqual(error.details["nvidia_gpus"], 2)

    def test_invalid_settings(self):
        self.assertEqual(self.refused(settings=provision_settings(accept_licenses=[])).code, CONFIG_INVALID)

    def test_the_ssh_user_must_be_root(self):
        self.assertEqual(self.refused(target={**PROVISION_TARGET, "user": "roza"}).code, CONFIG_INVALID)

    def test_a_failed_required_result_is_refused(self):
        self.assertEqual(self.refused(inventory=provision_inventory(cpu=result("cpu", "", 1))).code, INVENTORY_MISSING)


class FailClosedAndShapeTests(unittest.TestCase):
    def test_a_hostile_thread_count_fails_closed(self):
        with self.assertRaises(AgentError) as caught:
            create(provision_inventory(cpu=cpu_result(5000)))
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_the_tuning_is_refused_before_any_program_is_generated(self):
        with mock.patch("gentoo_agent.core.profiles.command_for") as command_for:
            with self.assertRaises(AgentError) as caught:
                create(provision_inventory(cpu=cpu_result(5000)))
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        command_for.assert_not_called()

    def test_the_inputs_are_not_modified(self):
        settings = provision_settings()
        inventory = provision_inventory()
        target = dict(PROVISION_TARGET)
        before = copy.deepcopy((settings, inventory, target))
        create_provision_plan(target, settings, inventory)
        self.assertEqual((settings, inventory, target), before)

    def test_the_plan_has_the_provision_shape(self):
        plan = create()
        self.assertNotIn("install", plan)
        self.assertNotIn("disks", plan)
        for phase in plan["phases"]:
            self.assertEqual(set(phase), {"id", "destructive", "run", "apply", "check"})
