"""Configuration validation: the single gate before commands are generated."""

from __future__ import annotations

import ipaddress
import math
import re

from gentoo_agent.core.arch import architecture_spec, live_architecture
from gentoo_agent.core.errors import CONFIG_CONFLICT, CONFIG_INVALID, AgentError


# Version 1 generates OpenRC programs only (rc-update, /etc/runlevels).
INIT_SYSTEMS = {"openrc"}
NETWORK_METHODS = {"dhcp", "static"}
MAX_SWAP_MB = 131072
# Linux interface names: at most 15 characters (IFNAMSIZ - 1), no spaces or shell syntax.
INTERFACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
IP_TEXT = re.compile(r"[0-9A-Fa-f:./]+")

# Block device paths that may reach a generated program or the approval dialog.
DEVICE_PATH = re.compile(r"/dev/[A-Za-z0-9._+/-]+")
# The volume serial of a FAT EFI system partition, as blkid and lsblk print it.
ESP_UUID = re.compile(r"[0-9A-F]{4}-[0-9A-F]{4}")


IPV4_CIDR = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}")
STATIC_PREFIXES = range(8, 31)
STATIC_ADDRESS_MESSAGE = "Static addresses must be IPv4 in CIDR form, for example 192.168.10.20/24"

# Positive rule: the name must say it holds a password. The denylist below
# stays as a second layer against names such as OPENAI_PW or SSH_PASSWORD.
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,62}(_PASSWORD|_PW)")
ENV_NAME_MESSAGE = 'Password variable names must end in _PASSWORD or _PW, for example {"env": "ALEX_PW"}.'
RESERVED_ENV_NAMES = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "TERM", "LANG", "TMPDIR", "EDITOR",
    "GENTOO_AGENT_HOME",
}
RESERVED_ENV_PREFIXES = ("SSH_", "LD_", "DYLD_", "PYTHON", "GIT_")
RESERVED_ENV_PARTS = ("TOKEN", "SECRET", "API_KEY", "PRIVATE", "AWS_", "ANTHROPIC", "OPENAI")


def password_env_name_ok(name: object) -> bool:
    """True for a name ending in _PASSWORD or _PW that is not a system or credential variable.

    Password variables are read from the agent's environment, so a name such
    as PATH or OPENAI_API_KEY would install that value as a password.
    """
    return (
        isinstance(name, str)
        and ENV_NAME.fullmatch(name) is not None
        and name not in RESERVED_ENV_NAMES
        and not name.startswith(RESERVED_ENV_PREFIXES)
        and not any(part in name for part in RESERVED_ENV_PARTS)
    )


def _invalid(field: str, message: str) -> AgentError:
    return AgentError(CONFIG_INVALID, message, {"field": field})


def _ip_text(value: object, parse) -> bool:
    """True when value is a plain IP string that parse() accepts.

    The character check rejects IPv6 scope ids (fe80::1%eth0), which
    ipaddress accepts with arbitrary text after the percent sign.
    """
    if not isinstance(value, str) or not IP_TEXT.fullmatch(value):
        return False
    try:
        parse(value)
    except ValueError:
        return False
    return True


def _check_network(install: dict) -> None:
    network = install.get("network_method", "dhcp")
    if not isinstance(network, str) or network not in NETWORK_METHODS:
        raise _invalid("network_method", "network_method must be 'dhcp' or 'static'")
    if network != "static":
        return
    static = install.get("network_static")
    message = ("network_static must be an object with interface (for example enp1s0), address (CIDR), "
               "gateway (IP address), and dns (a list of 1 to 3 IP addresses)")
    if not isinstance(static, dict) or set(static) != {"interface", "address", "gateway", "dns"}:
        raise _invalid("network_static", message)
    interface_name = static["interface"]
    if not isinstance(interface_name, str) or not INTERFACE_NAME.fullmatch(interface_name):
        raise _invalid("network_static", "network_static.interface must be a Linux interface name such as enp1s0")
    address = static["address"]
    if not isinstance(address, str) or not IPV4_CIDR.fullmatch(address):
        raise _invalid("network_static", STATIC_ADDRESS_MESSAGE)
    try:
        interface = ipaddress.IPv4Interface(address)
    except ValueError:
        raise _invalid("network_static", STATIC_ADDRESS_MESSAGE) from None
    if interface.network.prefixlen not in STATIC_PREFIXES:
        raise _invalid("network_static", STATIC_ADDRESS_MESSAGE + " with a prefix from /8 to /30")
    gateway = static["gateway"]
    if not _ip_text(gateway, ipaddress.IPv4Address) or ipaddress.IPv4Address(gateway) not in interface.network:
        raise _invalid("network_static", f"The static gateway must be an IPv4 address inside {interface.network}")
    dns = static["dns"]
    if (
        not isinstance(dns, list)
        or not 1 <= len(dns) <= 3
        or not all(_ip_text(item, ipaddress.ip_address) for item in dns)
    ):
        raise _invalid("network_static", message)


