"""Supervised Gentoo-over-SSH agent workflow tools."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys


from gentoo_agent.core.config import install_config, load_json, load_plan, now, save_json, target_config
from gentoo_agent.core.errors import AgentError
from gentoo_agent.core.handbook import fetch_handbook
from gentoo_agent.core.inventory import collect_inventory, latest_inventory, record_inventory
from gentoo_agent.core.paths import APPROVAL_DIR, CONFIG, STATE_DIR
from gentoo_agent.core.validate import validate_installation


def cmd_handbook(_: argparse.Namespace) -> int:
    print(json.dumps(fetch_handbook(target_config()), indent=2))
    return 0


def cmd_inspect(_: argparse.Namespace) -> int:
    target = target_config()
    _, path = collect_inventory(target, on_progress=lambda label: print(f"Inspecting {label}...", file=sys.stderr))
    record_inventory(target, path)
    print(path)
    return 0


def cmd_status(_: argparse.Namespace) -> None:
    state_path = STATE_DIR / "state.json"
    if not state_path.exists():
        print("No state/state.json exists. Copy state.example.json first.")
        return
    print(json.dumps(load_json(state_path), indent=2))


def cmd_validate(_: argparse.Namespace) -> int:
    result = validate_installation(target_config(), install_config(), load_plan(), latest_inventory())
    print(json.dumps(result, indent=2))
    return 2 if result["errors"] else 0


def cmd_approve(args: argparse.Namespace) -> None:
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


def cmd_phase(args: argparse.Namespace) -> None:
    plan = load_json(CONFIG / "plan.json")
    phase = next((item for item in plan["phases"] if item["id"] == args.phase), None)
    if phase is None:
        raise SystemExit(f"Unknown phase: {args.phase}")
    if not phase.get("implemented"):
        raise SystemExit(
            f"Phase '{args.phase}' is not implemented. No target changes were made."
        )
    if args.phase == "discovery":
        cmd_inspect(args)
        return
    from gentoo_agent.core.phases import cmd_install
    args.from_phase = args.phase
    args.execute = False
    args.dry_run = True
    args.confirm = ""
    cmd_install(args)


def cmd_install_command(args: argparse.Namespace) -> int:
    from gentoo_agent.core.phases import cmd_install
    return cmd_install(args)


def cmd_approve_install(args: argparse.Namespace) -> None:
    from gentoo_agent.core.phases import approve_execution
    approve_execution(args)


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
    install.add_argument("--from-phase", choices=[
        "preflight", "storage", "filesystems", "base-system", "configuration", "authentication",
        "kernel", "bootloader", "validation",
    ], default=None, help="Explicit restart point; omitted means resume from the first incomplete phase")
    install.set_defaults(func=cmd_install_command)
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
