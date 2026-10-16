"""The MCP server as a real stdio process: starts, keeps stdout clean, answers."""

import importlib.util
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

from tests.helpers import temp_home

HAS_MCP = importlib.util.find_spec("mcp") is not None
REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(HAS_MCP, "needs the [mcp] extra")
class StdioSmokeTests(unittest.TestCase):
    def test_server_starts_over_stdio_and_answers(self):
        import anyio
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        from tests.test_mcp_server import EXPECTED_TOOLS

        with temp_home() as home:
            (home / "config").mkdir()
            shutil.copy(REPO / "config" / "plan.json", home / "config" / "plan.json")
            params = StdioServerParameters(
                command=sys.executable,
                args=["-P", "-m", "gentoo_agent.mcp.server"],
                env={**os.environ, "GENTOO_AGENT_HOME": str(home), "PYTHONPATH": str(REPO)},
                cwd=str(home),
            )

            async def go():
                with anyio.fail_after(30), open(os.devnull, "w", encoding="utf-8") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            phases = await session.call_tool("list_phases", {})
                            return tools, phases

            # The SDK client logs and skips stdout lines that are not JSON-RPC,
            # so stray output would not fail the calls below without this check.
            with self.assertNoLogs("mcp.client.stdio", level="WARNING"):
                tools, phases = anyio.run(go)
        self.assertEqual({tool.name for tool in tools.tools}, EXPECTED_TOOLS)
        self.assertFalse(phases.is_error)
        self.assertIn("preflight", json.dumps(phases.structured_content))


if __name__ == "__main__":
    unittest.main()
