import datetime as dt
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import approval, install_plan, phases
from gentoo_agent.core.errors import INVENTORY_MISSING
from gentoo_agent.core.config import save_json
from gentoo_agent.core.errors import (
    APPROVAL_EXPIRED,
    APPROVAL_REQUIRED,
    CONFIG_INVALID,
    PLAN_HASH_MISMATCH,
    AgentError,
)
from tests.helpers import (
    fresh_inventory,
    identity_result,
    partitions_result,
    save_dual_boot_plan,
    storage_result,
    temp_home,
    windows_disk,
)


REPO = Path(__file__).resolve().parents[1]
TARGET = {
    "name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
    "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True,
}
START = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)


def save_signed(plan_id: str, record: dict) -> None:
    from gentoo_agent.core import signing

    unsigned = {name: value for name, value in record.items() if name != "signature"}
    save_json(approval.approval_path(plan_id),
              {**unsigned, "signature": signing.sign(unsigned, signing.load_or_create_key())})


def make_plan(serial: str = "QM00001", changes: dict | None = None, model: str = "QEMU HARDDISK") -> dict:
    install = json.loads((REPO / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))
    install.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True})
    install.update(changes or {})
    project = json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8"))
    stdout = json.dumps({"blockdevices": [
        {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": model, "serial": serial, "wwn": None},
    ]})
    inventory = fresh_inventory({"label": "boot_mode", "returncode": 0, "stdout": "bios-or-unknown"},
                                storage_result("/dev/sda"),
                                {"label": "disk_identity", "returncode": 0, "stdout": stdout},
                                identity_result("x86_64"),
                                partitions_result("/dev/sda"))
    with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAA test"):
        return install_plan.save_install_plan(install_plan.create_install_plan(TARGET, install, project, inventory))


class ApprovalTests(unittest.TestCase):
    def test_summary_is_built_from_the_plan(self):
        with temp_home():
            plan = make_plan()
            summary = approval.approval_summary(plan)
            weak = approval.approval_summary(make_plan(serial=""))
        self.assertIn(plan["plan_hash"], summary)
        self.assertIn("serial=QM00001", summary)
        self.assertIn("root@192.0.2.10:22", summary)
        self.assertIn("Destructive phases: storage, filesystems, base-system", summary)
        self.assertIn("all data on the disk will be destroyed", summary)
        self.assertNotIn("WARNING", summary)
        self.assertIn("WARNING: this disk has no serial number or WWN", weak)

    def test_unapproved_plan_is_refused(self):
        with temp_home():
            plan = make_plan()
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)
        self.assertIn(f"gentoo-agent approve {plan['plan_id']}", caught.exception.message)

    def test_recorded_approval_allows_the_plan_until_expiry(self):
        with temp_home():
            plan = make_plan()
            record = approval.record_approval(plan, "cli", at=START)
            self.assertEqual(record["expires_at"], "2026-10-15T13:00:00+00:00")
            loaded = approval.require_approved_plan(plan["plan_id"], at=START + dt.timedelta(minutes=59))
            self.assertEqual(loaded["plan_hash"], plan["plan_hash"])
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START + dt.timedelta(minutes=60))
        self.assertEqual(caught.exception.code, APPROVAL_EXPIRED)

    def test_approval_for_another_hash_is_rejected(self):
        with temp_home():
            plan = make_plan()
            record = approval.record_approval(plan, "cli", at=START)
            save_signed(plan["plan_id"], {**record, "plan_hash": "0" * 64})
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_unknown_channel_is_invalid(self):
        with temp_home():
            plan = make_plan()
            with self.assertRaises(AgentError) as caught:
                approval.record_approval(plan, "model", at=START)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_phrase_names_the_plan(self):
        self.assertEqual(approval.approval_phrase({"plan_id": "abc"}), "APPROVE abc")

    def test_status_reports_both_states(self):
        with temp_home():
            plan = make_plan()
            self.assertEqual(approval.approval_status(plan["plan_id"], at=START)["code"], APPROVAL_REQUIRED)
            approval.record_approval(plan, "elicitation", at=START)
            status = approval.approval_status(plan["plan_id"], at=START)
        self.assertEqual(status, {"approved": True, "channel": "elicitation", "expires_at": "2026-10-15T13:00:00+00:00"})

    def _write(self, plan, **changes):
        record = approval.record_approval(plan, "cli", at=START)
        value = {**record, **changes}
        for key in [k for k, v in changes.items() if v is KeyError]:
            del value[key]
        save_signed(plan["plan_id"], value)

    def _assert_invalid(self, plan, at=START):
        with self.assertRaises(AgentError) as caught:
            approval.require_approved_plan(plan["plan_id"], at=at)
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)
        self.assertIn("is invalid", caught.exception.message)
        self.assertFalse(approval.approval_status(plan["plan_id"], at=at)["approved"])

    def test_far_future_expiry_is_invalid(self):
        with temp_home():
            plan = make_plan()
            self._write(plan, expires_at="2999-01-01T00:00:00+00:00")
            self._assert_invalid(plan)

    def test_approval_recorded_in_the_future_is_invalid(self):
        with temp_home():
            plan = make_plan()
            approval.record_approval(plan, "cli", at=dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc))
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"])
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_extra_or_missing_key_is_invalid(self):
        with temp_home():
            plan = make_plan()
            self._write(plan, extra="x")
            self._assert_invalid(plan)
            self._write(plan, channel=KeyError)
            self._assert_invalid(plan)

    def test_record_for_another_plan_id_is_invalid(self):
        with temp_home():
            plan = make_plan()
            self._write(plan, plan_id="0" * 16)
            self._assert_invalid(plan)

    def test_record_with_unknown_channel_is_invalid(self):
        with temp_home():
            plan = make_plan()
            self._write(plan, channel="model")
            self._assert_invalid(plan)

    def test_naive_timestamp_is_invalid(self):
        with temp_home():
            plan = make_plan()
            self._write(plan, expires_at="2026-10-15T13:00:00")
            self._assert_invalid(plan)

    def test_garbage_files_are_invalid(self):
        with temp_home():
            plan = make_plan()
            path = approval.approval_path(plan["plan_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not json", encoding="utf-8")
            self._assert_invalid(plan)
            path.write_text("[]", encoding="utf-8")
            self._assert_invalid(plan)

    def test_naive_at_is_invalid(self):
        with temp_home():
            plan = make_plan()
            with self.assertRaises(AgentError) as caught:
                approval.record_approval(plan, "cli", at=dt.datetime(2026, 10, 15, 12, 0))
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_non_string_channel_is_invalid(self):
        with temp_home():
            plan = make_plan()
            with self.assertRaises(AgentError) as caught:
                approval.record_approval(plan, ["cli"], at=START)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_caller_dict_must_match_the_stored_plan(self):
        with temp_home():
            plan = make_plan()
            forged = {**plan, "plan_hash": "0" * 64}
            with self.assertRaises(AgentError) as caught:
                approval.record_approval(forged, "cli", at=START)
            self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)
            with self.assertRaises(AgentError) as caught:
                approval.approval_summary(forged)
            self.assertEqual(caught.exception.code, PLAN_HASH_MISMATCH)

    def test_summary_describes_the_install(self):
        with temp_home():
            summary = approval.approval_summary(make_plan())
        self.assertIn("Install: boot=bios", summary)
        self.assertIn("Architecture: amd64", summary)
        self.assertIn("hostname=gentoo", summary)
        self.assertIn("Created: ", summary)
        self.assertIn("Network: dhcp", summary)
        self.assertIn(
            "Passwords: source=environment root_password_env=GENTOO_ROOT_PASSWORD "
            "user_password_env=GENTOO_USER_PASSWORD",
            summary,
        )
        self.assertIn("Access: allow_root_password_ssh=no configure_sudo=yes user_groups=wheel", summary)

    def test_summary_shows_static_network(self):
        static = {"network_method": "static",
                  "network_static": {"interface": "enp1s0", "address": "10.0.0.2/24", "gateway": "10.0.0.1", "dns": ["1.1.1.1", "9.9.9.9"]}}
        with temp_home():
            summary = approval.approval_summary(make_plan(changes=static))
        self.assertIn("Network: static interface=enp1s0 address=10.0.0.2/24 gateway=10.0.0.1 dns=1.1.1.1,9.9.9.9", summary)


    def test_unprintable_disk_identity_never_becomes_a_plan(self):
        for model in ("NVMe\nDisk erase: no", "NVMe\x1b[2K", "NVMe\rx"):
            with self.subTest(model=model), temp_home():
                with self.assertRaises(AgentError) as caught:
                    make_plan(model=model)
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)
                self.assertIn("unprintable", caught.exception.message)

    def test_unprintable_plan_values_are_escaped_in_the_summary(self):
        with temp_home():
            plan = make_plan()
            plain = approval.approval_summary(plan)
            forged = {key: value for key, value in plan.items()}
            forged["target"] = {**plan["target"], "name": "gentoo\nDisk erase: no\x07"}
            forged["install"] = {**plan["install"], "hostname": "h" * 300}
            forged["plan_hash"] = install_plan.compute_plan_hash(forged)
            forged["plan_id"] = forged["plan_hash"][:install_plan.PLAN_ID_LENGTH]
            save_json(install_plan.plan_path(forged["plan_id"]), forged)
            summary = approval.approval_summary(forged)
        self.assertEqual(len(summary.splitlines()), len(plain.splitlines()))
        self.assertIn('"gentoo\\nDisk erase: no\\u0007"', summary)
        self.assertNotIn("\x07", summary)
        self.assertNotIn("h" * 201, summary)
        self.assertIn('"' + "h" * 199 + "...", summary)
        self.assertEqual(sum(line.startswith("Disk erase:") for line in summary.splitlines()), 1)

    def test_erase_and_destructive_lines_come_first(self):
        with temp_home():
            lines = approval.approval_summary(make_plan()).splitlines()
        self.assertTrue(lines[1].startswith("Plan hash: "))
        self.assertTrue(lines[2].startswith("Disk erase: YES"))
        self.assertTrue(lines[3].startswith("Destructive phases: "))
        first_disk = next(index for index, line in enumerate(lines) if line.startswith("Disk (erased): "))
        self.assertLess(2, first_disk)


