import json
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class OperatorProfileTests(unittest.TestCase):
    def test_sandbox_profile(self):
        profile = json.loads((REPO / "docs" / "operator" / "claude-settings.json").read_text(encoding="utf-8"))
        sandbox = profile["sandbox"]
        self.assertTrue(sandbox["enabled"])
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])
        for path in ("./approvals", "./plans", "./runs", "./secrets", "./config", "./inventory",
                     "./.mcp.json", "./.claude"):
            self.assertIn(path, sandbox["filesystem"]["denyWrite"])
        for path in ("./secrets", "./runs"):
            self.assertIn(path, sandbox["filesystem"]["denyRead"])
        deny = profile["permissions"]["deny"]
        for rule in ("Read(/secrets/**)", "Edit(/approvals/**)", "Edit(/plans/**)", "Edit(/runs/**)",
                     "Edit(/config/**)", "Edit(/inventory/**)", "Edit(/.mcp.json)", "Edit(/.claude/**)"):
            self.assertIn(rule, deny)
        self.assertFalse(any(rule.startswith("Write(") for rule in deny))
        self.assertFalse(any(rule in deny for rule in ("Edit(plans/**)", "Edit(approvals/**)")))

    def test_project_settings_keep_secrets_and_credentials_away_from_file_tools(self):
        deny = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["deny"]
        for rule in ("Read(/secrets/**)", "Read(/runs/*/credentials.json)", "Read(/runs/*/.credentials.*)",
                     "Edit(/runs/**)", "Edit(/secrets/**)"):
            self.assertIn(rule, deny)

    def test_mcp_example(self):
        example = json.loads((REPO / ".mcp.json.example").read_text(encoding="utf-8"))
        server = example["mcpServers"]["gentoo-agent"]
        self.assertEqual(server["type"], "stdio")
        self.assertIn("GENTOO_AGENT_HOME", server["env"])


if __name__ == "__main__":
    unittest.main()
