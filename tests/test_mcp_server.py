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


if __name__ == "__main__":
    unittest.main()
