"""Resolve an install configuration from layered inputs.

Priority, highest first: overrides (from the user's request), profile,
values inferred from the inventory, fallbacks, and defaults. The result
records the source of every value. Passwords are accepted only as
environment-variable references, never as text.

Overrides and profiles may come from a model, so every value is
type-checked before merging and any malformed input raises AgentError.
"""

from __future__ import annotations

import copy

from gentoo_agent.core.disks import disk_candidates, inventory_results
from gentoo_agent.core.errors import (
    CONFIG_CONFLICT,
    CONFIG_INVALID,
    DISK_AMBIGUOUS,
    INVENTORY_MISSING,
    AgentError,
)
from gentoo_agent.core.layout import build_layout
from gentoo_agent.core.validate import ENV_NAME_MESSAGE, password_env_name_ok, validated_settings


SETTABLE = {
    "username", "hostname", "timezone", "boot_mode", "init_system", "swap_mb", "disk",
    "network_method", "network_static", "create_user", "configure_sudo", "set_root_password",
    "allow_root_password_ssh", "install_user_ssh_key", "bootloader", "user_groups",
}
TYPES = {
    "username": str, "hostname": str, "timezone": str, "boot_mode": str, "init_system": str,
    "network_method": str, "disk": str, "bootloader": str,
    "swap_mb": int,
    "create_user": bool, "configure_sudo": bool, "set_root_password": bool,
    "allow_root_password_ssh": bool, "install_user_ssh_key": bool,
    "user_groups": list,
    "network_static": dict,
}
PASSWORD_FIELDS = {"user_password": "user_password_env", "root_password": "root_password_env"}
DERIVED = {
    "disk", "partition_layout", "partition_table", "disk_must_be_erased", "target_is_disposable",
    "disk_erase_authorized", "expected_disk_model", "expected_disk_size_gib",
}
# Fallbacks fill gaps the user left open; the disk to erase and the boot
# mode must come from the user, the profile, or the hardware.
NOT_FALLBACK = ("boot_mode", "disk")
SUPPORTED_BOOTLOADERS = {"grub"}
# Version 1 generates OpenRC commands only. validate.py still accepts systemd so
# hand-written configurations and the golden fixtures keep rendering.
SUPPORTED_INIT_SYSTEM = "openrc"
LIVE_BOOT_MODES = {"uefi": "uefi", "bios-or-unknown": "bios"}


def _type_ok(key: str, value: object) -> bool:
    expected = TYPES[key]
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is list:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return isinstance(value, expected)


def _check_layer(name: str, layer: object, allow_passwords: bool) -> dict:
    if layer is None:
        return {}
    if not isinstance(layer, dict):
        raise AgentError(CONFIG_INVALID, f"{name} must be an object", {"layer": name})
    allowed = SETTABLE | (set(PASSWORD_FIELDS) if allow_passwords else set())
    unknown = sorted(str(key) for key in set(layer) - allowed)
    if unknown:
        raise AgentError(CONFIG_INVALID, f"{name} contains settings that cannot be set: {', '.join(unknown)}",
                         {"layer": name, "keys": unknown})
    for key in sorted(set(layer) & SETTABLE):
        if not _type_ok(key, layer[key]):
            expected = "list of strings" if TYPES[key] is list else TYPES[key].__name__
            raise AgentError(CONFIG_INVALID, f"{name}.{key} must be of type {expected}", {"layer": name, "field": key})
    for field in PASSWORD_FIELDS:
        if field in layer:
            value = layer[field]
            if not (isinstance(value, dict) and set(value) == {"env"} and password_env_name_ok(value["env"])):
                raise AgentError(
                    CONFIG_INVALID,
                    'Passwords are passed by environment-variable name, never as text: {"env": "NAME"}. '
                    + ENV_NAME_MESSAGE,
                    {"field": field},
                )
    return layer


def _live_boot_mode(results: dict) -> str:
    item = results.get("boot_mode")
    stdout = item.get("stdout") if isinstance(item, dict) and item.get("returncode") == 0 else None
    if not isinstance(stdout, str) or stdout not in LIVE_BOOT_MODES:
        raise AgentError(INVENTORY_MISSING, "Boot mode is unknown; refresh the inventory.", {"label": "boot_mode"})
    return LIVE_BOOT_MODES[stdout]


def _excluded_text(excluded: list[dict]) -> str:
    return ", ".join(f"{item['path']} ({item['reason']})" for item in excluded) or "none"


