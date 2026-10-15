"""Supervised Gentoo-over-SSH agent workflow tools."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from gentoo_agent.core.approval import approval_phrase, enable_execution
from gentoo_agent.core.config import install_config, load_plan, now, save_json, target_config
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.handbook import fetch_handbook
from gentoo_agent.core.inventory import collect_inventory, latest_inventory, record_inventory
from gentoo_agent.core.paths import APPROVAL_DIR
from gentoo_agent.core.phases import PHASES
from gentoo_agent.core.runner import execute, execution_gate, install_phrase, render_dry_run, select_phases
from gentoo_agent.core.state import load_state, state_path
from gentoo_agent.core.validate import validate_installation, validated_settings


def ask_password(label: str) -> str:
    first = getpass.getpass(f"New Gentoo {label} password: ")
    second = getpass.getpass(f"Repeat Gentoo {label} password: ")
    if not first or first != second:
        raise AgentError(CONFIG_INVALID, f"Passwords for {label} were empty or did not match.")
    return first


def print_credentials(credentials: dict[str, str]) -> None:
    if not credentials:
        return
    print("Generated credentials (save them now; they are not stored by the agent):")
    for username, password in credentials.items():
        print(f"  {username}: {password}")


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


def cmd_approve(args: argparse.Namespace) -> int:
    if args.confirm != "I_CONFIRM_TARGET_AND_PHASE":
        raise SystemExit("Refusing approval: use the exact confirmation phrase.")
    target = target_config()
    approval = {
        "target": target["name"],
        "phase": args.phase,
        "disk": args.disk,
        "approved_at": now(),
        "confirmation": args.confirm,
        "note": args.note,
    }
    path = APPROVAL_DIR / f"{target['name']}-{args.phase}.json"
    save_json(path, approval)
    print(path)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    target = target_config()
    install = install_config()
    plan = load_plan()
    validated_settings(target, install)
    if args.execute and args.dry_run:
        raise AgentError(CONFIG_INVALID, "Choose either --execute or --dry-run, not both.")
    if args.from_phase is None:
        print("Starting from the beginning; completed phases will be checked and skipped.")
    phases = select_phases(args.from_phase)
    if not args.execute:
        print(render_dry_run(target, install, phases))
        return 0
    execution_gate(target, install, plan, args.confirm)
    result = execute(
        target,
        install,
        phases,
        prompt=ask_password,
        on_event=lambda message: print(message, flush=True),
        on_output=lambda line: print(f"    {line}", end="", flush=True),
    )
    if result["status"] != "installed_pending_reboot":
        print(result["stderr"], file=sys.stderr, end="")
        print_credentials(result["generated_credentials"])
        return result["returncode"]
    print("Installation phases completed. The target was not rebooted.")
    print_credentials(result["generated_credentials"])
    print("Review the target through the console before rebooting it.")
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
    args.dry_run = True
    args.confirm = ""
    return cmd_install(args)


def cmd_approve_install(_: argparse.Namespace) -> int:
    target = target_config()
    install = install_config()
    plan = load_plan()
    expected = approval_phrase(target, install)
    print("Review target.json, install.json, the latest inventory, and the dry-run output first.")
    if input(f"Type {expected}: ").strip() != expected:
        raise SystemExit("Approval not recorded.")
    enable_execution(target, install, plan)
    print("Execution enabled in all three local configuration files.")
    print(f"Start with: python tools/gentoo_agent.py install --execute --confirm {install_phrase(target, install)!r}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("handbook", help="Fetch and record the configured Handbook URL").set_defaults(func=cmd_handbook)
    sub.add_parser("inspect", help="Run the allowlisted read-only SSH inventory").set_defaults(func=cmd_inspect)
    sub.add_parser("status", help="Print current local workflow state").set_defaults(func=cmd_status)
    sub.add_parser("validate", help="Validate installation settings without touching the target").set_defaults(func=cmd_validate)
    approve = sub.add_parser("approve", help="Create an explicit approval record")
    approve.add_argument("phase")
    approve.add_argument("--disk", required=True)
    approve.add_argument("--note", required=True)
    approve.add_argument("--confirm", required=True)
    approve.set_defaults(func=cmd_approve)
    phase = sub.add_parser("phase", help="Run an implemented phase")
    phase.add_argument("phase")
    phase.set_defaults(func=cmd_phase)
    install = sub.add_parser("install", help="Print or execute the guarded installation phases")
    install.add_argument("--execute", action="store_true", help="Execute after explicit approval; dry-run is the default")
    install.add_argument("--dry-run", action="store_true", help="Print generated phases without connecting (default)")
    install.add_argument("--confirm", default="", help="Exact confirmation phrase printed by approve-install")
    install.add_argument("--from-phase", choices=PHASES, default=None,
                         help="Explicit restart point; omitted means resume from the first incomplete phase")
    install.set_defaults(func=cmd_install)
    approve_install = sub.add_parser("approve-install", help="Approve the reviewed installation for execution")
    approve_install.set_defaults(func=cmd_approve_install)
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
