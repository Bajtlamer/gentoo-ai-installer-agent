import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_agent.core import runner, state
from gentoo_agent.core.errors import APPROVAL_REQUIRED, CONFIG_INVALID, AgentError
from gentoo_agent.core.phases import PHASES


ROOT = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root", "identity_file": "~/.ssh/id_test"}


def fixture() -> dict:
    return json.loads((ROOT / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))


def approved(install: dict) -> tuple[dict, dict, dict]:
    target = {**TARGET, "execution_enabled": True, "target_is_disposable": True, "disk_erase_authorized": True}
    install = {**install, "execution_enabled": True, "review_status": "approved_for_execution"}
    plan = {"execution_enabled": True}
    return target, install, plan


class SelectPhasesTests(unittest.TestCase):
    def test_default_is_every_phase(self):
        self.assertEqual(runner.select_phases(None), PHASES)

    def test_start_phase_slices_the_list(self):
        self.assertEqual(runner.select_phases("kernel"), ["kernel", "bootloader", "validation"])

    def test_unknown_start_phase_is_invalid(self):
        with self.assertRaises(AgentError) as caught:
            runner.select_phases("reboot")
        self.assertEqual(caught.exception.code, CONFIG_INVALID)


class ExecutionGateTests(unittest.TestCase):
    def test_wrong_phrase_is_refused(self):
        target, install, plan = approved(fixture())
        with self.assertRaises(AgentError) as caught:
            runner.execution_gate(target, install, plan, "INSTALL gentoo ON /dev/sdb")
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_unapproved_review_is_refused(self):
        target, install, plan = approved(fixture())
        install["review_status"] = "pending_operator_review"
        with self.assertRaises(AgentError) as caught:
            runner.execution_gate(target, install, plan, "INSTALL gentoo ON /dev/sda")
        self.assertEqual(caught.exception.code, APPROVAL_REQUIRED)

    def test_disabled_plan_is_refused(self):
        target, install, plan = approved(fixture())
        plan["execution_enabled"] = False
        with self.assertRaises(AgentError):
            runner.execution_gate(target, install, plan, "INSTALL gentoo ON /dev/sda")

    def test_erase_without_authorization_is_refused(self):
        target, install, plan = approved(fixture())
        install["disk_must_be_erased"] = True
        with self.assertRaises(AgentError):
            runner.execution_gate(target, install, plan, "INSTALL gentoo ON /dev/sda")

    def test_fully_approved_passes(self):
        target, install, plan = approved(fixture())
        install.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True})
        runner.execution_gate(target, install, plan, "INSTALL gentoo ON /dev/sda")


class DryRunTests(unittest.TestCase):
    def test_render_lists_header_and_each_phase(self):
        with patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA test"):
            text = runner.render_dry_run(TARGET, fixture(), ["preflight", "storage"])
        self.assertTrue(text.startswith("Target: root@192.0.2.10:22\nDisk:   /dev/sda (bios/mbr)\n"))
        self.assertIn("\n\n===== preflight =====\n", text)
        self.assertIn("\n\n===== storage =====\n", text)
        self.assertNotIn("===== filesystems =====", text)


class ExecuteTests(unittest.TestCase):
    def run_execute(self, results_by_label: dict[str, int], phases: list[str]) -> tuple[dict, dict, list[str]]:
        sent = []

        def fake_remote_shell(target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
            sent.append(label)
            return {"label": label, "command": command, "returncode": results_by_label.get(label, 0),
                    "stdout": "", "stderr": f"{label}-stderr", "finished_at": "now"}

        with tempfile.TemporaryDirectory() as tmp, patch.object(state, "STATE_DIR", Path(tmp) / "state"), patch.object(
            runner, "LOG_DIR", Path(tmp) / "logs"
        ), patch.object(runner, "remote_shell", fake_remote_shell):
            result = runner.execute(TARGET, fixture(), phases)
            saved_state = state.load_state()
        return result, saved_state, sent

    def test_completed_checks_skip_apply(self):
        result, saved, sent = self.run_execute({}, ["preflight", "storage"])
        self.assertEqual(result["status"], "installed_pending_reboot")
        self.assertEqual(sent, ["preflight-check", "storage-check"])
        self.assertEqual(saved["completed_phases"], ["preflight", "storage"])

    def test_failed_apply_stops_and_records_state(self):
        result, saved, sent = self.run_execute({"storage-check": 1, "storage": 3}, ["preflight", "storage", "filesystems"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_phase"], "storage")
        self.assertEqual(result["returncode"], 3)
        self.assertEqual(result["stderr"], "storage-stderr")
        self.assertEqual(sent, ["preflight-check", "storage-check", "storage"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["last_error"], "storage apply returned 3")

    def test_failed_verify_stops(self):
        result, saved, _ = self.run_execute({"storage-check": 1, "storage-verify": 1}, ["storage"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(saved["last_error"], "storage verification returned 1")


if __name__ == "__main__":
    unittest.main()
