"""Supervised Gentoo-over-SSH agent workflow tools."""

from __future__ import annotations

import argparse
import datetime as dt
import html.parser
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import urllib.request


from gentoo_agent.core.config import install_config, load_json, now, save_json, target_config
from gentoo_agent.core.errors import AgentError
from gentoo_agent.core.paths import APPROVAL_DIR, CONFIG, INVENTORY_DIR, ROOT, STATE_DIR
from gentoo_agent.core.ssh import READ_ONLY_COMMANDS, run_ssh_readonly


class TitleParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def cmd_handbook(_: argparse.Namespace) -> None:
    target = target_config()
    url = target["handbook_url"]
    request = urllib.request.Request(url, headers={"User-Agent": "gentoo-agent/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    html_path = INVENTORY_DIR / f"handbook-{stamp}.html"
    meta_path = INVENTORY_DIR / f"handbook-{stamp}.json"
    html_path.write_bytes(raw)
    parser = TitleParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    metadata = {
        "url": url,
        "retrieved_at": now(),
        "content_type": content_type,
        "bytes": len(raw),
        "title": "".join(parser.title_parts).strip(),
        "saved_html": str(html_path.relative_to(ROOT)),
    }
    save_json(meta_path, metadata)
    print(json.dumps(metadata, indent=2))


def cmd_inspect(_: argparse.Namespace) -> None:
    target = target_config()
    results = []
    for label, command in READ_ONLY_COMMANDS.items():
        print(f"Inspecting {label}...", file=sys.stderr)
        try:
            results.append(run_ssh_readonly(target, label, command))
        except subprocess.TimeoutExpired as exc:
            results.append({"label": label, "command": command, "error": str(exc)})
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = {
        "target": target["name"],
        "target_address": f"{target['user']}@{target['host']}:{target.get('port', 22)}",
        "collected_at": now(),
        "read_only": True,
        "results": results,
    }
    path = INVENTORY_DIR / f"inventory-{stamp}.json"
    save_json(path, output)
    state_path = STATE_DIR / "state.json"
    state = load_json(state_path) if state_path.exists() else {}
    state.update({"target": target["name"], "last_inventory": str(path.relative_to(ROOT)), "updated_at": now()})
    save_json(state_path, state)
    print(path)


def cmd_status(_: argparse.Namespace) -> None:
    state_path = STATE_DIR / "state.json"
    if not state_path.exists():
        print("No state/state.json exists. Copy state.example.json first.")
        return
    print(json.dumps(load_json(state_path), indent=2))


def cmd_validate(_: argparse.Namespace) -> None:
    target = target_config()
    install = install_config()
    plan = load_json(CONFIG / "plan.json")
    errors: list[str] = []
    warnings: list[str] = []

    required = [
        "profile", "boot_mode", "partition_table", "disk", "partition_layout",
        "root_filesystem", "swap_mb", "bootloader", "init_system", "hostname",
        "timezone", "network_method",
    ]
    for key in required:
        if key not in install:
            errors.append(f"missing install setting: {key}")

    if install.get("boot_mode") not in {"uefi", "bios"}:
        errors.append("boot_mode must be 'uefi' or 'bios'")
    if install.get("boot_mode") == "uefi" and install.get("partition_table") != "gpt":
        errors.append("UEFI installation requires a GPT partition table")
    if not isinstance(install.get("partition_layout"), list) or not install.get("partition_layout"):
        errors.append("partition_layout must contain the proposed partitions")
    if install.get("root_filesystem") != "ext4":
        warnings.append("this starter configuration has only been reviewed for ext4")
    if not isinstance(install.get("swap_mb"), int) or install.get("swap_mb", -1) < 0:
        errors.append("swap_mb must be a non-negative integer")
    create_user = install.get("create_user", True)
    set_root_password = install.get("set_root_password", True)
    if not isinstance(create_user, bool):
        errors.append("create_user must be boolean")
    if not isinstance(set_root_password, bool):
        errors.append("set_root_password must be boolean")
    install_user_ssh_key = install.get("install_user_ssh_key", True)
    if not isinstance(install_user_ssh_key, bool):
        errors.append("install_user_ssh_key must be boolean")
    if install_user_ssh_key and not create_user:
        errors.append("install_user_ssh_key requires create_user=true")
    configure_sudo = install.get("configure_sudo", True)
    if not isinstance(configure_sudo, bool):
        errors.append("configure_sudo must be boolean")
    if configure_sudo and not create_user:
        errors.append("configure_sudo requires create_user=true")
    if not isinstance(install.get("allow_root_password_ssh", False), bool):
        errors.append("allow_root_password_ssh must be boolean")
    password_source = install.get("password_source", "environment")
    if password_source not in {"prompt", "environment", "generate"}:
        errors.append("password_source must be 'prompt', 'environment', or 'generate'")
    if password_source == "environment":
        for field in ("root_password_env", "user_password_env"):
            env_name = str(install.get(field, ""))
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", env_name):
                errors.append(f"{field} must contain a valid uppercase environment-variable name")
    if not isinstance(install.get("generate_missing_passwords", True), bool):
        errors.append("generate_missing_passwords must be boolean")
    if create_user:
        username = str(install.get("username", ""))
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) or username == "root":
            errors.append("username must be a non-root Linux account name")
        groups = install.get("user_groups", ["wheel"])
        if not isinstance(groups, list) or any(
            not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(group)) for group in groups
        ):
            errors.append("user_groups must contain simple Linux group names")
        if configure_sudo and "wheel" not in [str(group) for group in groups]:
            errors.append("configure_sudo requires wheel in user_groups")
    if not create_user and not set_root_password:
        errors.append("at least one installed password must be configured")
    execution_flags = [
        target.get("execution_enabled"),
        install.get("execution_enabled"),
        plan.get("execution_enabled"),
    ]
    if len(set(execution_flags)) != 1:
        errors.append("target, install, and plan execution_enabled flags must match")
    if any(execution_flags) and install.get("review_status") != "approved_for_execution":
        errors.append("execution_enabled=true requires approved_for_execution review status")
    if install.get("disk_must_be_erased") and not (
        install.get("target_is_disposable") and install.get("disk_erase_authorized")
    ):
        errors.append("disk erasure requires explicit disposable-target authorization")
    if install.get("review_status") not in {
        "pending_operator_review", "reviewed_execution_disabled", "approved_for_execution"
    }:
        warnings.append("unrecognized review_status")

    inventory_paths = sorted(INVENTORY_DIR.glob("inventory-*.json"), key=lambda p: p.stat().st_mtime)
    if inventory_paths:
        inventory = load_json(inventory_paths[-1])
        results = {item.get("label"): item for item in inventory.get("results", [])}
        storage = results.get("storage", {}).get("stdout", "")
        boot_mode = results.get("boot_mode", {}).get("stdout", "").strip()
        disk = install.get("disk", "")
        if disk and disk not in storage:
            errors.append(f"configured disk {disk} was not found in the latest inventory")
        if install.get("boot_mode") == "uefi" and boot_mode != "uefi":
            errors.append(
                "desired UEFI install conflicts with the latest live boot mode "
                f"({boot_mode or 'unknown'}); boot the VM with EFI enabled and inspect again"
            )
        if install.get("boot_mode") == "bios" and boot_mode == "uefi":
            errors.append("desired BIOS install conflicts with the latest UEFI live boot")
    else:
        warnings.append("no inventory exists; run inspect before review")

    result = {
        "valid_for_execution": not errors,
        "execution_enabled": all(execution_flags),
        "errors": errors,
        "warnings": warnings,
        "target": target.get("name"),
        "installation": install,
        "plan_review_status": plan.get("review_status"),
    }
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit(2)


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
