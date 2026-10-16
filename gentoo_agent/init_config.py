"""Create a safe, reviewable minimum Gentoo Agent configuration.

This command only writes local JSON files. It never connects to SSH and never
changes a target machine.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


from gentoo_agent.core import paths
from gentoo_agent.core.layout import build_layout


def write_json(path: Path, value: dict, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise SystemExit(f"Refusing to overwrite {path}; use --overwrite explicitly.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Live target IP address or hostname")
    parser.add_argument("--disk", required=True, help="Confirmed disk to erase, e.g. /dev/sda")
    parser.add_argument("--identity-file", default="~/.ssh/id_rsa")
    parser.add_argument("--name", default="gentoo")
    parser.add_argument("--user", default="root")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), default="amd64",
                        help="Gentoo architecture of the target (uname -m x86_64 is amd64, aarch64 is arm64)")
    parser.add_argument("--boot-mode", choices=("uefi", "bios"), default="uefi")
    parser.add_argument("--root-filesystem", choices=("ext4",), default="ext4")
    parser.add_argument("--swap-mb", type=int, default=2048)
    parser.add_argument("--init-system", choices=("openrc",), default="openrc",
                        help="Only openrc is supported in this version")
    parser.add_argument("--hostname", default="gentoo")
    parser.add_argument("--timezone", default="Europe/Prague")
    parser.add_argument("--network-method", default="dhcp")
    parser.add_argument("--username", default="gentoo", help="Installed regular username")
    parser.add_argument("--no-create-user", action="store_true", help="Do not create an installed regular user")
    parser.add_argument("--no-sudo", action="store_true", help="Do not install sudo or configure wheel-group sudo access")
    parser.add_argument("--no-root-password", action="store_true", help="Do not prompt for a root password")
    parser.add_argument("--allow-root-password-ssh", action="store_true", help="Allow root password authentication over SSH")
    parser.add_argument("--password-source", choices=("prompt", "environment"), default="environment")
    parser.add_argument("--root-password-env", default="GENTOO_ROOT_PASSWORD")
    parser.add_argument("--user-password-env", default="GENTOO_USER_PASSWORD")
    parser.add_argument("--no-dual-boot", action="store_true",
                        help="Do not add Windows found on other disks to the GRUB menu")
    parser.add_argument("--allow-disk-erase", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.swap_mb < 0:
        raise SystemExit("--swap-mb must be non-negative")
    if not args.allow_disk_erase:
        raise SystemExit(
            "Refusing to create an erase-enabled plan. Add --allow-disk-erase "
            "after confirming the exact target disk."
        )

    if args.architecture == "arm64" and args.boot_mode != "uefi":
        raise SystemExit("arm64 supports UEFI boot only; use --boot-mode uefi")

    partition_table, layout = build_layout(args.disk, args.boot_mode, args.swap_mb, args.root_filesystem)

    target = load_json(paths.config_dir() / "target.default.json")
    target.update({
        "name": args.name,
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "identity_file": args.identity_file,
        "architecture": args.architecture,
        "handbook_url": f"https://wiki.gentoo.org/wiki/Handbook:{args.architecture.upper()}",
        "expected_target_description": "Target created by init_config.py; review before use",
        "target_is_disposable": bool(args.allow_disk_erase),
        "disk_erase_authorized": bool(args.allow_disk_erase),
    })
    install = load_json(paths.config_dir() / "install.default.json")
    install.update({
        "architecture": args.architecture,
        "profile": f"gentoo-{args.architecture}-{args.init_system}",
        "boot_mode": args.boot_mode,
        "partition_table": partition_table,
        "disk": args.disk,
        "disk_must_be_erased": True,
        "target_is_disposable": bool(args.allow_disk_erase),
        "disk_erase_authorized": bool(args.allow_disk_erase),
        "partition_layout": layout,
        "root_filesystem": args.root_filesystem,
        "swap_mb": args.swap_mb,
        "init_system": args.init_system,
        "hostname": args.hostname,
        "timezone": args.timezone,
        "network_method": args.network_method,
        "create_user": not args.no_create_user,
        "username": args.username,
        "install_user_ssh_key": not args.no_create_user,
        "user_groups": ["wheel"],
        "configure_sudo": not args.no_sudo and not args.no_create_user,
        "user_shell": "/bin/bash",
        "set_root_password": not args.no_root_password,
        "allow_root_password_ssh": args.allow_root_password_ssh,
        "password_source": args.password_source,
        "generate_missing_passwords": True,
        "root_password_env": args.root_password_env,
        "user_password_env": args.user_password_env,
        "dual_boot": "off" if args.no_dual_boot else "auto",
    })

    write_json(paths.config_dir() / "target.json", target, args.overwrite)
    write_json(paths.config_dir() / "install.json", install, args.overwrite)
    print("Created local configuration only:")
    print(f"  {paths.config_dir() / 'target.json'}")
    print(f"  {paths.config_dir() / 'install.json'}")
    print("Nothing can run until a plan is created and approved; review both files first.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
