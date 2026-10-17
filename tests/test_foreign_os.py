import json
import unittest

from gentoo_agent.core.errors import INVENTORY_MISSING, AgentError
from gentoo_agent.core.foreign_os import detect_foreign_os, dual_boot_entries, parse_secure_boot
from tests.helpers import (
    BASIC_DATA_TYPE,
    ESP_TYPE,
    LINUX_TYPE,
    MSR_TYPE,
    linux_disk,
    partition,
    partitions_result,
    secure_boot_result,
    windows_bios_disk,
    windows_disk,
)


def inventory(*results: dict) -> dict:
    return {"results": list(results)}


class DetectForeignOsTests(unittest.TestCase):
    def test_windows_on_gpt_is_found_with_its_esp(self):
        records = detect_foreign_os(inventory(partitions_result("/dev/nvme0n1", windows_disk("/dev/sda"))))
        self.assertEqual(records, [{
            "disk": "/dev/sda", "os": "windows", "esp_uuid": "ABCD-1234",
            "evidence": ["esp vfat /dev/sda1", "msr /dev/sda2", "ntfs /dev/sda3"],
        }])

    def test_windows_in_bios_mode_has_no_esp_fields(self):
        records = detect_foreign_os(inventory(partitions_result(windows_bios_disk("/dev/sdb"))))
        self.assertEqual(records, [{"disk": "/dev/sdb", "os": "windows-bios",
                                    "evidence": ["ntfs /dev/sdb1", "ntfs /dev/sdb2"]}])

    def test_linux_only_and_empty_disks_hold_no_foreign_os(self):
        self.assertEqual(detect_foreign_os(inventory(partitions_result(linux_disk("/dev/sda"), "/dev/sdb"))), [])

    def test_esp_alone_is_not_windows(self):
        disk = {"path": "/dev/sda", "type": "disk", "children": [
            partition("/dev/sda", 1, ESP_TYPE, "vfat", "ABCD-1234", "6b8c1a2e-0d4f-4b6a-9c3e-1f2a3b4c5d6e")]}
        self.assertEqual(detect_foreign_os(inventory(partitions_result(disk))), [])

    def test_esp_partuuid_is_not_needed(self):
        for partuuid in (None, "x", "not-a-guid\n"):
            with self.subTest(partuuid=partuuid):
                disk = windows_disk("/dev/sda")
                disk["children"][0]["partuuid"] = partuuid
                records = detect_foreign_os(inventory(partitions_result(disk)))
                self.assertEqual([record["esp_uuid"] for record in records], ["ABCD-1234"])
                self.assertNotIn("esp_partuuid", records[0])

    def test_missing_or_failed_partitions_result_is_refused(self):
        for results in ([], [{**partitions_result("/dev/sda"), "returncode": 1}],
                        [{"label": "partitions", "error": "timed out"}]):
            with self.subTest(results=results), self.assertRaises(AgentError) as caught:
                detect_foreign_os(inventory(*results))
            self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def hostile(self, mutate) -> AgentError:
        disk = windows_disk("/dev/sda")
        mutate(disk)
        with self.assertRaises(AgentError) as caught:
            detect_foreign_os(inventory(partitions_result(disk)))
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)
        return caught.exception

    def test_hostile_values_make_the_inventory_unusable(self):
        cases = {
            "uuid with a quote": lambda d: d["children"][0].update(uuid="ABCD-1234'; reboot; '"),
            "lowercase uuid": lambda d: d["children"][0].update(uuid="abcd-1234"),
            "uuid with a newline": lambda d: d["children"][0].update(uuid="ABCD-1234\n"),
            "missing uuid": lambda d: d["children"][0].update(uuid=None),
            "disk path with a control character": lambda d: d.update(path="/dev/sda\x1b[2K"),
            "partition path with a newline": lambda d: d["children"][2].update(path="/dev/sda3\nDisk erase: no"),
            "non-text fstype": lambda d: d["children"][2].update(fstype=["ntfs"]),
            "children not a list": lambda d: d.update(children="sda1"),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.hostile(mutate)

    def test_malformed_json_is_refused(self):
        with self.assertRaises(AgentError) as caught:
            detect_foreign_os(inventory({"label": "partitions", "returncode": 0, "stdout": "not json"}))
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_type_guids_are_compared_without_case(self):
        disk = windows_disk("/dev/sda")
        for child in disk["children"]:
            child["parttype"] = child["parttype"].upper()
        self.assertEqual(detect_foreign_os(inventory(partitions_result(disk)))[0]["os"], "windows")

    def test_every_value_in_a_record_is_printable_text(self):
        records = detect_foreign_os(inventory(partitions_result(windows_disk("/dev/sda"), windows_bios_disk("/dev/sdb"))))
        for record in records:
            for value in [record["disk"], record["os"], *record["evidence"]]:
                self.assertTrue(value.isprintable())
        json.dumps(records)


def disk(path: str, *parts: tuple) -> dict:
    """A disk with partitions given as (parttype, fstype, uuid) tuples, numbered from 1."""
    return {"path": path, "type": "disk", "children": [
        partition(path, number, *part) for number, part in enumerate(parts, start=1)]}


ESP = (ESP_TYPE, "vfat", "1111-2222")
SWAP_TYPE = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"
UEFI = {"boot_mode": "uefi", "dual_boot": "auto", "disk": "/dev/nvme0n1"}


def identity(*disks: tuple) -> dict:
    """The disk_identity result for (path, tran, rm) tuples."""
    devices = [{"path": path, "type": "disk", "size": 1, "model": "m", "serial": path[-3:], "wwn": None,
                "tran": tran, "rm": rm} for path, tran, rm in disks]
    return {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": devices})}


FAILED_RUN_LAYOUT = (ESP, ("0657fd6d-a4ab-43c4-84e5-0933c84b4f4f", "swap"), (LINUX_TYPE, "ext4"))


class EspPropagationTests(unittest.TestCase):
    """When an EFI system partition counts as the boot ESP of a Windows on another disk."""

    def detect(self, disks: list, identities: list | None = None) -> dict:
        results = [partitions_result(*disks)]
        if identities is not None:
            results.append(identity(*identities))
        return {record["disk"]: record for record in detect_foreign_os(inventory(*results))}

    def test_failed_install_disk_next_to_complete_windows_and_ntfs_data_is_not_flagged(self):
        records = self.detect([disk("/dev/nvme0n1", *FAILED_RUN_LAYOUT), windows_disk("/dev/sda"),
                               disk("/dev/sdb", (BASIC_DATA_TYPE, "ntfs"))])
        self.assertNotIn("/dev/nvme0n1", records)
        self.assertEqual(records["/dev/sda"]["os"], "windows")

    def test_ntfs_usb_stick_never_uses_an_esp(self):
        records = self.detect([disk("/dev/nvme0n1", *FAILED_RUN_LAYOUT), disk("/dev/sdc", (BASIC_DATA_TYPE, "ntfs"))],
                              [("/dev/nvme0n1", "nvme", False), ("/dev/sdc", "usb", False)])
        self.assertNotIn("/dev/nvme0n1", records)
        removable = self.detect([disk("/dev/nvme0n1", *FAILED_RUN_LAYOUT), disk("/dev/sdc", (MSR_TYPE,), (BASIC_DATA_TYPE, "ntfs"))],
                                [("/dev/nvme0n1", "nvme", False), ("/dev/sdc", "sata", True)])
        self.assertNotIn("/dev/nvme0n1", removable)

    def test_live_iso_usb_esp_is_never_flagged(self):
        live = disk("/dev/sdd", ("0xef", "vfat", "5555-6666"))
        for other in (disk("/dev/sde", (BASIC_DATA_TYPE, "ntfs")), disk("/dev/sda", (MSR_TYPE,), (BASIC_DATA_TYPE, "ntfs"))):
            with self.subTest(other=other["path"]):
                records = self.detect([live, other], [("/dev/sdd", "usb", True), ("/dev/sde", "usb", True),
                                                      ("/dev/sda", "sata", False)])
                self.assertNotIn("/dev/sdd", records)

    def test_windows_in_bios_mode_and_mbr_ntfs_never_use_an_esp(self):
        for other in (windows_bios_disk("/dev/sda"), disk("/dev/sda", ("0x7", "ntfs"), ("0xef", None))):
            with self.subTest(other=other["children"][-1]["parttype"]):
                self.assertNotIn("/dev/nvme0n1", self.detect([disk("/dev/nvme0n1", *FAILED_RUN_LAYOUT), other]))

    def test_genuine_split_still_flags_the_fixed_esp_disk(self):
        disks = [disk("/dev/sda", (MSR_TYPE,), (BASIC_DATA_TYPE, "ntfs")), disk("/dev/nvme0n1", *FAILED_RUN_LAYOUT)]
        for identities in (None, [("/dev/sda", "sata", False), ("/dev/nvme0n1", "nvme", "0")]):
            with self.subTest(identities=identities):
                records = self.detect(disks, identities)
                self.assertEqual(records["/dev/nvme0n1"], {
                    "disk": "/dev/nvme0n1", "os": "windows-data",
                    "evidence": ["esp vfat /dev/nvme0n1p1", "may hold the boot files of Windows on /dev/sda"]})

    def test_hostile_identity_fields_count_as_not_removable(self):
        disks = [disk("/dev/sda", (MSR_TYPE,), (BASIC_DATA_TYPE, "ntfs")), disk("/dev/nvme0n1", *FAILED_RUN_LAYOUT)]
        for tran, rm in ((["usb"], {"x": 1}), (None, None), (5, "yes")):
            with self.subTest(tran=tran, rm=rm):
                records = self.detect(disks, [("/dev/sda", tran, rm), ("/dev/nvme0n1", tran, rm)])
                self.assertIn("/dev/nvme0n1", records)
        broken = {"label": "disk_identity", "returncode": 0, "stdout": "not json"}
        records = detect_foreign_os(inventory(partitions_result(*disks), broken))
        self.assertIn("/dev/nvme0n1", [record["disk"] for record in records])


class WindowsDataTests(unittest.TestCase):
    """The broad rule that protects a disk holding Windows data or boot files from an ordinary erase."""

    def detect(self, *disks) -> dict:
        records = detect_foreign_os(inventory(partitions_result(*disks)))
        return {record["disk"]: record for record in records}

    def test_windows_split_across_two_disks_protects_both(self):
        records = self.detect(disk("/dev/sda", (MSR_TYPE,), (BASIC_DATA_TYPE, "ntfs", "01D9")),
                              disk("/dev/sdb", ESP))
        self.assertEqual(records["/dev/sda"], {"disk": "/dev/sda", "os": "windows-data",
                                               "evidence": ["msr /dev/sda1", "ntfs /dev/sda2"]})
        self.assertEqual(records["/dev/sdb"], {"disk": "/dev/sdb", "os": "windows-data",
                                               "evidence": ["esp vfat /dev/sdb1", "may hold the boot files of Windows on /dev/sda"]})
        self.assertEqual(dual_boot_entries(UEFI, list(records.values())), [])

    def test_every_esp_is_protected_when_windows_has_none_of_its_own(self):
        records = self.detect(disk("/dev/sda", (BASIC_DATA_TYPE, "ntfs", "01D9")),
                              disk("/dev/sdb", ESP, (LINUX_TYPE, "ext4", "u")), disk("/dev/sdc", ESP))
        self.assertEqual(sorted(records), ["/dev/sda", "/dev/sdb", "/dev/sdc"])
        self.assertEqual(records["/dev/sdc"]["evidence"][-1], "may hold the boot files of Windows on /dev/sda")

    def test_bitlocker_without_msr_on_gpt(self):
        records = self.detect(disk("/dev/sda", ESP, (BASIC_DATA_TYPE, "BitLocker")))
        self.assertEqual(records["/dev/sda"], {"disk": "/dev/sda", "os": "windows-data",
                                               "evidence": ["esp vfat /dev/sda1", "bitlocker /dev/sda2"]})
        self.assertEqual(dual_boot_entries(UEFI, list(records.values())), [])

    def test_bitlocker_on_mbr(self):
        records = self.detect(disk("/dev/sda", ("0x7", "BitLocker")))
        self.assertEqual(records["/dev/sda"], {"disk": "/dev/sda", "os": "windows-bios",
                                               "evidence": ["bitlocker /dev/sda1"]})

    def test_hidden_ntfs_type_on_mbr(self):
        self.assertEqual(self.detect(disk("/dev/sda", ("0x17", "ntfs")))["/dev/sda"]["os"], "windows-bios")

    def test_mbr_disk_with_an_esp_and_ntfs(self):
        records = self.detect(disk("/dev/sda", ("0xef", "vfat", "3333-4444"), ("0x7", "ntfs")))
        self.assertEqual(records["/dev/sda"], {"disk": "/dev/sda", "os": "windows-data",
                                               "evidence": ["esp vfat /dev/sda1", "ntfs /dev/sda2"]})
        self.assertEqual(dual_boot_entries(UEFI, list(records.values())), [])

    def test_msr_alone_is_windows_data(self):
        self.assertEqual(self.detect(disk("/dev/sda", (MSR_TYPE,), (LINUX_TYPE, "ext4")))["/dev/sda"]["os"],
                         "windows-data")

    def test_empty_disk_and_linux_disk_with_an_esp_are_not_flagged(self):
        self.assertEqual(self.detect("/dev/sda", linux_disk("/dev/sdb"), disk("/dev/sdc", ESP)), {})

    def test_fresh_layout_without_filesystems_is_usable_and_not_windows(self):
        """Our own storage phase before mkfs: ESP, swap, and Linux types, no filesystem yet."""
        fresh = disk("/dev/nvme0n1", (ESP_TYPE,), (SWAP_TYPE,), (LINUX_TYPE,))
        mbr = disk("/dev/sda", ("0x83",), ("0x82",))
        self.assertEqual(self.detect(fresh, mbr, windows_disk("/dev/sdb"))["/dev/sdb"]["os"], "windows")
        self.assertEqual(sorted(self.detect(fresh, mbr, windows_disk("/dev/sdb"))), ["/dev/sdb"])

    def test_a_filesystem_without_partition_type_still_counts_when_another_partition_has_one(self):
        self.assertEqual(self.detect(disk("/dev/sda", (LINUX_TYPE,), (None, "ntfs")))["/dev/sda"]["os"],
                         "windows-data")

    def test_partitions_without_any_partition_type_are_unusable(self):
        for parts in ([(None,), (None,)], [(None, None)], [(None, "vfat"), (None, "ntfs")], [(None, "ext4")]):
            with self.subTest(parts=parts):
                with self.assertRaises(AgentError) as caught:
                    self.detect(disk("/dev/sda", *parts))
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_menu_entries_still_need_the_strict_rule(self):
        records = self.detect(windows_disk("/dev/sda"), disk("/dev/sdb", ESP, (BASIC_DATA_TYPE, "BitLocker")))
        self.assertEqual([entry["disk"] for entry in dual_boot_entries(UEFI, list(records.values()))], ["/dev/sda"])
        self.assertEqual(records["/dev/sda"]["os"], "windows")
        self.assertNotIn("esp_uuid", records["/dev/sdb"])


class SecureBootTests(unittest.TestCase):
    def test_states(self):
        cases = [
            ([secure_boot_result("1")], "on"),
            ([secure_boot_result("0")], "off"),
            ([secure_boot_result("unknown")], "unknown"),
            ([secure_boot_result("1\n")], "on"),
            ([secure_boot_result("2")], "unknown"),
            ([secure_boot_result("on")], "unknown"),
            ([secure_boot_result("")], "unknown"),
            ([{**secure_boot_result("1"), "returncode": 1}], "unknown"),
            ([{"label": "secure_boot", "returncode": 0, "stdout": None}], "unknown"),
            ([], "unknown"),
        ]
        for results, expected in cases:
            with self.subTest(results=results):
                self.assertEqual(parse_secure_boot(inventory(*results)), expected)


if __name__ == "__main__":
    unittest.main()
