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
    "block_ids": "blkid || true",
    "mounts": "findmnt -D || mount",
    "network": "ip -brief address; printf '\\n'; ip route",
    "errors": "dmesg --level=err,warn 2>/dev/null | tail -n 200 || true",
}


def run_ssh_readonly(target: dict, label: str, command: str) -> dict:
    completed = subprocess.run(
        ssh_base(target) + ["--", "sh", "-c", command],
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
