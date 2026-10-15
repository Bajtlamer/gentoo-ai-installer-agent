"""core/ must stay usable from the MCP server: no printing, prompting, or exiting."""

import ast
import unittest
from pathlib import Path


CORE = Path(__file__).resolve().parents[1] / "gentoo_agent" / "core"
FORBIDDEN_CALLS = {"print", "input", "exit", "quit"}
FORBIDDEN_NAMES = {"SystemExit"}
FORBIDDEN_MODULES = {"getpass", "argparse"}


def violations_in(path: Path) -> list[str]:
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            found.append(f"{path.name}:{line} calls {node.func.id}()")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{path.name}:{line} uses {node.id}")
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "exit"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        ):
            found.append(f"{path.name}:{line} calls sys.exit")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES:
                    found.append(f"{path.name}:{line} imports {alias.name}")
        if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_MODULES:
            found.append(f"{path.name}:{line} imports from {node.module}")
    return found


class CorePurityTests(unittest.TestCase):
    def test_core_modules_do_not_print_prompt_or_exit(self):
        violations = []
        for path in sorted(CORE.glob("*.py")):
            violations += violations_in(path)
        self.assertEqual(violations, [])

    def test_guard_detects_a_violation(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.py"
            sample.write_text("import getpass\nprint('x')\nraise SystemExit(1)\n", encoding="utf-8")
            self.assertEqual(len(violations_in(sample)), 3)


if __name__ == "__main__":
    unittest.main()
