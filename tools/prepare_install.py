#!/usr/bin/env python3
"""Prepare a Gentoo installation configuration without installing anything.

This is the beginner-facing entry point. It performs read-only discovery,
asks for missing installation choices, writes the local configuration, and
prints a complete review. It never partitions, formats, installs, reboots, or
enables execution.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request

from gentoo_agent import (
    CONFIG,
    INVENTORY_DIR,
    ROOT,
    READ_ONLY_COMMANDS,
    load_json,
    now,
    run_ssh_readonly,
    save_json,
)


def partition_device(disk: str, number: int) -> str:
    return f"{disk}{'p' if disk[-1:].isdigit() else ''}{number}"


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("A value is required.")


def ask_choice(prompt: str, choices: list[str], default: str) -> str:
    allowed = {choice.lower(): choice for choice in choices}
    while True:
        value = ask(f"{prompt} ({'/'.join(choices)})", default).lower()
        if value in allowed:
            return allowed[value]
        print(f"Choose one of: {', '.join(choices)}")


def ask_int(prompt: str, default: int) -> int:
    while True:
        value = ask(prompt, str(default))
        try:
            parsed = int(value)
            if parsed >= 0:
                return parsed
        except ValueError:
            pass
        print("Enter a non-negative whole number.")


def ensure_target_config() -> dict:
    path = CONFIG / "target.json"
    if not path.exists():
        default_path = CONFIG / "target.default.json"
        if not default_path.exists():
            raise SystemExit("Missing config/target.json and config/target.default.json")
        shutil.copyfile(default_path, path)
        print(f"Created {path} from the visible default template.")

    target = load_json(path)
    target["host"] = ask("Live target IP address or hostname", target.get("host"))
    target["user"] = ask("SSH user", target.get("user", "root"))
    target["identity_file"] = ask("SSH private-key path", target.get("identity_file", "~/.ssh/id_rsa"))
    target["port"] = ask_int("SSH port", int(target.get("port", 22)))
    target["execution_enabled"] = False
    target["dangerous_operations_enabled"] = False
    save_json(path, target)
    return target


def collect_inventory(target: dict) -> tuple[dict, Path]:
    results = []
    for label, command in READ_ONLY_COMMANDS.items():
        print(f"  Read-only check: {label}")
        try:
            result = run_ssh_readonly(target, label, command)
        except FileNotFoundError:
            raise SystemExit("OpenSSH client was not found. Install ssh and retry.")
        except subprocess.TimeoutExpired:
            result = {"label": label, "command": command, "returncode": None, "error": "timeout"}
        results.append(result)
        if result.get("returncode") not in (0, None):
            print(f"    warning: command returned {result.get('returncode')}")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    inventory = {
        "target": target["name"],
        "target_address": f"{target['user']}@{target['host']}:{target.get('port', 22)}",
        "collected_at": now(),
        "read_only": True,
        "results": results,
    }
    path = INVENTORY_DIR / f"inventory-{stamp}.json"
    save_json(path, inventory)
    return inventory, path


def result_map(inventory: dict) -> dict:
    return {item.get("label"): item for item in inventory.get("results", [])}


def disks_from_inventory(inventory: dict) -> list[dict]:
    item = result_map(inventory).get("storage_json", {})
    try:
        devices = json.loads(item.get("stdout", "{}")).get("blockdevices", [])
    except json.JSONDecodeError:
        return []
    return [device for device in devices if device.get("type") == "disk"]


def size_gib(value: str | None) -> float | None:
    if not value:
        return None
    match = re.match(r"^([0-9.]+)\s*([KMGT])", value.upper())
    if not match:
        return None
    units = {"K": 1 / (1024 * 1024), "M": 1 / 1024, "G": 1, "T": 1024}
    return round(float(match.group(1)) * units[match.group(2)], 2)


def choose_disk(inventory: dict) -> dict:
    disks = disks_from_inventory(inventory)
    if disks:
        print("\nDetected disks:")
        for disk in disks:
            print(
                f"  {disk.get('path', disk.get('name'))}: "
                f"{disk.get('size', '?')}, model={disk.get('model') or 'unknown'}, "
                f"mounts={disk.get('mountpoints') or 'none'}"
            )
        default = disks[0].get("path") or disks[0].get("name")
        selected = ask("Disk to use", default)
        for disk in disks:
            if (disk.get("path") or disk.get("name")) == selected:
                return disk
    selected = ask("Disk to use, for example /dev/sda")
    return {"path": selected, "size": None, "model": None}


def fetch_handbook(target: dict) -> str | None:
    url = target["handbook_url"]
    request = urllib.request.Request(url, headers={"User-Agent": "gentoo-agent/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except Exception as exc:
        print(f"Handbook snapshot warning: {exc}")
        print(f"The agent must still use the configured Handbook URL: {url}")
        return None

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = INVENTORY_DIR / f"handbook-{stamp}.html"
    path.write_bytes(raw)
    metadata = {"url": url, "retrieved_at": now(), "bytes": len(raw), "saved_html": str(path.relative_to(ROOT))}
    save_json(INVENTORY_DIR / f"handbook-{stamp}.json", metadata)
    print(f"Saved Handbook snapshot: {path}")
    return str(path.relative_to(ROOT))


def build_install_config(target: dict, inventory: dict) -> dict:
    defaults = load_json(CONFIG / "install.default.json")
    results = result_map(inventory)
    detected_boot = results.get("boot_mode", {}).get("stdout", "").strip()
    disk = choose_disk(inventory)
    disk_path = disk.get("path") or disk.get("name")

    print("\nInstallation choices. Press Enter to accept the suggested default.")
    boot_mode = ask_choice("Boot mode", ["uefi", "bios"], "uefi")
    root_filesystem = ask_choice("Root filesystem", ["ext4"], defaults["root_filesystem"])
    swap_mb = ask_int("Swap size in MiB", defaults["swap_mb"])
    init_system = ask_choice("Init system", ["openrc", "systemd"], defaults["init_system"])
    hostname = ask("Hostname", defaults["hostname"])
    timezone = ask("Timezone", defaults["timezone"])
    network_method = ask_choice("Network method", ["dhcp", "static"], defaults["network_method"])
    static_network = None
    if network_method == "static":
        static_network = {
            "address": ask("Static address in CIDR notation"),
            "gateway": ask("Default gateway"),
            "dns": [item.strip() for item in ask("DNS servers, comma separated").split(",") if item.strip()],
        }

    create_user = ask_choice("Create a regular login user", ["yes", "no"], "yes") == "yes"
    username = defaults.get("username", "gentoo")
    if create_user:
        username = ask("Regular username", username)
    install_user_ssh_key = False
    if create_user:
        install_user_ssh_key = ask_choice(
            "Install the live SSH key for the regular user", ["yes", "no"], "yes"
        ) == "yes"
    configure_sudo = False
    if create_user:
        configure_sudo = ask_choice(
            "Configure wheel-group sudo access", ["yes", "no"], "yes"
        ) == "yes"
    set_root_password = ask_choice("Set a root console password", ["yes", "no"], "yes") == "yes"
    allow_root_password_ssh = ask_choice(
        "Allow root password login over SSH", ["yes", "no"], "no"
    ) == "yes"
    password_source = ask_choice("Password source", ["prompt", "environment"], defaults.get("password_source", "environment"))
    root_password_env = defaults.get("root_password_env", "GENTOO_ROOT_PASSWORD")
    user_password_env = defaults.get("user_password_env", "GENTOO_USER_PASSWORD")
    if password_source == "environment":
        root_password_env = ask("Root password environment variable", root_password_env)
        user_password_env = ask("User password environment variable", user_password_env)

    if boot_mode == "uefi":
        partition_table = "gpt"
        layout = [
            {"device": partition_device(disk_path, 1), "size_mib": 512, "type": "efi-system", "filesystem": "vfat", "mount": "/boot"},
            {"device": partition_device(disk_path, 2), "size_mib": swap_mb, "type": "linux-swap", "mount": None},
            {"device": partition_device(disk_path, 3), "size": "remaining", "type": "linux-root", "filesystem": root_filesystem, "mount": "/"},
        ]
    else:
        partition_table = "mbr"
        layout = [
            {"device": partition_device(disk_path, 1), "size_mib": swap_mb, "type": "linux-swap", "mount": None},
            {"device": partition_device(disk_path, 2), "size": "remaining", "type": "linux-root", "filesystem": root_filesystem, "mount": "/"},
        ]

    print(f"\nThe selected disk is {disk_path}. This plan will erase it during a future storage phase.")
    preauthorized = bool(target.get("target_is_disposable") and target.get("disk_erase_authorized"))
    if preauthorized:
        print("Disk erasure is pre-authorized by the disposable-target configuration.")
        disk_must_be_erased = True
    else:
        erase_confirmation = ask(f"Type ERASE {disk_path} to record that decision", "CANCEL")
        disk_must_be_erased = erase_confirmation == f"ERASE {disk_path}"
        if not disk_must_be_erased:
            print("Disk erasure was not confirmed; execution will remain blocked.")

    install = dict(defaults)
    install.update({
        "profile": f"gentoo-{target.get('architecture', 'amd64')}-{init_system}",
        "boot_mode": boot_mode,
        "partition_table": partition_table,
        "disk": disk_path,
        "disk_must_be_erased": disk_must_be_erased,
        "target_is_disposable": bool(target.get("target_is_disposable")),
        "disk_erase_authorized": bool(target.get("disk_erase_authorized")),
        "partition_layout": layout,
        "root_filesystem": root_filesystem,
        "swap_mb": swap_mb,
        "init_system": init_system,
        "hostname": hostname,
        "timezone": timezone,
        "network_method": network_method,
        "network_static": static_network,
        "create_user": create_user,
        "username": username,
        "install_user_ssh_key": install_user_ssh_key,
        "user_groups": defaults.get("user_groups", ["wheel"]),
        "configure_sudo": configure_sudo,
        "user_shell": defaults.get("user_shell", "/bin/bash"),
        "set_root_password": set_root_password,
        "allow_root_password_ssh": allow_root_password_ssh,
        "password_source": password_source,
        "generate_missing_passwords": defaults.get("generate_missing_passwords", True),
        "root_password_env": root_password_env,
        "user_password_env": user_password_env,
        "execution_enabled": False,
        "review_status": "pending_operator_review",
        "detected_live_boot_mode": detected_boot,
        "expected_disk_model": disk.get("model"),
        "expected_disk_size_gib": size_gib(disk.get("size")),
    })
    return install


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    print("Gentoo Agent installation preparation")
    print("This command performs read-only discovery and writes local review files.")
    print("It will not install, partition, format, reboot, or enable execution.\n")

    target = ensure_target_config()
    print(f"Connecting read-only to {target['user']}@{target['host']}...")
    inventory, inventory_path = collect_inventory(target)
    handbook_path = fetch_handbook(target)
    install = build_install_config(target, inventory)
    install["inventory_source"] = str(inventory_path.relative_to(ROOT))
    install["handbook_snapshot"] = handbook_path
    save_json(CONFIG / "install.json", install)

    state_path = ROOT / "state" / "state.json"
    state = load_json(state_path) if state_path.exists() else {}
    state.update({
        "target": target["name"],
        "status": "review_required",
        "current_phase": "discovery",
        "last_inventory": str(inventory_path.relative_to(ROOT)),
        "last_handbook_fetch": handbook_path,
        "updated_at": now(),
    })
    save_json(state_path, state)

    print("\n===== COMPLETE INSTALLATION CONFIGURATION =====")
    print(json.dumps(install, indent=2, ensure_ascii=False))
    print("================================================")
    print(f"Saved: {CONFIG / 'install.json'}")
    print("\nLocal validation:")
    subprocess.run([sys.executable, str(ROOT / "tools" / "gentoo_agent.py"), "validate"], cwd=ROOT, check=False)
    print("\nReview the printed configuration and config/install.json.")
    if input("Type REVIEWED to record that you reviewed it, or press Enter to leave it pending: ").strip() == "REVIEWED":
        install["review_status"] = "reviewed_execution_disabled"
        save_json(CONFIG / "install.json", install)
        state["status"] = "reviewed_execution_disabled"
        save_json(state_path, state)
        print("Review recorded. Execution is still disabled.")
    else:
        print("Review remains pending. Execution is still disabled.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
