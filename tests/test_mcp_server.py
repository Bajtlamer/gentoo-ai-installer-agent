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
    "verify_install",
}


def call(name, arguments=None, *, elicitation_callback=None):
    async def go():
        kwargs = {"mode": "legacy"}
        if elicitation_callback is not None:
            kwargs["elicitation_callback"] = elicitation_callback
        async with Client(mcp_server.server, **kwargs) as client:
            return await client.call_tool(name, arguments or {})
    return anyio.run(go)


def write_home(home, *, boot="uefi", disks=("/dev/nvme0n1",)):
    from gentoo_agent.core.config import save_json

    save_json(home / "config" / "target.json", {
        "name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root", "identity_file": "~/.ssh/id_test",
        "target_is_disposable": True, "disk_erase_authorized": True,
    })
    save_json(home / "config" / "plan.json", json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8")))
    save_json(home / "config" / "install.default.json",
              json.loads((REPO / "config" / "install.default.json").read_text(encoding="utf-8")))
    devices = [{"path": path, "type": "disk", "size": 512110190592, "model": "NVMe", "serial": f"S{i}", "wwn": None}
               for i, path in enumerate(disks)]
    save_json(home / "inventory" / "inventory-20261015T120000Z.json", {"target": "gentoo", "collected_at": "x", "results": [
        {"label": "boot_mode", "returncode": 0, "stdout": boot},
        {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": devices})},
    ]})


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
        self.assertIn("storage", ids)


@unittest.skipUnless(HAS_MCP, "mcp extra not installed")
class DiscoveryAndPlanningTests(unittest.TestCase):
    def test_detect_hardware_summarizes_the_latest_inventory(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            result = call("detect_hardware", {})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["disks"][0]["path"], "/dev/nvme0n1")

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

    def test_plain_text_password_is_refused(self):
        from tests.helpers import temp_home

        with temp_home() as home:
            write_home(home)
            result = call("create_plan", {"overrides": {"user_password": "hunter2"}})
        self.assertEqual(result.structured_content["code"], "CONFIG_INVALID")
        self.assertNotIn("hunter2", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
