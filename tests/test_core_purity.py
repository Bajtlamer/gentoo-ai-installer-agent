"""core/ must stay usable from the MCP server: no printing, prompting, or exiting."""

import ast
import unittest
from pathlib import Path


CORE = Path(__file__).resolve().parents[1] / "gentoo_agent" / "core"
FORBIDDEN_CALLS = {"print", "input", "exit", "quit", "breakpoint"}
FORBIDDEN_NAMES = {"SystemExit"}
FORBIDDEN_MODULES = {"getpass", "argparse", "sys", "builtins", "pdb"}
FORBIDDEN_ATTRS = {"_exit", "abort"}


MCP = Path(__file__).resolve().parents[1] / "gentoo_agent" / "mcp"


def violations_in(path: Path, base: Path | None = None, *, check_imports: bool = True) -> list[str]:
    found = []
    name = path.relative_to(base).as_posix() if base else path.name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            found.append(f"{name}:{line} calls {node.func.id}()")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{name}:{line} uses {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            found.append(f"{name}:{line} uses {node.attr}")
        if check_imports and isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    found.append(f"{name}:{line} imports {alias.name}")
        if isinstance(node, ast.ImportFrom):
            if check_imports and (node.module or "").split(".")[0] in FORBIDDEN_MODULES:
                found.append(f"{name}:{line} imports from {node.module}")
            for alias in node.names:
                if alias.name in FORBIDDEN_ATTRS:
                    found.append(f"{name}:{line} uses {alias.name}")
    return found


BYPASS_SAMPLE = """\
import builtins
import sys as s
import pdb
from os import _exit
from sys import exit
builtins.print('x')
getattr(s, "exit")()
os._exit(1)
os.abort()
s.exit()
s.stdout.write('x')
s.stderr.write('x')
s.stdin.readline()
breakpoint()
"""


class CorePurityTests(unittest.TestCase):
    def test_core_modules_do_not_print_prompt_or_exit(self):
        violations = []
        for path in sorted(CORE.rglob("*.py")):
            violations += violations_in(path, CORE)
        self.assertEqual(violations, [])

    def test_mcp_package_never_prints_or_exits(self):
        # stdout carries the MCP protocol; module-import rules do not apply here.
        violations = []
        for path in sorted(MCP.glob("*.py")):
            violations += violations_in(path, MCP, check_imports=False)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr == "exit"
                        and isinstance(node.value, ast.Name) and node.value.id == "sys"):
                    violations.append(f"{path.name}:{node.lineno} calls sys.exit")
        self.assertEqual(violations, [])

    def test_guard_detects_a_violation(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.py"
            sample.write_text("import getpass\nprint('x')\nraise SystemExit(1)\n", encoding="utf-8")
            self.assertEqual(len(violations_in(sample)), 3)

    def test_guard_detects_known_bypasses(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "bypass.py"
            sample.write_text(BYPASS_SAMPLE, encoding="utf-8")
            found = "\n".join(violations_in(sample))
        for needle in (
            "bypass.py:1 imports builtins",
            "bypass.py:2 imports sys",
            "bypass.py:3 imports pdb",
            "bypass.py:4 uses _exit",
            "bypass.py:5 imports from sys",
            "bypass.py:8 uses _exit",
            "bypass.py:9 uses abort",
            "bypass.py:14 calls breakpoint()",
        ):
            self.assertIn(needle, found)


if __name__ == "__main__":
    unittest.main()
