import datetime as dt
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import install_plan, paths, phases
from gentoo_agent.core.config import load_json, save_json
from gentoo_agent.core.errors import (
    CONFIG_CONFLICT,
    CONFIG_INVALID,
    INVENTORY_MISSING,
    PLAN_HASH_MISMATCH,
    PLAN_NOT_FOUND,
    AgentError,
)
from tests.helpers import (
    DISPOSABLE_TARGET,
    dual_boot_inventory,
    fresh_inventory,
    BASIC_DATA_TYPE,
    ESP_TYPE,
    MSR_TYPE,
    identity_result,
    linux_disk,
    partition,
    partitions_result,
    storage_result,
    temp_home,
    uefi_install,
    windows_bios_disk,
    windows_disk,
)


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


def inventory(serial: str = "QM00001", collected_at: str | None = None, machine: str = "x86_64") -> dict:
    stdout = json.dumps({"blockdevices": [
        {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK", "serial": serial, "wwn": None},
    ]})
    data = fresh_inventory(
        {"label": "boot_mode", "returncode": 0, "stdout": "bios-or-unknown"},
        storage_result("/dev/sda"),
        {"label": "disk_identity", "returncode": 0, "stdout": stdout},
        identity_result(machine),
        partitions_result("/dev/sda"),
    )
    if collected_at is not None:
        data["collected_at"] = collected_at
    return data


class CreatePlanTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.object(phases, "public_key_for_target", return_value=PUBLIC_KEY)
        self.key.start()

    def tearDown(self):
        self.key.stop()

    def create(self, **changes):
        install = {**install_fixture(), **changes.pop("install", {})}
        return install_plan.create_install_plan(TARGET, install, project_plan(), changes.pop("inventory", inventory()))

    def test_architecture_must_match_the_inventory(self):
        with self.assertRaises(AgentError) as caught:
            self.create(inventory=inventory(machine="aarch64"))
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)

    def test_review_errors_block_the_plan(self):
        # validated_settings() does not check the boot_mode value for BIOS tables; the review does.
        with self.assertRaises(AgentError) as caught:
            self.create(install={"boot_mode": "efi"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertIn("boot_mode must be 'uefi' or 'bios'", caught.exception.details["errors"])

    def test_review_needs_the_disk_in_the_storage_inventory(self):
        data = inventory()
        data["results"] = [item for item in data["results"] if item["label"] != "storage"]
        with self.assertRaises(AgentError) as caught:
            self.create(inventory=data)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertIn("configured disk /dev/sda was not found in the latest inventory", caught.exception.details["errors"])

    def test_stale_inventory_is_refused(self):
        for collected_at in ("2020-01-01T00:00:00+00:00", "not a date", ""):
            with self.subTest(collected_at=collected_at):
                with self.assertRaises(AgentError) as caught:
                    self.create(inventory=inventory(collected_at=collected_at))
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_inventory_dated_in_the_future_is_refused(self):
        ahead = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(timespec="seconds")
        with self.assertRaises(AgentError) as caught:
            self.create(inventory=inventory(collected_at=ahead))
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)
        self.assertEqual(caught.exception.details["collected_at"], ahead)

    def test_inventory_a_little_ahead_is_accepted(self):
        ahead = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)).isoformat(timespec="seconds")
        self.create(inventory=inventory(collected_at=ahead))

    def replace_result(self, data: dict, result: dict) -> dict:
        data["results"] = [item for item in data["results"] if item["label"] != result["label"]] + [result]
        return data

    def test_mounted_disk_is_refused(self):
        storage = {"label": "storage_json", "returncode": 0, "stdout": json.dumps({"blockdevices": [
            {"path": "/dev/sda", "type": "disk", "mountpoints": [None],
             "children": [{"path": "/dev/sda1", "type": "part", "mountpoints": ["/mnt/data"]}]},
        ]})}
        with self.assertRaises(AgentError) as caught:
            self.create(inventory=self.replace_result(inventory(), storage))
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)
        self.assertEqual(caught.exception.details["field"], "disk")
        self.assertEqual(caught.exception.details["reason"], "mounted at /mnt/data")
        self.assertEqual(caught.exception.message, "Disk /dev/sda cannot be installed to: mounted at /mnt/data.")

    def test_usb_disk_is_refused(self):
        identity = {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": [
            {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK", "serial": "QM00001",
             "wwn": None, "tran": "usb", "rm": False, "ro": False},
        ]})}
        with self.assertRaises(AgentError) as caught:
            self.create(inventory=self.replace_result(inventory(), identity))
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)
        self.assertEqual(caught.exception.details["field"], "disk")
        self.assertEqual(caught.exception.details["reason"], "usb transport")

    def test_expected_disk_model_must_match_the_inventory(self):
        with self.assertRaises(AgentError) as caught:
            self.create(install={"expected_disk_model": "Samsung SSD"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(caught.exception.details["field"], "expected_disk_model")

    def test_expected_disk_size_must_match_within_one_gib(self):
        self.create(install={"expected_disk_size_gib": 40})
        with self.assertRaises(AgentError) as caught:
            self.create(install={"expected_disk_size_gib": 60})
        self.assertEqual(caught.exception.details["field"], "expected_disk_size_gib")

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
        self.assertEqual(plan["install"], {**install_fixture(), "dual_boot_entries": []})
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


class DualBootPlanTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.object(phases, "public_key_for_target", return_value=PUBLIC_KEY)
        self.key.start()

    def tearDown(self):
        self.key.stop()

    def create(self, install: dict | None = None, **inventory_options) -> dict:
        return install_plan.create_install_plan(DISPOSABLE_TARGET, install or uefi_install(), project_plan(),
                                                dual_boot_inventory(**inventory_options))

    def test_windows_on_another_disk_becomes_a_frozen_entry(self):
        plan = self.create(sda=windows_disk("/dev/sda"))
        self.assertEqual(plan["install"]["dual_boot_entries"], [
            {"os": "windows", "disk": "/dev/sda", "esp_uuid": "ABCD-1234",
             "loader": "/EFI/Microsoft/Boot/bootmgfw.efi"},
        ])
        bootloader = next(phase for phase in plan["phases"] if phase["id"] == "bootloader")
        self.assertIn("ABCD-1234", bootloader["apply"])
        self.assertIn("ABCD-1234", bootloader["check"])
        self.assertIsNone(plan["erases_existing_os"])
        self.assertEqual(plan["secure_boot"], "off")
        self.assertEqual(plan["warnings"], [])

    def test_windows_disk_identity_joins_the_drift_checked_disks(self):
        plan = self.create(sda=windows_disk("/dev/sda"))
        self.assertEqual([disk["path"] for disk in plan["disks"]], ["/dev/nvme0n1", "/dev/sda"])
        self.assertEqual(plan["disks"][1]["serial"], "W1")
        other = self.create(sda=windows_disk("/dev/sda"), sda_serial="W2")
        self.assertNotEqual(plan["plan_hash"], other["plan_hash"])

    def test_dual_boot_off_derives_no_entries(self):
        plan = self.create({**uefi_install(), "dual_boot": "off"}, sda=windows_disk("/dev/sda"))
        self.assertEqual(plan["install"]["dual_boot_entries"], [])
        self.assertEqual([disk["path"] for disk in plan["disks"]], ["/dev/nvme0n1"])

    def test_no_windows_gives_no_entries_and_no_warning(self):
        for sda in (None, linux_disk("/dev/sda")):
            with self.subTest(sda=bool(sda)):
                plan = self.create(sda=sda)
                self.assertEqual(plan["install"]["dual_boot_entries"], [])
                self.assertEqual(plan["warnings"], [])
                self.assertEqual(len(plan["disks"]), 1)

    def test_entries_in_the_input_are_replaced_by_the_derived_ones(self):
        forged = [{"os": "windows", "disk": "/dev/sdz", "esp_uuid": "FFFF-FFFF",
                   "loader": "/EFI/Microsoft/Boot/bootmgfw.efi"}]
        plan = self.create({**uefi_install(), "dual_boot_entries": forged})
        self.assertEqual(plan["install"]["dual_boot_entries"], [])

    def test_secure_boot_on_is_refused_for_uefi(self):
        with self.assertRaises(AgentError) as caught:
            self.create(sda=windows_disk("/dev/sda"), secure_boot="1")
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)
        self.assertIn("Secure Boot is on", caught.exception.message)
        self.assertIn("refresh the inventory", caught.exception.message)

    def test_unknown_secure_boot_warns(self):
        plan = self.create(secure_boot="unknown")
        self.assertEqual(plan["secure_boot"], "unknown")
        self.assertEqual(len(plan["warnings"]), 1)
        self.assertIn("Secure Boot state is unknown", plan["warnings"][0])

    def test_secure_boot_does_not_matter_for_a_bios_install(self):
        data = inventory()
        data["results"].append({"label": "secure_boot", "returncode": 0, "stdout": "1"})
        plan = install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), data)
        self.assertEqual(plan["secure_boot"], "on")
        self.assertEqual(plan["warnings"], [])

    def test_windows_in_bios_mode_on_another_disk_warns(self):
        plan = self.create(sda=windows_bios_disk("/dev/sda"))
        self.assertEqual(plan["install"]["dual_boot_entries"], [])
        self.assertEqual(plan["warnings"], ["Windows in BIOS mode on /dev/sda will not be in the GRUB menu."])

    def test_windows_with_dual_boot_off_warns(self):
        plan = self.create({**uefi_install(), "dual_boot": "off"}, sda=windows_disk("/dev/sda"))
        self.assertEqual(plan["install"]["dual_boot_entries"], [])
        self.assertEqual(plan["warnings"], ["Windows on /dev/sda will not be added to the GRUB menu (dual_boot is off)."])

    def test_windows_in_bios_mode_with_dual_boot_off_warns_once(self):
        plan = self.create({**uefi_install(), "dual_boot": "off"}, sda=windows_bios_disk("/dev/sda"))
        self.assertEqual(plan["warnings"], ["Windows in BIOS mode on /dev/sda will not be in the GRUB menu."])

    def bios_plan(self, install: dict) -> dict:
        data = inventory()
        data["results"] = [item for item in data["results"] if item["label"] != "partitions"]
        data["results"].append(partitions_result("/dev/sda", windows_disk("/dev/sdb")))
        return install_plan.create_install_plan(TARGET, install, project_plan(), data)

    def test_uefi_windows_on_another_disk_warns_for_a_bios_install(self):
        plan = self.bios_plan(install_fixture())
        self.assertEqual(plan["install"]["dual_boot_entries"], [])
        self.assertEqual(plan["warnings"], [
            "Windows on /dev/sdb will not be added to the GRUB menu: the install boots in BIOS mode."])

    def test_uefi_windows_with_dual_boot_off_warns_once_for_a_bios_install(self):
        plan = self.bios_plan({**install_fixture(), "dual_boot": "off"})
        self.assertEqual(plan["warnings"], [
            "Windows on /dev/sdb will not be added to the GRUB menu (dual_boot is off)."])

    def test_windows_on_the_install_disk_with_dual_boot_off_does_not_warn(self):
        plan = self.create({**uefi_install(), "dual_boot": "off"}, nvme=windows_disk("/dev/nvme0n1"))
        self.assertEqual(plan["warnings"], [])

    def test_windows_on_the_install_disk_is_recorded_as_erased(self):
        plan = self.create(nvme=windows_disk("/dev/nvme0n1"))
        self.assertEqual(plan["erases_existing_os"], {
            "disk": "/dev/nvme0n1", "os": "windows",
            "evidence": ["esp vfat /dev/nvme0n1p1", "msr /dev/nvme0n1p2", "ntfs /dev/nvme0n1p3"],
        })
        self.assertEqual(plan["install"]["dual_boot_entries"], [])

    def test_windows_in_bios_mode_on_the_install_disk_is_recorded_as_erased(self):
        data = inventory()
        data["results"] = [item for item in data["results"] if item["label"] != "partitions"]
        data["results"].append(partitions_result(windows_bios_disk("/dev/sda")))
        plan = install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), data)
        self.assertEqual(plan["erases_existing_os"]["os"], "windows-bios")
        self.assertEqual(plan["warnings"], [])

    def test_windows_split_across_disks_protects_the_esp_on_the_install_disk(self):
        nvme = {"path": "/dev/nvme0n1", "type": "disk", "children": [
            partition("/dev/nvme0n1", 1, ESP_TYPE, "vfat", "1111-2222")]}
        sda = {"path": "/dev/sda", "type": "disk", "children": [
            partition("/dev/sda", 1, MSR_TYPE), partition("/dev/sda", 2, BASIC_DATA_TYPE, "ntfs", "01D9")]}
        plan = self.create(nvme=nvme, sda=sda)
        self.assertEqual(plan["erases_existing_os"], {
            "disk": "/dev/nvme0n1", "os": "windows-data",
            "evidence": ["esp vfat /dev/nvme0n1p1", "esp used by Windows on /dev/sda"],
        })
        self.assertEqual(plan["install"]["dual_boot_entries"], [])
        self.assertEqual(plan["warnings"], [
            "/dev/sda holds Windows data or boot files (msr /dev/sda1, ntfs /dev/sda2) but no complete UEFI "
            "Windows; it will not be added to the GRUB menu."])

    def test_bitlocker_on_the_install_disk_is_recorded_as_erased(self):
        nvme = {"path": "/dev/nvme0n1", "type": "disk", "children": [
            partition("/dev/nvme0n1", 1, ESP_TYPE, "vfat", "1111-2222"),
            partition("/dev/nvme0n1", 2, BASIC_DATA_TYPE, "BitLocker")]}
        plan = self.create(nvme=nvme)
        self.assertEqual(plan["erases_existing_os"]["os"], "windows-data")
        self.assertEqual(plan["erases_existing_os"]["evidence"], ["esp vfat /dev/nvme0n1p1", "bitlocker /dev/nvme0n1p2"])

    def test_partitions_without_metadata_are_refused(self):
        nvme = {"path": "/dev/nvme0n1", "type": "disk", "children": [
            partition("/dev/nvme0n1", 1, None), partition("/dev/nvme0n1", 2, None)]}
        with self.assertRaises(AgentError) as caught:
            self.create(nvme=nvme)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_missing_partition_list_is_refused(self):
        data = inventory()
        data["results"] = [item for item in data["results"] if item["label"] != "partitions"]
        with self.assertRaises(AgentError) as caught:
            install_plan.create_install_plan(TARGET, install_fixture(), project_plan(), data)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_hostile_partition_values_never_become_a_plan(self):
        with self.assertRaises(AgentError) as caught:
            self.create(sda=windows_disk("/dev/sda", esp_uuid="ABCD-1234' ; reboot ; '"))
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_windows_disk_missing_from_the_identity_inventory_is_refused(self):
        data = dual_boot_inventory(sda=windows_disk("/dev/sda"))
        data["results"] = [item for item in data["results"] if item["label"] != "partitions"]
        data["results"].append(partitions_result("/dev/nvme0n1", windows_disk("/dev/sdc")))
        with self.assertRaises(AgentError) as caught:
            install_plan.create_install_plan(DISPOSABLE_TARGET, uefi_install(), project_plan(), data)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)
        self.assertEqual(caught.exception.details, {"disk": "/dev/sdc"})


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
