"""Supervised Gentoo-over-SSH agent workflow tools."""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import sys

from gentoo_agent.core.config import install_config, load_plan, target_config
from gentoo_agent.core.approval import (
    APPROVAL_TTL_MINUTES,
    approval_phrase,
    approval_status,
    approval_summary,
    erase_phrase,
    record_approval,
)
from gentoo_agent.core.errors import APPROVAL_DECLINED, APPROVAL_REQUIRED, CONFIG_INVALID, AgentError
from gentoo_agent.core.handbook import fetch_handbook
from gentoo_agent.core.inventory import (
    collect_inventory, collect_provision_inventory, latest_inventory, latest_provision_inventory, record_inventory,
)
from gentoo_agent.core.install_plan import create_install_plan, load_install_plan, save_install_plan
from gentoo_agent.core.phases import PHASES
from gentoo_agent.core.profiles import provision_overview
from gentoo_agent.core.provision_plan import create_provision_plan
from gentoo_agent.core.runner import render_dry_run, select_phases
from gentoo_agent.core.runs import ACTIVE_STATUSES, read_log, run_status, take_credentials
from gentoo_agent.core.state import load_state, state_path
from gentoo_agent.core.validate import validate_installation, validated_settings
from gentoo_agent.jobs.launcher import cancel_run, resume_run, start_run


EXECUTE_REPLACED = (
    "install --execute was replaced by approved plans: run 'gentoo-agent plan create', "
    "approve the plan yourself in a terminal with 'gentoo-agent approve <plan_id>', "
    "then 'gentoo-agent run start <plan_id>'."
)


TTY_PATH = "/dev/tty"
NO_TERMINAL = (
    "approve needs an interactive terminal; run it yourself in your own terminal."
)
PASSWORD_NEEDS_TERMINAL = "password_source 'prompt' needs an interactive terminal."


