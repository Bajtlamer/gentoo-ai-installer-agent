import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gentoo_agent import cli
from gentoo_agent.core import approval, paths, phases, runs
from gentoo_agent.core.config import save_json
from tests.helpers import fresh_inventory, identity_result, partitions_result, storage_result, temp_home
from tests.test_launcher import approved_plan


REPO = Path(__file__).resolve().parents[1]


class InstallCommandTests(unittest.TestCase):
    def test_install_execute_points_to_the_plan_workflow(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.main(["install", "--execute"])
        self.assertEqual(code, 1)
        self.assertIn("error [APPROVAL_REQUIRED]: install --execute was replaced", stderr.getvalue())
        self.assertIn("gentoo-agent plan create", stderr.getvalue())


class FakeTty:
    def __init__(self, answer):
        self.answer = answer
        self.written = []

    def write(self, text):
        self.written.append(text)

    def flush(self):
        pass

    def readline(self):
        return self.answer + "\n"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ApproveCommandTests(unittest.TestCase):
    def test_approve_refuses_without_a_terminal(self):
        stderr = io.StringIO()
        with temp_home():
            plan = approved_plan()
            approval.approval_path(plan["plan_id"]).unlink()
            with patch.object(cli, "TTY_PATH", "/nonexistent/tty"), redirect_stderr(stderr):
                code = cli.main(["approve", plan["plan_id"]])
            approved = approval.approval_path(plan["plan_id"]).exists()
        self.assertEqual(code, 1)
        self.assertIn("error [APPROVAL_REQUIRED]: approve needs an interactive terminal", stderr.getvalue())
        self.assertFalse(approved)

    def test_open_tty_without_o_noctty_reports_approval_required(self):
        def refuse(*args):
            raise OSError("no terminal")

        windows_os = SimpleNamespace(open=refuse, O_RDWR=os.O_RDWR, close=os.close)
        with patch.object(cli, "os", windows_os):
            with self.assertRaises(cli.AgentError) as caught:
                cli.open_tty()
        self.assertEqual(caught.exception.code, "APPROVAL_REQUIRED")

    def test_approve_with_the_exact_phrase_records_approval(self):
        with temp_home():
            plan = approved_plan()
            approval.approval_path(plan["plan_id"]).unlink()
            tty = FakeTty(f"APPROVE {plan['plan_id']}")
            with patch.object(cli, "open_tty", return_value=tty), redirect_stdout(io.StringIO()):
                code = cli.main(["approve", plan["plan_id"]])
            status = approval.approval_status(plan["plan_id"])
        self.assertEqual(code, 0)
        self.assertTrue(status["approved"])
        self.assertEqual(status["channel"], "cli")
        self.assertIn(plan["plan_hash"], "".join(tty.written))

    def test_wrong_phrase_records_nothing(self):
        stderr = io.StringIO()
        with temp_home():
            plan = approved_plan()
            approval.approval_path(plan["plan_id"]).unlink()
            with patch.object(cli, "open_tty", return_value=FakeTty("yes")), redirect_stderr(stderr):
                code = cli.main(["approve", plan["plan_id"]])
            approved = approval.approval_path(plan["plan_id"]).exists()
        self.assertEqual(code, 1)
        self.assertIn("APPROVAL_DECLINED", stderr.getvalue())
        self.assertFalse(approved)


class PlanAndRunCommandTests(unittest.TestCase):
    def test_plan_create_saves_a_plan_from_the_config(self):
        with temp_home() as home:
            config = home / "config"
            install = json.loads((REPO / "tests" / "golden" / "configs" / "bios_mbr_user_sudo.json").read_text(encoding="utf-8"))
            install.update({"disk_must_be_erased": True, "target_is_disposable": True, "disk_erase_authorized": True})
            save_json(config / "install.json", install)
            save_json(config / "target.json", {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
                                               "identity_file": "~/.ssh/id_test", "target_is_disposable": True,
                                               "disk_erase_authorized": True})
            save_json(config / "plan.json", json.loads((REPO / "config" / "plan.json").read_text(encoding="utf-8")))
            stdout = json.dumps({"blockdevices": [{"path": "/dev/sda", "type": "disk", "size": 42949672960,
                                                   "model": "QEMU HARDDISK",
                                                   "serial": "s1", "wwn": None}]})
            save_json(home / "inventory" / "inventory-20261015T120000Z.json",
                      fresh_inventory({"label": "boot_mode", "returncode": 0, "stdout": "bios-or-unknown"},
                                      storage_result("/dev/sda"),
                                      {"label": "disk_identity", "returncode": 0, "stdout": stdout},
                                      identity_result("x86_64"),
                                      partitions_result("/dev/sda")))
            output = io.StringIO()
            with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAA test"), redirect_stdout(output):
                code = cli.main(["plan", "create"])
            saved = list(paths.plans_dir().glob("*.json"))
        self.assertEqual(code, 0)
        self.assertEqual(len(saved), 1)
        self.assertIn(f"gentoo-agent approve {saved[0].stem}", output.getvalue())
        self.assertIn("serial=s1", output.getvalue())

    def test_run_status_shows_generated_credentials_once(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": "a" * 16, "status": "failed", "pid": None,
                                    "started_at": runs.now()})
            runs.write_credentials(run_id, {"credentials": {"root": "s3cret-value"}, "applied": True})
            first, second = io.StringIO(), io.StringIO()
            with redirect_stdout(first):
                cli.main(["run", "status", run_id])
            with redirect_stdout(second):
                cli.main(["run", "status", run_id])
        self.assertIn("s3cret-value", first.getvalue())
        self.assertNotIn("s3cret-value", second.getvalue())


class RealTerminalTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "needs a POSIX pty")
    def test_open_tty_reads_a_line_on_a_real_terminal(self):
        import pty

        pid, master = pty.fork()
        if pid == 0:
            code = 4
            try:
                with cli.open_tty() as tty:
                    tty.write("prompt: ")
                    tty.flush()
                    code = 0 if tty.readline().strip() == "hello" else 3
            except BaseException:
                code = 5
            os._exit(code)
        os.write(master, b"hello\n")
        output = b""
        try:
            while True:
                chunk = os.read(master, 1024)
                if not chunk:
                    break
                output += chunk
        except OSError:
            pass
        _, status = os.waitpid(pid, 0)
        os.close(master)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0, output)
        self.assertIn(b"prompt:", output)


class EdgeCaseTests(unittest.TestCase):
    def test_approve_eof_is_declined(self):
        stderr = io.StringIO()
        with temp_home():
            plan = approved_plan()
            approval.approval_path(plan["plan_id"]).unlink()
            tty = FakeTty("")
            tty.readline = lambda: ""
            with patch.object(cli, "open_tty", return_value=tty), redirect_stderr(stderr):
                code = cli.main(["approve", plan["plan_id"]])
            approved = approval.approval_path(plan["plan_id"]).exists()
        self.assertEqual(code, 1)
        self.assertIn("APPROVAL_DECLINED", stderr.getvalue())
        self.assertFalse(approved)

    def test_ask_password_needs_a_terminal(self):
        with patch.object(cli.sys, "stdin", io.StringIO("")):
            with self.assertRaises(cli.AgentError) as caught:
                cli.ask_password("root")
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.assertIn("password_source 'prompt' needs an interactive terminal.", caught.exception.message)

    def test_ask_password_converts_eof(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        with patch.object(cli.sys, "stdin", Tty("")), patch.object(cli.getpass, "getpass", side_effect=EOFError):
            with self.assertRaises(cli.AgentError) as caught:
                cli.ask_password("root")
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def _status_output(self, status, applied):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": "a" * 16, "status": status, "pid": None,
                                    "started_at": runs.now()})
            runs.write_credentials(run_id, {"credentials": {"root": "pw"}, "applied": applied})
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main(["run", "status", run_id])
        return out.getvalue()

    def test_status_wording_for_failed_run_with_applied_credentials(self):
        text = self._status_output("failed", True)
        self.assertIn("may have been applied (the authentication phase was attempted or the worker stopped early)", text)
        self.assertNotIn("were applied", text)

    def test_status_wording_for_installed_run(self):
        self.assertIn("were applied", self._status_output("installed_pending_reboot", True))

    def test_status_wording_when_not_applied(self):
        self.assertIn("were generated but the authentication phase did not run", self._status_output("failed", False))

    def test_status_wording_when_applied_is_pending(self):
        text = self._status_output("interrupted", None)
        self.assertIn("may have been applied (the authentication phase was attempted or the worker stopped early)", text)
        self.assertNotIn("did not run", text)


class DenyRuleTests(unittest.TestCase):
    def test_deny_rules_use_edit_forms_only_for_paths(self):
        settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        for directory in ("approvals", "plans"):
            self.assertIn(f"Edit(/{directory}/**)", deny)
            # Unanchored forms would also match nested dirs such as docs/superpowers/plans/.
            self.assertNotIn(f"Edit({directory}/**)", deny)
            self.assertNotIn(f"Edit(./{directory}/**)", deny)
        self.assertFalse([rule for rule in deny if rule.startswith("Write(")])

    def test_agent_may_not_run_approve_or_write_approval_files(self):
        settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        for rule in (
            "Bash(gentoo-agent approve:*)",
            "Bash(python -m gentoo_agent approve:*)",
            "Bash(python3 -m gentoo_agent approve:*)",
            "Bash(python tools/gentoo_agent.py approve:*)",
            "Bash(python3 tools/gentoo_agent.py approve:*)",
            "Edit(/approvals/**)",
            "Edit(/plans/**)",
        ):
            self.assertIn(rule, deny)


if __name__ == "__main__":
    unittest.main()
