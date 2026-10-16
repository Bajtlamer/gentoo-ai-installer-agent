import json
import unittest

from gentoo_agent.core.errors import INVENTORY_MISSING, AgentError
from gentoo_agent.core.foreign_os import detect_foreign_os, parse_secure_boot
from tests.helpers import (
    ESP_TYPE,
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
            "esp_partuuid": "6b8c1a2e-0d4f-4b6a-9c3e-1f2a3b4c5d6e",
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
            "bad partuuid": lambda d: d["children"][0].update(partuuid="x"),
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