def validated_settings(target: dict, install: dict) -> tuple[str, list[dict]]:
    disk = install.get("disk", "")
    if not re.fullmatch(r"/dev/[A-Za-z0-9._+/-]+", disk):
        raise AgentError(CONFIG_INVALID, f"Unsafe disk path in configuration: {disk!r}", {"field": "disk"})
    layout = install.get("partition_layout")
    if not isinstance(layout, list) or not layout:
        raise AgentError(CONFIG_INVALID, "partition_layout is empty", {"field": "partition_layout"})
    for item in layout:
        device = item.get("device", "")
        if not re.fullmatch(r"/dev/[A-Za-z0-9._+/-]+", device):
            raise AgentError(CONFIG_INVALID, f"Unsafe partition path in configuration: {device!r}", {"field": "partition_layout"})
    spec = architecture_spec(install.get("architecture"))
    if install.get("boot_mode") == "bios" and not spec["bios"]:
        raise AgentError(CONFIG_CONFLICT, f"{install['architecture']} supports UEFI boot only.",
                         {"fields": ["architecture", "boot_mode"]})
    if target.get("user") != "root":
        raise AgentError(CONFIG_INVALID, "The installation executor currently requires SSH user root.", {"field": "user"})
    if install.get("boot_mode") == "uefi" and install.get("partition_table") != "gpt":
        raise AgentError(CONFIG_CONFLICT, "UEFI requires GPT.", {"fields": ["boot_mode", "partition_table"]})
    if install.get("root_filesystem") != "ext4":
        raise AgentError(CONFIG_INVALID, "The executor currently supports ext4 root filesystems only.", {"field": "root_filesystem"})
    hostname = install.get("hostname")
    if not isinstance(hostname, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", hostname):
        raise _invalid("hostname", "hostname must be a simple DNS-compatible hostname")
    timezone = install.get("timezone")
    if (
        not isinstance(timezone, str)
        or not re.fullmatch(r"[A-Za-z0-9._+/-]+", timezone)
        or timezone.startswith("/")
        or ".." in timezone.split("/")
    ):
        raise _invalid("timezone", "timezone must be a relative zoneinfo name such as Europe/Prague")
    init_system = install.get("init_system")
    if not isinstance(init_system, str) or init_system not in INIT_SYSTEMS:
        raise _invalid("init_system", "init_system must be 'openrc'; systemd is not supported in this version")
    _check_network(install)
    if "swap_mb" in install:
        swap_mb = install["swap_mb"]
        if not isinstance(swap_mb, int) or isinstance(swap_mb, bool) or not 0 <= swap_mb <= MAX_SWAP_MB:
            raise _invalid("swap_mb", f"swap_mb must be an integer from 0 to {MAX_SWAP_MB}")
    expected_size = install.get("expected_disk_size_gib")
    if expected_size is not None and (
        not isinstance(expected_size, (int, float)) or isinstance(expected_size, bool)
        or not math.isfinite(expected_size) or expected_size <= 0
    ):
        raise _invalid("expected_disk_size_gib", "expected_disk_size_gib must be a number greater than 0")
    expected_model = install.get("expected_disk_model")
    if expected_model is not None and not isinstance(expected_model, str):
        raise _invalid("expected_disk_model", "expected_disk_model must be a string")
    create_user = install.get("create_user", True)
    set_root_password = install.get("set_root_password", True)
    install_user_ssh_key = install.get("install_user_ssh_key", True)
    configure_sudo = install.get("configure_sudo", True)
    allow_root_password_ssh = install.get("allow_root_password_ssh", False)
    generate_missing_passwords = install.get("generate_missing_passwords", True)
    for field, value in (
        ("create_user", create_user),
        ("set_root_password", set_root_password),
        ("install_user_ssh_key", install_user_ssh_key),
        ("configure_sudo", configure_sudo),
        ("allow_root_password_ssh", allow_root_password_ssh),
        ("generate_missing_passwords", generate_missing_passwords),
    ):
        if not isinstance(value, bool):
            raise AgentError(CONFIG_INVALID, f"{field} must be boolean", {"field": field})
    if install_user_ssh_key and not create_user:
        raise AgentError(CONFIG_INVALID, "install_user_ssh_key requires create_user=true", {"field": "install_user_ssh_key"})
    if configure_sudo and not create_user:
        raise _invalid("configure_sudo", "configure_sudo requires create_user=true")
    password_source = install.get("password_source", "environment")
    if password_source not in {"prompt", "environment", "generate"}:
        raise AgentError(CONFIG_INVALID, "password_source must be 'prompt', 'environment', or 'generate'", {"field": "password_source"})
    for field in ("root_password_env", "user_password_env"):
        if (password_source == "environment" or field in install) and not password_env_name_ok(install.get(field)):
            raise AgentError(
                CONFIG_INVALID,
                f"{field}: {ENV_NAME_MESSAGE}",
                {"field": field},
            )
    if not create_user and not set_root_password:
        raise AgentError(CONFIG_INVALID, "at least one installed password must be configured", {"fields": ["create_user", "set_root_password"]})
    if create_user:
        username = str(install.get("username", ""))
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) or username == "root":
            raise AgentError(CONFIG_INVALID, "username must be a non-root Linux account name", {"field": "username"})
        groups = install.get("user_groups", ["wheel"])
        if not isinstance(groups, list) or any(
            not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(group)) for group in groups
        ):
            raise AgentError(CONFIG_INVALID, "user_groups must contain simple Linux group names", {"field": "user_groups"})
        if configure_sudo and "wheel" not in [str(group) for group in groups]:
            raise AgentError(CONFIG_INVALID, "configure_sudo requires wheel in user_groups", {"field": "configure_sudo"})
        shell = str(install.get("user_shell", "/bin/bash"))
        if shell != "/bin/bash":
            raise AgentError(CONFIG_INVALID, "user_shell must be /bin/bash; other shells are not installed by this workflow", {"field": "user_shell"})
    return disk, layout


def validate_installation(target: dict, install: dict, plan: dict, inventory: dict | None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    required = [
        "architecture", "profile", "boot_mode", "partition_table", "disk", "partition_layout",
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
    for field in ("root_password_env", "user_password_env"):
        if (password_source == "environment" or field in install) and not password_env_name_ok(install.get(field)):
            errors.append(f"{field}: {ENV_NAME_MESSAGE}")
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
    if install.get("disk_must_be_erased") and not (
        install.get("target_is_disposable") and install.get("disk_erase_authorized")
    ):
        errors.append("disk erasure requires explicit disposable-target authorization")

    if inventory is not None:
        results = {item.get("label"): item for item in inventory.get("results", [])}
        storage = results.get("storage", {}).get("stdout", "")
        boot_mode = results.get("boot_mode", {}).get("stdout", "").strip()
        try:
            live = live_architecture(inventory)
        except AgentError as error:
            errors.append(error.message)
        else:
            if install.get("architecture") != live:
                errors.append(f"architecture {install.get('architecture')} does not match the live system ({live})")
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

    return {
        "valid_for_execution": not errors,
        "errors": errors,
        "warnings": warnings,
        "target": target.get("name"),
        "installation": install,
    }
