import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

HAS_MCP = importlib.util.find_spec("mcp") is not None

if HAS_MCP:
    import anyio
    from mcp import Client

    from gentoo_agent.mcp import server as mcp_server

REPO = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "detect_hardware", "list_phases", "resolve_config", "create_plan", "get_plan", "approve_plan",
    "start_install", "run_phase", "resume_run", "cancel_run", "get_run_status", "get_run_log",
    "verify_install", "detect_provision", "create_provision_plan",
}


def call(name, arguments=None, *, elicitation_callback=None):
    async def go():
        kwargs = {"mode": "legacy"}
        if elicitation_callback is not None:
            kwargs["elicitation_callback"] = elicitation_callback
        async with Client(mcp_server.server, **kwargs) as client:
            return await client.call_tool(name, arguments or {})
    return anyio.run(go)


def write_home(home, *, boot="uefi", disks=("/dev/nvme0n1",), machine="x86_64", partitions=None, secure_boot="0"):
    """partitions replaces the partitions result (default: every disk empty)."""
    from gentoo_agent.core.config import save_json
    from tests.helpers import fresh_inventory, identity_result, partitions_result, secure_boot_result, storage_result

    save_json(home / "config" / "target.json", {
        "name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root", "identity_file": "~/.ssh/id_test",
        "target_is_disposable": True, "disk_erase_authorized": True,
    })
    save_json(home / "config" / "plan.json", json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8")))
    save_json(home / "config" / "install.default.json",
              json.loads((REPO / "config" / "install.default.json").read_text(encoding="utf-8")))
    devices = [{"path": path, "type": "disk", "size": 512110190592, "model": "NVMe", "serial": f"S{i}", "wwn": None}
               for i, path in enumerate(disks)]
    save_json(home / "inventory" / "inventory-20261015T120000Z.json", fresh_inventory(
        {"label": "boot_mode", "returncode": 0, "stdout": boot},
        storage_result(*disks),
        {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": devices})},
        identity_result(machine),
        partitions or partitions_result(*disks),
        secure_boot_result(secure_boot),
    ))


def write_provision_home(home):
    from gentoo_agent.core.config import save_json
    from tests.provision_helpers import provision_inventory

    save_json(home / "config" / "target.json", {
        "name": "gentoo-server2", "host": "192.0.2.10", "port": 22, "user": "root", "identity_file": "~/.ssh/id_test",
        "target_is_disposable": False, "disk_erase_authorized": False,
    })
    save_json(home / "inventory" / "provision-inventory-20261019T120000Z.json", provision_inventory())


@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class SkeletonTests(unittest.TestCase):
    def test_tools_are_registered_with_annotations(self):
        async def go():
            async with Client(mcp_server.server, mode="legacy") as client:
                return await client.list_tools()
        tools = {tool.name: tool for tool in anyio.run(go).tools}
        self.assertEqual(set(tools), EXPECTED_TOOLS)
        self.assertFalse(tools["detect_hardware"].annotations.read_only_hint)
        self.assertTrue(tools["list_phases"].annotations.read_only_hint)
        self.assertTrue(tools["start_install"].annotations.destructive_hint)
        self.assertTrue(tools["get_run_log"].annotations.read_only_hint)
        detect = tools["detect_hardware"].annotations
        self.assertEqual((detect.destructive_hint, detect.idempotent_hint, detect.open_world_hint), (False, True, True))
        self.assertFalse(tools["get_run_status"].annotations.read_only_hint)
        self.assertFalse(tools["get_run_status"].annotations.destructive_hint)

    def test_errors_are_structured(self):
        from tests.helpers import temp_home

        with temp_home():
            result = call("get_plan", {"plan_id": "../x"})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")

    def test_list_phases(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            result = call("list_phases")
        ids = [phase["id"] for phase in result.structured_content["phases"]]
        from gentoo_agent.core.phases import PHASES
        self.assertEqual(ids, PHASES)
        self.assertNotIn("discovery", ids)

    def test_unexpected_exception_is_structured_internal_error(self):
        from tests.helpers import temp_home

        with temp_home() as home, patch("gentoo_agent.mcp.server.load_install_plan", side_effect=RuntimeError("boom")):
            write_home(home)
            with self.assertLogs("gentoo_agent.mcp", "ERROR") as logged:
                result = call("get_plan", {"plan_id": "0" * 16})
        self.assertIn("boom", "\n".join(logged.output))
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "INTERNAL_ERROR")
        self.assertNotIn("boom", result.content[0].text)

    def test_corrupt_config_is_structured(self):
        from tests.helpers import temp_home

        for text in ("{not json", "[]"):
            with self.subTest(text=text), temp_home() as home:
                write_home(home)
                (home / "config" / "target.json").write_text(text, encoding="utf-8")
                result = call("detect_hardware", {})
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")

    def test_corrupt_plan_json_is_structured(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            (home / "config" / "plan.json").write_text("{not json", encoding="utf-8")
            result = call("list_phases")
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")

    def test_refresh_without_ssh_is_structured(self):
        from tests.helpers import temp_home

        with temp_home() as home, patch("gentoo_agent.core.inventory.run_ssh_readonly",
                                        side_effect=FileNotFoundError("ssh")):
            write_home(home)
            result = call("detect_hardware", {"refresh": True})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "SSH_UNREACHABLE")

    def test_refresh_against_an_unreachable_target_is_structured(self):
        from gentoo_agent.core import paths
        from gentoo_agent.core.state import load_state
        from tests.helpers import temp_home

        def refused(target, label, command):
            return {"label": label, "command": command, "returncode": 255, "stdout": "",
                    "stderr": "ssh: Could not resolve hostname gentoo.invalid"}

        with temp_home() as home, patch("gentoo_agent.core.inventory.run_ssh_readonly", refused):
            write_home(home)
            before = sorted(paths.inventory_dir().glob("inventory-*.json"))
            result = call("detect_hardware", {"refresh": True})
            after = sorted(paths.inventory_dir().glob("inventory-*.json"))
            state = load_state()
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "SSH_UNREACHABLE")
        self.assertEqual(result.structured_content["details"], {"host": "192.0.2.10"})
        self.assertEqual(before, after)
        self.assertNotIn("last_inventory", state)


@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class DiscoveryAndPlanningTests(unittest.TestCase):
    def test_detect_hardware_summarizes_the_latest_inventory(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            result = call("detect_hardware", {})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["disks"][0]["path"], "/dev/nvme0n1")

    def test_detect_hardware_reports_the_architecture(self):
        from tests.helpers import temp_home

        async def go():
            async with Client(mcp_server.server, mode="legacy") as client:
                return await client.list_tools()
        description = {tool.name: tool for tool in anyio.run(go).tools}["detect_hardware"].description
        self.assertIn("architecture", description)
        with temp_home() as home:
            write_home(home, machine="aarch64")
            result = call("detect_hardware", {})
            resolved = call("resolve_config", {})
        self.assertEqual(result.structured_content["architecture"], "arm64")
        self.assertEqual(resolved.structured_content["install"]["architecture"], "arm64")
        self.assertEqual(resolved.structured_content["sources"]["architecture"], "inferred")

    def test_detect_hardware_without_inventory_asks_for_refresh(self):
        from gentoo_agent.core.config import save_json
        from tests.helpers import temp_home

        with temp_home() as home:
            save_json(home / "config" / "target.json", {"name": "gentoo", "host": "192.0.2.10", "user": "root"})
            result = call("detect_hardware", {})
        self.assertEqual(result.structured_content["code"], "INVENTORY_MISSING")

    def test_resolve_config_preview_never_writes(self):
        from gentoo_agent.core import paths
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            result = call("resolve_config", {"overrides": {"username": "Alex"}, "fallbacks": {"hostname": "server"}})
            plans_exist = paths.plans_dir().exists()
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(result.structured_content["install"]["username"], "alex")
        self.assertEqual(result.structured_content["sources"]["hostname"], "fallback")
        self.assertFalse(plans_exist)

    def test_create_plan_then_get_plan(self):
        from tests.helpers import temp_home

        with temp_home() as home, patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            write_home(home)
            created = call("create_plan", {"overrides": {"user_password": {"env": "ALEX_PW"}}})
            plan_id = created.structured_content["plan_id"]
            shown = call("get_plan", {"plan_id": plan_id})
        self.assertFalse(created.is_error, created.content[0].text)
        self.assertEqual(shown.structured_content["plan_hash"], created.structured_content["plan_hash"])
        self.assertFalse(shown.structured_content["approval"]["approved"])
        self.assertIn("serial=S0", created.structured_content["summary"])
        self.assertEqual(created.structured_content["sources"]["user_password_env"], "override")

    def test_create_plan_reports_plan_warnings_and_secure_boot_refusal(self):
        from tests.helpers import temp_home, windows_disk, partitions_result

        with temp_home() as home, patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            write_home(home, secure_boot="unknown")
            unknown = call("create_plan", {})
        self.assertFalse(unknown.is_error, unknown.content[0].text)
        self.assertTrue(any("Secure Boot state is unknown" in w for w in unknown.structured_content["warnings"]))
        with temp_home() as home:
            write_home(home, disks=("/dev/nvme0n1", "/dev/sda"), secure_boot="1",
                       partitions=partitions_result("/dev/nvme0n1", windows_disk("/dev/sda")))
            refused = call("create_plan", {"overrides": {"disk": "/dev/nvme0n1"}})
        self.assertEqual(refused.structured_content["code"], "CONFIG_CONFLICT")
        self.assertIn("Secure Boot is on", refused.structured_content["message"])

    def test_plan_view_has_the_dual_boot_fields(self):
        from tests.helpers import partitions_result, temp_home, windows_disk

        with temp_home() as home, patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            write_home(home, disks=("/dev/nvme0n1", "/dev/sda"), secure_boot="unknown",
                       partitions=partitions_result("/dev/nvme0n1", windows_disk("/dev/sda")))
            created = call("create_plan", {"overrides": {"disk": "/dev/nvme0n1"}})
            shown = call("get_plan", {"plan_id": created.structured_content["plan_id"]})
        self.assertFalse(created.is_error, created.content[0].text)
        entries = [{"os": "windows", "disk": "/dev/sda", "esp_uuid": "ABCD-1234",
                    "loader": "/EFI/Microsoft/Boot/bootmgfw.efi"}]
        for view in (created.structured_content, shown.structured_content):
            self.assertEqual(view["dual_boot_entries"], entries)
            self.assertEqual(view["secure_boot"], "unknown")
            self.assertIsNone(view["erases_existing_os"])
            self.assertTrue(any("Secure Boot state is unknown" in w for w in view["warnings"]))

    def test_plan_view_shows_that_windows_is_erased(self):
        from tests.helpers import partitions_result, temp_home, windows_disk

        with temp_home() as home, patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            write_home(home, partitions=partitions_result(windows_disk("/dev/nvme0n1")))
            resolved = call("resolve_config", {"overrides": {"disk": "/dev/nvme0n1"}})
            created = call("create_plan", {"overrides": {"disk": "/dev/nvme0n1"}})
            shown = call("get_plan", {"plan_id": created.structured_content["plan_id"]})
        warning = "/dev/nvme0n1 holds Windows; approving will require typing ERASE WINDOWS /dev/nvme0n1"
        self.assertIn(warning, resolved.structured_content["warnings"])
        self.assertIn(warning, created.structured_content["warnings"])
        self.assertEqual(shown.structured_content["erases_existing_os"]["disk"], "/dev/nvme0n1")
        self.assertEqual(shown.structured_content["erases_existing_os"]["os"], "windows")

    def test_plain_text_password_is_refused(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            result = call("create_plan", {"overrides": {"user_password": "hunter2"}})
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")
        self.assertNotIn("hunter2", result.content[0].text)


@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class ApprovalTests(unittest.TestCase):
    def make_plan(self, home):
        with patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            write_home(home)
            return call("create_plan", {}).structured_content["plan_id"]

    def test_accepted_dialog_records_an_approval(self):
        from mcp.types import ElicitResult
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        seen = []

        async def accept(context, params):
            seen.append(params.message)
            return ElicitResult(action="accept", content={"approve": True})

        with temp_home() as home:
            plan_id = self.make_plan(home)
            result = call("approve_plan", {"plan_id": plan_id}, elicitation_callback=accept)
            status = approval_status(plan_id)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertTrue(status["approved"])
        self.assertEqual(status["channel"], "elicitation")
        self.assertIn(plan_id, seen[0])
        self.assertIn("Disk erase: YES", seen[0])

    def test_declined_cancelled_or_unchecked_dialog_records_nothing(self):
        from mcp.types import ElicitResult
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        answers = [ElicitResult(action="decline"), ElicitResult(action="cancel"),
                   ElicitResult(action="accept", content={"approve": False})]
        for answer in answers:
            async def reply(context, params, answer=answer):
                return answer
            with self.subTest(answer=answer.action), temp_home() as home:
                plan_id = self.make_plan(home)
                result = call("approve_plan", {"plan_id": plan_id}, elicitation_callback=reply)
                status = approval_status(plan_id)
            self.assertEqual(result.structured_content["code"], "APPROVAL_DECLINED")
            self.assertFalse(status["approved"])

    def test_non_boolean_approve_records_nothing(self):
        from mcp.types import ElicitResult
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        for value in ("true", 1, "yes"):
            async def reply(context, params, value=value):
                return ElicitResult(action="accept", content={"approve": value})
            with self.subTest(value=value), temp_home() as home:
                plan_id = self.make_plan(home)
                result = call("approve_plan", {"plan_id": plan_id}, elicitation_callback=reply)
                status = approval_status(plan_id)
            self.assertTrue(result.is_error)
            self.assertFalse(status["approved"])

    def make_erase_windows_plan(self, home):
        from tests.helpers import partitions_result, windows_disk

        with patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            write_home(home, partitions=partitions_result(windows_disk("/dev/nvme0n1")))
            created = call("create_plan", {"overrides": {"disk": "/dev/nvme0n1"}})
        self.assertFalse(created.is_error, created.content[0].text)
        return created.structured_content["plan_id"]

    def test_erasing_windows_needs_the_typed_confirmation(self):
        from mcp.types import ElicitResult
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        seen = []

        async def accept(context, params):
            seen.append(params)
            return ElicitResult(action="accept", content={"approve": True, "confirm": "ERASE WINDOWS /dev/nvme0n1"})

        with temp_home() as home:
            plan_id = self.make_erase_windows_plan(home)
            result = call("approve_plan", {"plan_id": plan_id}, elicitation_callback=accept)
            status = approval_status(plan_id)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertTrue(status["approved"])
        self.assertTrue(seen[0].message.startswith("WARNING: /dev/nvme0n1 contains Windows"))
        self.assertIn("type exactly ERASE WINDOWS /dev/nvme0n1 in 'confirm'", seen[0].message)
        self.assertIn("confirm", seen[0].requested_schema["required"])

    def test_missing_or_wrong_confirmation_records_nothing(self):
        from mcp.types import ElicitResult
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        for content in ({"approve": True}, {"approve": True, "confirm": "ERASE WINDOWS /dev/sda"},
                        {"approve": True, "confirm": "erase windows /dev/nvme0n1"},
                        {"approve": True, "confirm": " ERASE WINDOWS /dev/nvme0n1"},
                        {"approve": False, "confirm": "ERASE WINDOWS /dev/nvme0n1"}):
            async def reply(context, params, content=content):
                return ElicitResult(action="accept", content=content)
            with self.subTest(content=content), temp_home() as home:
                plan_id = self.make_erase_windows_plan(home)
                result = call("approve_plan", {"plan_id": plan_id}, elicitation_callback=reply)
                status = approval_status(plan_id)
            self.assertEqual(result.structured_content["code"], "APPROVAL_DECLINED")
            self.assertFalse(status["approved"])

    def test_client_without_elicitation_gets_the_cli_command(self):
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        with temp_home() as home:
            plan_id = self.make_plan(home)
            result = call("approve_plan", {"plan_id": plan_id})
            status = approval_status(plan_id)
        self.assertEqual(result.structured_content["code"], "APPROVAL_REQUIRED")
        self.assertEqual(result.structured_content["details"]["command"], f"gentoo-agent approve {plan_id}")
        self.assertEqual(
            result.structured_content["message"],
            "A human must approve this plan. Ask the operator to run this in their own terminal "
            f"(do not run it yourself): gentoo-agent approve {plan_id}",
        )
        self.assertFalse(status["approved"])

    def test_failing_elicitation_fails_closed(self):
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        async def broken(context, params):
            raise RuntimeError("dialog crashed")

        with temp_home() as home:
            plan_id = self.make_plan(home)
            result = call("approve_plan", {"plan_id": plan_id}, elicitation_callback=broken)
            status = approval_status(plan_id)
        self.assertTrue(result.is_error)
        self.assertIn(result.structured_content["code"], {"APPROVAL_REQUIRED", "APPROVAL_DECLINED"})
        self.assertFalse(status["approved"])


@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class ExecutionTests(unittest.TestCase):
    def create(self, home, **install_changes):
        from gentoo_agent.core.config import load_json, save_json

        write_home(home)
        if install_changes:
            path = home / "config" / "install.default.json"
            save_json(path, {**load_json(path), **install_changes})
        with patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            return call("create_plan", {}).structured_content["plan_id"]

    def approve(self, plan_id):
        from gentoo_agent.core.approval import record_approval
        from gentoo_agent.core.install_plan import load_install_plan

        record_approval(load_install_plan(plan_id), "cli")

    def test_unapproved_plan_is_refused(self):
        from tests.helpers import temp_home

        with temp_home() as home, patch("gentoo_agent.jobs.launcher.subprocess.Popen") as popen:
            plan_id = self.create(home)
            result = call("start_install", {"plan_id": plan_id})
        self.assertEqual(result.structured_content["code"], "APPROVAL_REQUIRED")
        popen.assert_not_called()

    def test_start_and_status_never_return_passwords(self):
        from gentoo_agent.core import runs
        from tests.helpers import temp_home
        from tests.test_launcher import FakeProcess

        with temp_home() as home, patch("gentoo_agent.jobs.launcher.subprocess.Popen", return_value=FakeProcess()):
            plan_id = self.create(home, password_source="generate")
            self.approve(plan_id)
            started = call("start_install", {"plan_id": plan_id})
            run_id = started.structured_content["run_id"]
            runs.update_run(run_id, status="failed")
            runs.release_lock(run_id)
            runs.write_credentials(run_id, {"credentials": {"root": "s3cret"}, "applied": True})
            status = call("get_run_status", {"run_id": run_id})
            still_there = runs.credentials_path(run_id).exists()
        self.assertFalse(started.is_error, started.content[0].text)
        self.assertTrue(status.structured_content["credentials_waiting"])
        self.assertIn(f"gentoo-agent run status {run_id}", status.structured_content["credentials_hint"])
        self.assertNotIn("s3cret", json.dumps(status.structured_content) + status.content[0].text)
        self.assertTrue(still_there)
        self.assertIs(status.structured_content["credentials_applied"], True)

    def test_status_reports_whether_credentials_were_applied(self):
        from gentoo_agent.core import runs
        from tests.helpers import temp_home
        from tests.test_launcher import FakeProcess

        cases = [(False, False), (None, None), ("missing", None)]
        for applied, expected in cases:
            with self.subTest(applied=applied), temp_home() as home, \
                    patch("gentoo_agent.jobs.launcher.subprocess.Popen", return_value=FakeProcess()):
                plan_id = self.create(home, password_source="generate")
                self.approve(plan_id)
                run_id = call("start_install", {"plan_id": plan_id}).structured_content["run_id"]
                runs.update_run(run_id, status="failed")
                runs.release_lock(run_id)
                payload = {"credentials": {"root": "s3cret"}}
                if applied != "missing":
                    payload["applied"] = applied
                runs.write_credentials(run_id, payload)
                status = call("get_run_status", {"run_id": run_id})
                still_there = runs.credentials_path(run_id).exists()
            data = status.structured_content
            self.assertFalse(status.is_error, status.content[0].text)
            self.assertIs(data["credentials_applied"], expected)
            self.assertTrue(still_there)
            self.assertNotIn("s3cret", json.dumps(data) + status.content[0].text)
            self.assertIn(f"gentoo-agent run status {run_id}", data["credentials_hint"])
            if applied is False:
                self.assertTrue(data["credentials_hint"].startswith(
                    "Passwords were generated but the authentication phase did not run; they were not applied."))
            else:
                self.assertTrue(data["credentials_hint"].startswith("Generated passwords are waiting."))

    def test_status_without_credentials_has_no_applied_value(self):
        from gentoo_agent.core import runs
        from tests.helpers import temp_home
        from tests.test_launcher import FakeProcess

        with temp_home() as home, patch("gentoo_agent.jobs.launcher.subprocess.Popen", return_value=FakeProcess()):
            plan_id = self.create(home, password_source="generate")
            self.approve(plan_id)
            run_id = call("start_install", {"plan_id": plan_id}).structured_content["run_id"]
            runs.update_run(run_id, status="failed")
            runs.release_lock(run_id)
            status = call("get_run_status", {"run_id": run_id})
        self.assertFalse(status.structured_content["credentials_waiting"])
        self.assertIsNone(status.structured_content["credentials_applied"])
        self.assertNotIn("credentials_hint", status.structured_content)

    def test_run_phase_records_the_selection(self):
        from gentoo_agent.core import runs
        from tests.helpers import temp_home
        from tests.test_launcher import FakeProcess

        with temp_home() as home, patch("gentoo_agent.jobs.launcher.subprocess.Popen", return_value=FakeProcess()):
            plan_id = self.create(home, password_source="generate")
            self.approve(plan_id)
            result = call("run_phase", {"plan_id": plan_id, "phases": ["kernel"]})
            record = runs.read_run(result.structured_content["run_id"])
        self.assertEqual(record["phase_ids"], ["kernel"])

    def test_prompt_password_source_is_refused(self):
        from tests.helpers import temp_home

        with temp_home() as home, patch("gentoo_agent.jobs.launcher.subprocess.Popen") as popen:
            plan_id = self.create(home, password_source="prompt")
            self.approve(plan_id)
            result = call("start_install", {"plan_id": plan_id})
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")
        popen.assert_not_called()

    def test_verify_install_runs_only_plan_checks(self):
        from gentoo_agent.core.install_plan import load_install_plan
        from tests.helpers import temp_home

        seen = []

        def fake_shell(target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
            seen.append((label, command, input_data))
            return {"label": label, "command": command, "returncode": 0, "stdout": "ok\n", "stderr": "",
                    "finished_at": "t"}

        with temp_home() as home, patch("gentoo_agent.mcp.server.remote_shell", fake_shell), \
                patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            plan_id = self.create(home)
            plan = load_install_plan(plan_id)
            result = call("verify_install", {"plan_id": plan_id})
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual([command for _, command, _ in seen], [phase["check"] for phase in plan["phases"]])
        self.assertTrue(all(data is None for _, _, data in seen))
        self.assertTrue(all(item["complete"] for item in result.structured_content["phases"]))

    def test_verify_install_gives_desktop_checks_a_longer_timeout(self):
        from gentoo_agent.core.install_plan import save_install_plan
        from gentoo_agent.core.provision_plan import create_provision_plan
        from tests.helpers import temp_home
        from tests.provision_helpers import (GPU_SYSFS, PROVISION_TARGET, desktop_inventory, gpu_result,
                                             provision_inventory, provision_settings)

        timeouts = []

        def fake_shell(target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
            timeouts.append(timeout)
            return {"label": label, "command": command, "returncode": 0, "stdout": "", "stderr": "",
                    "finished_at": "t"}

        for settings, inventory, expected in (
            ({"profile": "desktop-plasma"}, desktop_inventory(gpu=gpu_result(GPU_SYSFS)), 1800),
            (provision_settings(), provision_inventory(), 300),
        ):
            timeouts.clear()
            with temp_home(), patch("gentoo_agent.mcp.server.remote_shell", fake_shell):
                saved = save_install_plan(create_provision_plan(PROVISION_TARGET, settings, inventory))
                result = call("verify_install", {"plan_id": saved["plan_id"]})
            self.assertFalse(result.is_error, result.content[0].text)
            self.assertTrue(timeouts)
            self.assertEqual(set(timeouts), {expected})

    def test_verify_install_reports_an_unreachable_target(self):
        from tests.helpers import temp_home

        def refused(target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
            return {"label": label, "command": command, "returncode": 255, "stdout": "",
                    "stderr": "ssh: connect to host 192.0.2.10 port 22: Connection refused", "finished_at": "t"}

        with temp_home() as home, patch("gentoo_agent.mcp.server.remote_shell", refused), \
                patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            plan_id = self.create(home)
            result = call("verify_install", {"plan_id": plan_id})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "SSH_UNREACHABLE")

    def test_verify_install_reports_a_host_key_failure(self):
        from tests.helpers import temp_home

        def refused(target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
            return {"label": label, "command": command, "returncode": 255, "stdout": "",
                    "stderr": "Host key verification failed.\r\n", "finished_at": "t"}

        with temp_home() as home, patch("gentoo_agent.mcp.server.remote_shell", refused), \
                patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            plan_id = self.create(home)
            result = call("verify_install", {"plan_id": plan_id})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "HOST_KEY_UNKNOWN")
        self.assertEqual(result.structured_content["details"]["host"], "192.0.2.10")

    def test_verify_install_refuses_a_tampered_check(self):
        import copy
        from gentoo_agent.core.install_plan import load_install_plan
        from tests.helpers import temp_home

        seen = []

        def fake_shell(*args, **kwargs):
            seen.append(args)
            return {"returncode": 0, "stdout": "", "stderr": ""}

        with temp_home() as home, patch("gentoo_agent.mcp.server.remote_shell", fake_shell), \
                patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            plan_id = self.create(home)
            tampered = copy.deepcopy(load_install_plan(plan_id))
            tampered["phases"][1]["check"] = "rm -rf /"
            with patch("gentoo_agent.mcp.server.load_install_plan", return_value=tampered):
                result = call("verify_install", {"plan_id": plan_id})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "PLAN_HASH_MISMATCH")
        self.assertEqual(seen, [])


@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class ResourceAndPromptTests(unittest.TestCase):
    def test_plan_resource_and_prompt(self):
        from tests.helpers import temp_home

        async def go(plan_id):
            async with Client(mcp_server.server, mode="legacy") as client:
                resource = await client.read_resource(f"gentoo://plan/{plan_id}")
                prompt = await client.get_prompt("install_gentoo", {"request": "user alex, UEFI"})
                return resource, prompt

        with temp_home() as home, patch("gentoo_agent.core.phases.public_key_for_target", return_value="ssh-ed25519 AAAA t"):
            write_home(home)
            plan_id = call("create_plan", {}).structured_content["plan_id"]
            resource, prompt = anyio.run(go, plan_id)
        self.assertEqual(json.loads(resource.contents[0].text)["plan_id"], plan_id)
        text = prompt.messages[0].content.text
        self.assertIn("approve_plan", text)
        self.assertIn("user alex, UEFI", text)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class ProvisionToolTests(unittest.TestCase):
    def test_detect_provision_summarizes_the_saved_inventory(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_provision_home(home)
            result = call("detect_provision")
        self.assertFalse(result.is_error, result.content[0].text)
        data = result.structured_content
        self.assertEqual(data["identity"]["hostname"], "gentoo")
        self.assertEqual(data["cpu"]["threads"], 24)
        self.assertEqual(data["gpus"][0]["vendor"], "nvidia")
        self.assertEqual(data["memory_gib"], 31)
        self.assertEqual(data["hardware"]["video_cards"], ["nvidia"])
        self.assertEqual([(row["id"], row["applicable"]) for row in data["profiles"]], [("nvidia-driver", True), ("desktop-plasma", False)])

    def test_detect_provision_without_an_inventory(self):
        from tests.helpers import temp_home

        with temp_home():
            result = call("detect_provision")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "INVENTORY_MISSING")

    def test_create_provision_plan_then_get_plan(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_provision_home(home)
            created = call("create_provision_plan", {"accept_licenses": ["NVIDIA-r2"]})
            shown = call("get_plan", {"plan_id": created.structured_content["plan_id"]})
        self.assertFalse(created.is_error, created.content[0].text)
        data = created.structured_content
        self.assertEqual(data["kind"], "provision")
        self.assertIn("Licenses accepted: NVIDIA-r2", data["summary"])
        self.assertEqual(data["disks"], [])
        self.assertEqual(data["provision"]["tuning"]["jobs"], 15)
        self.assertEqual(data["identity"]["hostname"], "gentoo")
        self.assertEqual(shown.structured_content["plan_hash"], data["plan_hash"])
        self.assertFalse(shown.structured_content["approval"]["approved"])

    def test_create_provision_plan_passes_package_use(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_provision_home(home)
            created = call("create_provision_plan", {"accept_licenses": ["NVIDIA-r2"],
                                                     "package_use": [">=media-libs/libglvnd-1.7.0 X"]})
        self.assertFalse(created.is_error, created.content[0].text)
        self.assertEqual(created.structured_content["provision"]["package_use"], [">=media-libs/libglvnd-1.7.0 X"])
        self.assertIn("USE changes: >=media-libs/libglvnd-1.7.0 X", created.structured_content["summary"])

    def test_create_provision_plan_refuses_missing_licenses(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_provision_home(home)
            result = call("create_provision_plan", {"accept_licenses": []})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")

    def test_create_provision_plan_for_the_desktop_needs_no_licenses(self):
        from gentoo_agent.core.config import save_json
        from tests.helpers import temp_home
        from tests.provision_helpers import desktop_inventory

        with temp_home() as home:
            write_provision_home(home)
            save_json(home / "inventory" / "provision-inventory-20261019T120000Z.json", desktop_inventory())
            created = call("create_provision_plan", {"profile": "desktop-plasma", "package_use": ["media-libs/a Y"]})
            refused = call("create_provision_plan", {"profile": "desktop-plasma", "accept_licenses": ["NVIDIA-r2"]})
        self.assertFalse(created.is_error, created.content[0].text)
        data = created.structured_content
        self.assertEqual(data["kind"], "provision")
        self.assertEqual(data["provision"]["plasma_profile"], "default/linux/amd64/23.0/desktop/plasma")
        self.assertIn("Purpose: desktop-plasma", data["summary"])
        self.assertIn("; media-libs/a Y", data["summary"])
        self.assertTrue(refused.is_error)
        self.assertEqual(refused.structured_content["code"], "CONFIG_INVALID")

    def test_the_nvidia_profile_without_licenses_is_still_refused(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_provision_home(home)
            result = call("create_provision_plan", {})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")

    def test_verify_install_runs_the_provision_checks(self):
        from gentoo_agent.core.install_plan import load_install_plan
        from tests.helpers import temp_home

        seen = []

        def fake_shell(target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
            seen.append(command)
            return {"label": label, "command": command, "returncode": 0, "stdout": "ok\n", "stderr": "",
                    "finished_at": "t"}

        with temp_home() as home, patch("gentoo_agent.mcp.server.remote_shell", fake_shell):
            write_provision_home(home)
            plan_id = call("create_provision_plan", {"accept_licenses": ["NVIDIA-r2"]}).structured_content["plan_id"]
            plan = load_install_plan(plan_id)
            result = call("verify_install", {"plan_id": plan_id})
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(seen, [phase["check"] for phase in plan["phases"]])
        self.assertEqual(len(result.structured_content["phases"]), 7)

    def test_verify_install_takes_the_expected_check_from_the_registry(self):
        from tests.helpers import temp_home

        def fake_shell(target, label, command, timeout=3600, stream=False, input_data=None, on_output=None):
            return {"label": label, "command": command, "returncode": 0, "stdout": "ok\n", "stderr": "",
                    "finished_at": "t"}

        with temp_home() as home, patch("gentoo_agent.mcp.server.remote_shell", fake_shell):
            write_provision_home(home)
            plan_id = call("create_provision_plan", {"accept_licenses": ["NVIDIA-r2"]}).structured_content["plan_id"]
            with patch("gentoo_agent.core.profiles.check_command_for", return_value="echo registry"):
                result = call("verify_install", {"plan_id": plan_id})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "PLAN_HASH_MISMATCH")

    def make_plan(self, home):
        write_provision_home(home)
        return call("create_provision_plan", {"accept_licenses": ["NVIDIA-r2"]}).structured_content["plan_id"]

    def test_approve_plan_dialog_for_a_provision_plan(self):
        from mcp.types import ElicitResult
        from gentoo_agent.core.approval import approval_status
        from tests.helpers import temp_home

        seen = []

        async def accept(context, params):
            seen.append(params.message)
            return ElicitResult(action="accept", content={"approve": True})

        with temp_home() as home:
            plan_id = self.make_plan(home)
            result = call("approve_plan", {"plan_id": plan_id}, elicitation_callback=accept)
            status = approval_status(plan_id)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertTrue(status["approved"])
        self.assertIn("Kind: provision", seen[0])
        self.assertNotIn("ERASE WINDOWS", seen[0])

    def test_approve_plan_without_elicitation_returns_the_terminal_command(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            plan_id = self.make_plan(home)
            result = call("approve_plan", {"plan_id": plan_id})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "APPROVAL_REQUIRED")
        self.assertEqual(result.structured_content["details"]["command"], f"gentoo-agent approve {plan_id}")

    def test_start_install_for_an_unapproved_provision_plan_needs_approval(self):
        from tests.helpers import temp_home

        with temp_home() as home, patch("gentoo_agent.jobs.launcher.subprocess.Popen") as popen:
            plan_id = self.make_plan(home)
            result = call("start_install", {"plan_id": plan_id})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["code"], "APPROVAL_REQUIRED")
        popen.assert_not_called()


@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class ApprovalQuestionTests(unittest.TestCase):
    def test_install_question_is_unchanged(self):
        self.assertEqual(
            mcp_server.approval_question({"kind": "install"}),
            "Approve this plan? The approval is valid for 60 minutes "
            "and covers exactly the commands and disk above. Check 'approve' and accept only if you, "
            "the human operator, agree.",
        )

    def test_provision_question_has_no_disk(self):
        text = mcp_server.approval_question({"kind": "provision"})
        self.assertNotIn("disk", text)
        self.assertIn("covers exactly the commands above and the host identity shown", text)


@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class ProvisionGuidanceTests(unittest.TestCase):
    def test_the_server_instructions_explain_provisioning_and_forbid_invented_licenses(self):
        text = mcp_server.server.instructions
        for name in ("detect_provision", "create_provision_plan", "verify_install"):
            self.assertIn(name, text)
        self.assertIn("never invent or guess license names or package.use lines", text)
        self.assertIn('profile="desktop-plasma"', text)
        self.assertNotIn("\u2014", text)

    def test_the_install_prompt_mentions_provisioning(self):
        async def go():
            async with Client(mcp_server.server, mode="legacy") as client:
                return await client.get_prompt("install_gentoo", {"request": ""})
        text = anyio.run(go).messages[0].content.text
        self.assertIn("create_provision_plan", text)
        self.assertIn("package.use lines", text)

    def test_the_provision_tool_description_does_not_overpromise_threads(self):
        async def go():
            async with Client(mcp_server.server, mode="legacy") as client:
                return await client.list_tools()
        tools = {tool.name: tool for tool in anyio.run(go).tools}
        description = " ".join(tools["create_provision_plan"].description.split())
        self.assertNotIn("all CPU threads", description)
        self.assertIn("parallel jobs derived from the CPU threads and the memory", description)
        self.assertIn("package_use lines come only from the human's emerge --pretend output", description)
