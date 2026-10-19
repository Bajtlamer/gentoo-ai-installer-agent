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
from tests.helpers import (
    fresh_inventory,
    identity_result,
    partitions_result,
    save_dual_boot_plan,
    storage_result,
    temp_home,
    windows_disk,
)
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


class EraseWindowsApproveTests(unittest.TestCase):
    def approve(self, answer_for) -> tuple[int, dict, dict, FakeTty]:
        with temp_home():
            plan = save_dual_boot_plan(nvme=windows_disk("/dev/nvme0n1"))
            tty = FakeTty(answer_for(plan["plan_id"]))
            with patch.object(cli, "open_tty", return_value=tty), redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()):
                code = cli.main(["approve", plan["plan_id"]])
            status = approval.approval_status(plan["plan_id"])
        return code, plan, status, tty

    def test_extended_phrase_is_asked_for_and_recorded(self):
        code, plan, status, tty = self.approve(lambda plan_id: f"APPROVE {plan_id} ERASE WINDOWS /dev/nvme0n1")
        self.assertEqual(code, 0)
        self.assertTrue(status["approved"])
        written = "".join(tty.written)
        self.assertTrue(written.startswith("WARNING: /dev/nvme0n1 contains Windows"))
        self.assertIn(f"Type APPROVE {plan['plan_id']} ERASE WINDOWS /dev/nvme0n1 to approve", written)

    def test_ordinary_or_wrong_phrase_records_nothing(self):
        for answer in (lambda plan_id: f"APPROVE {plan_id}",
                       lambda plan_id: f"APPROVE {plan_id} ERASE WINDOWS /dev/sda",
                       lambda plan_id: f"APPROVE {plan_id} ERASE /dev/nvme0n1"):
            with self.subTest(answer=answer("ID")):
                code, _, status, _ = self.approve(answer)
                self.assertEqual(code, 1)
                self.assertFalse(status["approved"])


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


class ProvisionCommandTests(unittest.TestCase):
    def write_home(self, home):
        save_json(home / "config" / "target.json", {
            "name": "gentoo-server2", "host": "192.0.2.10", "port": 22, "user": "root",
            "identity_file": "~/.ssh/id_test", "target_is_disposable": False, "disk_erase_authorized": False})
        from tests.provision_helpers import provision_inventory
        save_json(home / "inventory" / "provision-inventory-20261019T120000Z.json", provision_inventory())

    def test_provision_plan_saves_a_plan_and_prints_the_summary(self):
        stdout = io.StringIO()
        with temp_home() as home:
            self.write_home(home)
            with redirect_stdout(stdout):
                code = cli.main(["provision", "plan", "--accept-license", "NVIDIA-r2"])
            plans = list((home / "plans").glob("*.json"))
        self.assertEqual(code, 0)
        self.assertEqual(len(plans), 1)
        self.assertIn("Kind: provision", stdout.getvalue())
        self.assertIn("Licenses accepted: NVIDIA-r2", stdout.getvalue())
        self.assertIn(f"gentoo-agent approve {plans[0].stem}", stdout.getvalue())

    def test_provision_plan_takes_repeatable_package_use_lines(self):
        stdout = io.StringIO()
        with temp_home() as home:
            self.write_home(home)
            with redirect_stdout(stdout):
                code = cli.main(["provision", "plan", "--accept-license", "NVIDIA-r2",
                                 "--package-use", ">=media-libs/libglvnd-1.7.0 X", "--package-use", "media-libs/a Y"])
        self.assertEqual(code, 0)
        self.assertIn("USE changes: >=media-libs/libglvnd-1.7.0 X; media-libs/a Y", stdout.getvalue())

    def test_provision_plan_needs_a_license(self):
        stderr = io.StringIO()
        with temp_home() as home, redirect_stderr(stderr):
            self.write_home(home)
            with self.assertRaises(SystemExit) as caught:
                cli.main(["provision", "plan"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_provision_plan_without_an_inventory_reports_the_core_error(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with temp_home() as home:
            self.write_home(home)
            for path in (home / "inventory").glob("provision-inventory-*.json"):
                path.unlink()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(["provision", "plan", "--accept-license", "NVIDIA-r2"])
            plans = list((home / "plans").glob("*.json"))
        self.assertEqual(code, 1)
        self.assertIn("error [INVENTORY_MISSING]:", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(plans, [])

    def test_provision_inspect_prints_the_summary_and_the_inventory_path(self):
        from tests.provision_helpers import provision_inventory

        stdout = io.StringIO()
        with temp_home() as home:
            self.write_home(home)
            fake = (provision_inventory(), home / "inventory" / "provision-inventory-20261019T120000Z.json")
            with patch.object(cli, "collect_provision_inventory", return_value=fake), redirect_stdout(stdout):
                code = cli.main(["provision", "inspect"])
        self.assertEqual(code, 0)
        self.assertIn('"threads": 24', stdout.getvalue())
        self.assertIn("provision-inventory-20261019T120000Z.json", stdout.getvalue())

    def test_provision_inspect_sends_progress_to_stderr_only(self):
        from tests.provision_helpers import provision_inventory

        stdout = io.StringIO()
        stderr = io.StringIO()
        with temp_home() as home:
            self.write_home(home)
            path = home / "inventory" / "provision-inventory-20261019T120000Z.json"

            def fake(target, on_progress=None):
                on_progress("cpu")
                return provision_inventory(), path

            with patch.object(cli, "collect_provision_inventory", side_effect=fake), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(["provision", "inspect"])
        self.assertEqual(code, 0)
        self.assertIn("Inspecting cpu...", stderr.getvalue())
        self.assertNotIn("Inspecting", stdout.getvalue())
        lines = stdout.getvalue().rstrip("\n").split("\n")
        self.assertEqual(lines[-1], str(path))
        json.loads("\n".join(lines[:-1]))


if __name__ == "__main__":
    unittest.main()