class DualBootApprovalTests(unittest.TestCase):
    def test_phrase_names_the_windows_disk_when_it_is_erased(self):
        self.assertEqual(approval.approval_phrase({"plan_id": "abc", "erases_existing_os": None}), "APPROVE abc")
        plan = {"plan_id": "abc", "erases_existing_os": {"disk": "/dev/sdb", "os": "windows", "evidence": []}}
        self.assertEqual(approval.erase_phrase(plan), "ERASE WINDOWS /dev/sdb")
        self.assertEqual(approval.approval_phrase(plan), "APPROVE abc ERASE WINDOWS /dev/sdb")

    def test_dialog_starts_with_the_windows_erase_warning(self):
        with temp_home():
            lines = approval.approval_summary(save_dual_boot_plan(nvme=windows_disk("/dev/nvme0n1"))).splitlines()
        self.assertEqual(lines[0], "WARNING: /dev/nvme0n1 contains Windows (esp vfat /dev/nvme0n1p1, msr /dev/nvme0n1p2, "
                                   "ntfs /dev/nvme0n1p3). It will be erased.")
        self.assertTrue(lines[1].startswith("Plan: "))

    def test_dialog_lists_the_dual_boot_entry_and_the_kept_disk(self):
        with temp_home():
            summary = approval.approval_summary(save_dual_boot_plan(sda=windows_disk("/dev/sda")))
        self.assertIn("Dual boot: Windows on /dev/sda (EFI UUID ABCD-1234) will be added to the GRUB menu. "
                      "Gentoo becomes the default; Windows may ask once for its BitLocker recovery key.", summary)
        self.assertIn("Windows disk (kept, not written): /dev/sda  size=256060514304 bytes", summary)
        self.assertIn("Disk (erased): /dev/nvme0n1", summary)
        self.assertTrue(any(line.startswith("Disk (erased): ") for line in summary.splitlines()))
        self.assertFalse(summary.startswith("WARNING"))

    def test_dialog_shows_plan_warnings_after_the_destructive_line(self):
        with temp_home():
            lines = approval.approval_summary(save_dual_boot_plan(secure_boot="unknown")).splitlines()
        self.assertTrue(lines[3].startswith("Destructive phases: "))
        self.assertTrue(lines[4].startswith("WARNING: Secure Boot state is unknown"))

    def test_ordinary_approval_does_not_satisfy_a_plan_that_erases_windows(self):
        with temp_home():
            plan = save_dual_boot_plan(nvme=windows_disk("/dev/nvme0n1"))
            approval.record_approval(plan, "cli", at=START)
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
            self.assertIn("ERASE WINDOWS /dev/nvme0n1", caught.exception.message)
            self.assertFalse(approval.approval_status(plan["plan_id"], at=START)["approved"])
            approval.record_approval(plan, "cli", at=START, erase_confirmed=True)
            self.assertEqual(approval.require_approved_plan(plan["plan_id"], at=START)["plan_id"], plan["plan_id"])
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_record_stores_whether_the_extended_phrase_was_used(self):
        with temp_home():
            plan = make_plan()
            self.assertIs(approval.record_approval(plan, "cli", at=START)["erase_confirmed"], False)
            self.assertIs(approval.record_approval(plan, "cli", at=START, erase_confirmed=True)["erase_confirmed"], True)
            with self.assertRaises(AgentError) as caught:
                approval.record_approval(plan, "cli", at=START, erase_confirmed="yes")
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_record_with_a_non_boolean_flag_is_invalid(self):
        with temp_home():
            plan = make_plan()
            record = approval.record_approval(plan, "cli", at=START)
            save_signed(plan["plan_id"], {**record, "erase_confirmed": "true"})
            with self.assertRaises(AgentError) as caught:
                approval.require_approved_plan(plan["plan_id"], at=START)
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)
        self.assertIn("is invalid", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