def resolve_config(
    target: dict,
    overrides: dict | None,
    fallbacks: dict | None,
    defaults: dict,
    inventory: dict | None,
    *,
    profile: dict | None = None,
) -> dict:
    overrides = _check_layer("overrides", overrides, allow_passwords=True)
    profile = _check_layer("profile", profile, allow_passwords=True)
    fallbacks = _check_layer("fallbacks", fallbacks, allow_passwords=False)
    for key in NOT_FALLBACK:
        if key in fallbacks:
            raise AgentError(CONFIG_INVALID, f"fallbacks cannot set {key}; choose it explicitly or let the hardware decide.",
                             {"layer": "fallbacks", "field": key})
    if inventory is None:
        raise AgentError(INVENTORY_MISSING, "No inventory exists; run detect_hardware with refresh first.", {})
    results = inventory_results(inventory)
    eligible, excluded = disk_candidates(inventory)
    live_boot = _live_boot_mode(results)

    inferred: dict = {"boot_mode": live_boot}
    if len(eligible) == 1:
        inferred["disk"] = eligible[0]["path"]

    base = {key: value for key, value in copy.deepcopy(defaults).items() if key not in DERIVED}
    layers = [("override", overrides), ("profile", profile), ("inferred", inferred),
              ("fallback", fallbacks), ("default", base)]
    install: dict = {}
    sources: dict = {}
    for name, layer in reversed(layers):
        for key, value in layer.items():
            install[key] = copy.deepcopy(value)
            sources[key] = name
    warnings: list[str] = []

    if install.get("bootloader", "grub") not in SUPPORTED_BOOTLOADERS:
        raise AgentError(CONFIG_INVALID, "Only the grub bootloader is supported.", {"field": "bootloader"})
    if install.get("init_system", SUPPORTED_INIT_SYSTEM) != SUPPORTED_INIT_SYSTEM:
        raise AgentError(CONFIG_INVALID, "Only OpenRC is supported in this version (init_system must be openrc).",
                         {"field": "init_system"})
    if sources.get("boot_mode") != "inferred" and install["boot_mode"] != live_boot:
        raise AgentError(
            CONFIG_CONFLICT,
            f"boot_mode {install['boot_mode']} was requested but the live system booted in {live_boot} mode.",
            {"requested": install["boot_mode"], "live": live_boot},
        )

    excluded_details = [{"path": item["path"], "reason": item["reason"]} for item in excluded]
    if "disk" not in install:
        if not eligible and not excluded:
            raise AgentError(INVENTORY_MISSING, "The inventory lists no disks.", {})
        raise AgentError(
            DISK_AMBIGUOUS,
            "No single eligible disk was found; choose one with the disk setting.",
            {"candidates": [disk["path"] for disk in eligible], "excluded": excluded_details},
        )
    chosen = next((disk for disk in eligible if disk["path"] == install["disk"]), None)
    if chosen is None:
        rejected = next((disk for disk in excluded if disk["path"] == install["disk"]), None)
        if rejected is None:
            raise AgentError(CONFIG_INVALID, f"Disk {install['disk']} is not in the latest inventory.",
                             {"field": "disk", "disk": install["disk"]})
        raise AgentError(CONFIG_CONFLICT, f"Disk {install['disk']} cannot be installed to: {rejected['reason']}.",
                         {"field": "disk", "disk": install["disk"], "reason": rejected["reason"]})
    if sources["disk"] == "inferred":
        warnings.append(f"disk {install['disk']} was inferred as the only eligible disk; "
                        f"excluded: {_excluded_text(excluded_details)}")

    username = install.get("username")
    if isinstance(username, str) and username != username.lower():
        warnings.append(f"username {username} was lowercased to {username.lower()}")
        install["username"] = username.lower()

    for field, env_field in PASSWORD_FIELDS.items():
        reference = install.pop(field, None)
        source = sources.pop(field, None)
        if reference is not None:
            install[env_field] = reference["env"]
            install["password_source"] = "environment"
            install["generate_missing_passwords"] = True
            sources[env_field] = source

    swap_mb = install.get("swap_mb", 2048)
    if isinstance(swap_mb, int) and not isinstance(swap_mb, bool) and swap_mb * 1048576 >= chosen["size_bytes"] / 2:
        raise AgentError(CONFIG_INVALID, f"swap_mb {swap_mb} must be less than half of disk {install['disk']}.",
                         {"field": "swap_mb", "disk_size_bytes": chosen["size_bytes"]})
    install["partition_table"], install["partition_layout"] = build_layout(
        install["disk"], install["boot_mode"], swap_mb, install.get("root_filesystem", "ext4")
    )
    install["disk_must_be_erased"] = True
    install["target_is_disposable"] = target.get("target_is_disposable") is True
    install["disk_erase_authorized"] = target.get("disk_erase_authorized") is True
    for key in ("partition_table", "partition_layout", "disk_must_be_erased", "target_is_disposable",
                "disk_erase_authorized"):
        sources[key] = "derived"

    validated_settings(target, install)
    return {"install": install, "sources": sources, "warnings": warnings}