def open_tty():
    try:
        fd = os.open(TTY_PATH, os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    except OSError as exc:
        raise AgentError(APPROVAL_REQUIRED, NO_TERMINAL) from exc
    try:
        handle = io.TextIOWrapper(io.FileIO(fd, "r+"), encoding="utf-8", write_through=True)
    except Exception:
        os.close(fd)
        raise
    if not handle.isatty():
        handle.close()
        raise AgentError(APPROVAL_REQUIRED, NO_TERMINAL)
    return handle


def ask_password(label: str) -> str:
    if not sys.stdin.isatty():
        raise AgentError(CONFIG_INVALID, PASSWORD_NEEDS_TERMINAL)
    try:
        first = getpass.getpass(f"New Gentoo {label} password: ")
        second = getpass.getpass(f"Repeat Gentoo {label} password: ")
    except EOFError as exc:
        raise AgentError(CONFIG_INVALID, PASSWORD_NEEDS_TERMINAL) from exc
    if not first or first != second:
        raise AgentError(CONFIG_INVALID, f"Passwords for {label} were empty or did not match.")
    return first


def cmd_plan_create(_: argparse.Namespace) -> int:
    plan = save_install_plan(create_install_plan(target_config(), install_config(), load_plan(), latest_inventory()))
    print(approval_summary(plan))
    print(f"\nPlan saved: {plan['plan_id']}")
    print(f"Approve it yourself in a terminal: gentoo-agent approve {plan['plan_id']}")
    return 0


def cmd_plan_show(args: argparse.Namespace) -> int:
    plan = load_install_plan(args.plan_id)
    print(approval_summary(plan))
    print(json.dumps({"approval": approval_status(args.plan_id)}, indent=2))
    return 0


def cmd_provision_inspect(_: argparse.Namespace) -> int:
    target = target_config()
    inventory, path = collect_provision_inventory(
        target, on_progress=lambda label: print(f"Inspecting {label}...", file=sys.stderr))
    print(json.dumps(provision_overview(inventory), indent=2))
    print(path)
    return 0


def cmd_provision_plan(args: argparse.Namespace) -> int:
    settings = {"profile": args.profile, "accept_licenses": args.accept_license,
                "package_use": args.package_use or []}
    plan = save_install_plan(create_provision_plan(target_config(), settings, latest_provision_inventory()))
    print(approval_summary(plan))
    print(f"\nPlan saved: {plan['plan_id']}")
    print(f"Approve it yourself in a terminal: gentoo-agent approve {plan['plan_id']}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    plan = load_install_plan(args.plan_id)
    phrase = approval_phrase(plan)
    with open_tty() as tty:
        tty.write(approval_summary(plan) + "\n\n")
        tty.write(f"Type {phrase} to approve this plan for {APPROVAL_TTL_MINUTES} minutes: ")
        tty.flush()
        answer = tty.readline().strip()
    if answer != phrase:
        raise AgentError(APPROVAL_DECLINED, "Approval not recorded.", {"plan_id": plan["plan_id"]})
    # The phrase already contains ERASE WINDOWS <disk> when the plan erases Windows.
    record = record_approval(plan, "cli", erase_confirmed=erase_phrase(plan) is not None)
    print(f"Approved until {record['expires_at']}. Start with: gentoo-agent run start {plan['plan_id']}")
    return 0


def cmd_run_start(args: argparse.Namespace) -> int:
    run_id = start_run(args.plan_id, prompt=ask_password)
    print(f"Run started: {run_id}")
    print(f"Follow it with: gentoo-agent run status {run_id}  and  gentoo-agent run log {run_id}")
    return 0


def cmd_run_status(args: argparse.Namespace) -> int:
    record = run_status(args.run_id)
    print(json.dumps(record, indent=2))
    if record["status"] not in ACTIVE_STATUSES:
        credentials = take_credentials(args.run_id)
        if credentials:
            applied = credentials.get("applied")
            if applied is False:
                state = "were generated but the authentication phase did not run"
            elif applied is True and record["status"] == "installed_pending_reboot":
                state = "were applied"
            else:  # applied, or None: pending because the worker stopped before recording it
                state = "may have been applied (the authentication phase was attempted or the worker stopped early)"
            print(f"Generated credentials ({state}; shown once, save them now):")
            for username, password in credentials["credentials"].items():
                print(f"  {username}: {password}")
    return 0


def cmd_run_log(args: argparse.Namespace) -> int:
    print(json.dumps(read_log(args.run_id, args.offset, args.limit), indent=2))
    return 0


def cmd_run_cancel(args: argparse.Namespace) -> int:
    print(json.dumps(cancel_run(args.run_id), indent=2))
    return 0


def cmd_run_resume(args: argparse.Namespace) -> int:
    run_id = resume_run(args.run_id, prompt=ask_password)
    print(f"Run started: {run_id} (resuming {args.run_id})")
    return 0


def cmd_handbook(_: argparse.Namespace) -> int:
    print(json.dumps(fetch_handbook(target_config()), indent=2))
    return 0


def cmd_inspect(_: argparse.Namespace) -> int:
    target = target_config()
    _, path = collect_inventory(target, on_progress=lambda label: print(f"Inspecting {label}...", file=sys.stderr))
    record_inventory(target, path)
    print(path)
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    if not state_path().exists():
        print("No state/state.json exists. Copy state.example.json first.")
        return 0
    print(json.dumps(load_state(), indent=2))
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    result = validate_installation(target_config(), install_config(), load_plan(), latest_inventory())
    print(json.dumps(result, indent=2))
    return 2 if result["errors"] else 0


def cmd_install(args: argparse.Namespace) -> int:
    if args.execute:
        raise AgentError(APPROVAL_REQUIRED, EXECUTE_REPLACED)
    target = target_config()
    install = install_config()
    validated_settings(target, install)
    if args.from_phase is None:
        print("Starting from the beginning; completed phases will be checked and skipped.")
    print(render_dry_run(target, install, select_phases(args.from_phase)))
    return 0


def cmd_phase(args: argparse.Namespace) -> int:
    plan = load_plan()
    phase = next((item for item in plan["phases"] if item["id"] == args.phase), None)
    if phase is None:
        raise AgentError(CONFIG_INVALID, f"Unknown phase: {args.phase}")
    if not phase.get("implemented"):
        raise AgentError(CONFIG_INVALID, f"Phase '{args.phase}' is not implemented. No target changes were made.")
    if args.phase == "discovery":
        return cmd_inspect(args)
    args.from_phase = args.phase
    args.execute = False
    return cmd_install(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("handbook", help="Fetch and record the configured Handbook URL").set_defaults(func=cmd_handbook)
    sub.add_parser("inspect", help="Run the allowlisted read-only SSH inventory").set_defaults(func=cmd_inspect)
    sub.add_parser("status", help="Print current local workflow state").set_defaults(func=cmd_status)
    sub.add_parser("validate", help="Validate installation settings without touching the target").set_defaults(func=cmd_validate)
    phase = sub.add_parser("phase", help="Print the dry run of one implemented phase")
    phase.add_argument("phase")
    phase.set_defaults(func=cmd_phase)
    install = sub.add_parser("install", help="Print the generated installation phases (dry run)")
    install.add_argument("--execute", action="store_true", help="Replaced by plan create, approve, and run start")
    install.add_argument("--dry-run", action="store_true", help="Print generated phases without connecting (default)")
    install.add_argument("--from-phase", choices=PHASES, default=None, help="First phase to print")
    install.set_defaults(func=cmd_install)
    plan = sub.add_parser("plan", help="Create or show an install plan")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    plan_sub.add_parser("create", help="Freeze the current config, disk identity, and commands into a plan").set_defaults(func=cmd_plan_create)
    plan_show = plan_sub.add_parser("show", help="Show a plan and its approval status")
    plan_show.add_argument("plan_id")
    plan_show.set_defaults(func=cmd_plan_show)
    provision = sub.add_parser("provision", help="Inspect and plan changes to an installed, running system")
    provision_sub = provision.add_subparsers(dest="provision_command", required=True)
    provision_sub.add_parser(
        "inspect", help="Run the allowlisted read-only SSH inventory of the running system"
    ).set_defaults(func=cmd_provision_inspect)
    provision_plan = provision_sub.add_parser("plan", help="Freeze a provision plan for approval")
    provision_plan.add_argument("--profile", default="nvidia-driver")
    provision_plan.add_argument("--accept-license", action="append", required=True,
                                help="A license the plan accepts (repeat for several)")
    provision_plan.add_argument("--package-use", action="append", metavar="LINE",
                                help="A package.use line from emerge --pretend output, e.g. "
                                     "'>=media-libs/libglvnd-1.7.0 X' (repeat for several)")
    provision_plan.set_defaults(func=cmd_provision_plan)
    approve = sub.add_parser("approve", help="Approve a plan (interactive terminal only)")
    approve.add_argument("plan_id")
    approve.set_defaults(func=cmd_approve)
    run = sub.add_parser("run", help="Start and follow background runs of approved plans")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    run_start = run_sub.add_parser("start", help="Start an approved plan in the background")
    run_start.add_argument("plan_id")
    run_start.set_defaults(func=cmd_run_start)
    for name, func, help_text in (
        ("status", cmd_run_status, "Show a run's status (and generated credentials once)"),
        ("cancel", cmd_run_cancel, "Stop a run at the next phase boundary"),
        ("resume", cmd_run_resume, "Start a new run of the same plan; completed phases are skipped"),
    ):
        command = run_sub.add_parser(name, help=help_text)
        command.add_argument("run_id")
        command.set_defaults(func=func)
    run_log = run_sub.add_parser("log", help="Print part of a run's log")
    run_log.add_argument("run_id")
    run_log.add_argument("--offset", type=int, default=0)
    run_log.add_argument("--limit", type=int, default=50)
    run_log.set_defaults(func=cmd_run_log)
    return parser


def main(argv: list[str] | None = None) -> int:
    parsed = build_parser().parse_args(argv)
    try:
        result = parsed.func(parsed)
    except AgentError as error:
        print(f"error [{error.code}]: {error.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
