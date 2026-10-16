"""Prepare a Gentoo installation configuration without installing anything.

This is the beginner-facing entry point. It performs read-only discovery,
asks for missing installation choices, writes the local configuration, and
prints a complete review. It never partitions, formats, installs, reboots, or
enables execution.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil

from gentoo_agent.core.config import load_json, load_plan, save_json
from gentoo_agent.core.errors import AgentError
from gentoo_agent.core.handbook import fetch_handbook
from gentoo_agent.core.inventory import collect_inventory, latest_inventory
from gentoo_agent.core.layout import build_layout
from gentoo_agent.core import paths
from gentoo_agent.core.state import load_state, save_state
from gentoo_agent.core.validate import validate_installation


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
    path = paths.config_dir() / "target.json"
    if not path.exists():
        default_path = paths.config_dir() / "target.default.json"
        if not default_path.exists():
            raise SystemExit("Missing config/target.json and config/target.default.json")
        shutil.copyfile(default_path, path)
        print(f"Created {path} from the visible default template.")

    target = load_json(path)
    target["host"] = ask("Live target IP address or hostname", target.get("host"))
    target["user"] = ask("SSH user", target.get("user", "root"))
    target["identity_file"] = ask("SSH private-key path", target.get("identity_file", "~/.ssh/id_rsa"))
    target["port"] = ask_int("SSH port", int(target.get("port", 22)))
    save_json(path, target)
    return target


def collect_inventory_interactive(target: dict) -> tuple[dict, object]:
    try:
        inventory, path = collect_inventory(target, on_progress=lambda label: print(f"  Read-only check: {label}"))
    except FileNotFoundError:
        raise SystemExit("OpenSSH client was not found. Install ssh and retry.")
    except AgentError as error:
        raise SystemExit(f"{error.code}: {error.message}") from None
    for result in inventory.get("results", []):
        if result.get("returncode") not in (0, None):
            print(f"    warning: {result['label']} returned {result.get('returncode')}")
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


def snapshot_handbook(target: dict) -> str | None:
    try:
        metadata = fetch_handbook(target)
    except Exception as exc:
        print(f"Handbook snapshot warning: {exc}")
        print(f"The agent must still use the configured Handbook URL: {target['handbook_url']}")
        return None
    print(f"Saved Handbook snapshot: {paths.root() / metadata['saved_html']}")
    return metadata["saved_html"]


def build_install_config(target: dict, inventory: dict) -> dict:
    defaults = load_json(paths.config_dir() / "install.default.json")
    results = result_map(inventory)
    detected_boot = results.get("boot_mode", {}).get("stdout", "").strip()
    disk = choose_disk(inventory)
    disk_path = disk.get("path") or disk.get("name")

    print("\nInstallation choices. Press Enter to accept the suggested default.")
    boot_mode = ask_choice("Boot mode", ["uefi", "bios"], "uefi")
    root_filesystem = ask_choice("Root filesystem", ["ext4"], defaults["root_filesystem"])
    swap_mb = ask_int("Swap size in MiB", defaults["swap_mb"])
    init_system = "openrc"
    print("Init system: openrc (systemd is not supported in this version)")
    hostname = ask("Hostname", defaults["hostname"])
    timezone = ask("Timezone", defaults["timezone"])
    network_method = ask_choice("Network method", ["dhcp", "static"], defaults["network_method"])
    static_network = None
    if network_method == "static":
        static_network = {
            "interface": ask("Network interface on the installed system (see the inventory network section), for example enp1s0"),
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

    partition_table, layout = build_layout(disk_path, boot_mode, swap_mb, root_filesystem)

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
    inventory, inventory_path = collect_inventory_interactive(target)
    handbook_path = snapshot_handbook(target)
    install = build_install_config(target, inventory)
    install["inventory_source"] = str(inventory_path.relative_to(paths.root()))
    install["handbook_snapshot"] = handbook_path
    save_json(paths.config_dir() / "install.json", install)

    state = load_state()
    state.update({
        "target": target["name"],
        "status": "review_required",
        "current_phase": "discovery",
        "last_inventory": str(inventory_path.relative_to(paths.root())),
        "last_handbook_fetch": handbook_path,
    })
    save_state(state)

    print("\n===== COMPLETE INSTALLATION CONFIGURATION =====")
    print(json.dumps(install, indent=2, ensure_ascii=False))
    print("================================================")
    print(f"Saved: {paths.config_dir() / 'install.json'}")
    print("\nLocal validation:")
    result = validate_installation(target, install, load_plan(), latest_inventory())
    print(json.dumps(result, indent=2))
    print("\nReview the printed configuration and config/install.json.")
    print("Next: run 'gentoo-agent plan create', then approve the plan yourself in a terminal.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
