#!/usr/bin/env python3
"""Safe starter tools for a supervised Gentoo-over-SSH agent workflow.

This module intentionally implements only Handbook retrieval, read-only SSH
inspection, status reporting, and explicit approval records. It does not run
destructive installation or repair commands.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html.parser
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
STATE_DIR = ROOT / "state"
INVENTORY_DIR = ROOT / "inventory"
LOG_DIR = ROOT / "logs"
APPROVAL_DIR = ROOT / "approvals"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    temporary.replace(path)


def target_config() -> dict:
    path = CONFIG / "target.json"
    if not path.exists():
        raise SystemExit("Missing config/target.json; copy target.example.json first.")
    return load_json(path)


def install_config() -> dict:
    target = target_config()
    configured_path = ROOT / target.get("install_config", "config/install.json")
    if not configured_path.exists():
        raise SystemExit(f"Missing installation configuration: {configured_path}")
    return load_json(configured_path)


def ssh_base(target: dict) -> list[str]:
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    known_hosts = target.get("known_hosts_file", "")
    if known_hosts:
        command += ["-o", f"UserKnownHostsFile={os.path.expanduser(known_hosts)}"]
    if target.get("identity_file"):
        command += ["-i", os.path.expanduser(target["identity_file"])]
    if target.get("port"):
        command += ["-p", str(target["port"])]
    command.append(f"{target['user']}@{target['host']}")
    return command


READ_ONLY_COMMANDS = {
    "identity": "printf 'hostname='; hostname; printf '\\narch='; uname -m; printf '\\nkernel='; uname -r",
    "boot_mode": "if [ -d /sys/firmware/efi ]; then printf 'uefi'; else printf 'bios-or-unknown'; fi",
    "storage": "lsblk -e7 -o NAME,PATH,SIZE,TYPE,FSTYPE,FSVER,LABEL,UUID,MOUNTPOINTS,ROTA,MODEL",
    "storage_json": "lsblk -J -e7 -o NAME,PATH,SIZE,TYPE,FSTYPE,MODEL,MOUNTPOINTS",
    "block_ids": "blkid || true",
    "mounts": "findmnt -D || mount",
    "network": "ip -brief address; printf '\\n'; ip route",
    "errors": "dmesg --level=err,warn 2>/dev/null | tail -n 200 || true",
}


def run_ssh_readonly(target: dict, label: str, command: str) -> dict:
    # OpenSSH joins all arguments after the host into one remote command. Quote
    # the shell program explicitly so pipes, semicolons, and conditionals are
    # interpreted on the target instead of being split by the SSH invocation.
    remote_command = "sh -c " + shlex.quote(command)
    completed = subprocess.run(
        ssh_base(target) + ["--", remote_command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    return {
        "label": label,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


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
    if install.get("execution_enabled") is not False:
        errors.append("execution_enabled must remain false during review")
    if plan.get("execution_enabled") is not False:
        errors.append("plan execution_enabled must remain false during review")
    if target.get("execution_enabled") is not False:
        errors.append("target execution_enabled must remain false during review")
    if install.get("review_status") != "pending_operator_review":
        warnings.append("review_status is not pending_operator_review")

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
        "execution_enabled": False,
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
    if args.phase != "discovery":
        raise SystemExit("Only the read-only discovery phase is currently executable.")
    cmd_inspect(args)


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
    return parser


if __name__ == "__main__":
    try:
        parsed = build_parser().parse_args()
        parsed.func(parsed)
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
