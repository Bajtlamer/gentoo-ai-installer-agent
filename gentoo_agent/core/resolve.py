"""Resolve an install configuration from layered inputs.

Priority, highest first: overrides (from the user's request), profile,
values inferred from the inventory, fallbacks, and defaults. The result
records the source of every value. Passwords are accepted only as
environment-variable references, never as text.
"""

from __future__ import annotations

import copy
import re

from gentoo_agent.core.disks import parse_disk_identities
from gentoo_agent.core.errors import (
    CONFIG_CONFLICT,
    CONFIG_INVALID,
    DISK_AMBIGUOUS,
    INVENTORY_MISSING,
    AgentError,
)
from gentoo_agent.core.layout import build_layout
from gentoo_agent.core.validate import validated_settings


SETTABLE = {
    "username", "hostname", "timezone", "boot_mode", "init_system", "swap_mb", "disk",
    "network_method", "network_static", "create_user", "configure_sudo", "set_root_password",
    "allow_root_password_ssh", "install_user_ssh_key", "bootloader", "user_groups",
}
PASSWORD_FIELDS = {"user_password": "user_password_env", "root_password": "root_password_env"}
DERIVED = {
    "disk", "partition_layout", "partition_table", "disk_must_be_erased", "target_is_disposable",
    "disk_erase_authorized", "expected_disk_model", "expected_disk_size_gib",
}
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
SUPPORTED_BOOTLOADERS = {"grub"}


def _check_layer(name: str, layer: object, allow_passwords: bool) -> dict:
    if layer is None:
        return {}
    if not isinstance(layer, dict):
        raise AgentError(CONFIG_INVALID, f"{name} must be an object", {"layer": name})
    allowed = SETTABLE | (set(PASSWORD_FIELDS) if allow_passwords else set())
    unknown = sorted(set(layer) - allowed)
    if unknown:
        raise AgentError(CONFIG_INVALID, f"{name} contains settings that cannot be set: {', '.join(unknown)}",
                         {"layer": name, "keys": unknown})
    for field in PASSWORD_FIELDS:
        if field in layer:
            value = layer[field]
            if not (isinstance(value, dict) and set(value) == {"env"} and isinstance(value["env"], str)
                    and ENV_NAME.fullmatch(value["env"])):
                raise AgentError(
                    CONFIG_INVALID,
                    'Passwords are passed by environment-variable name, never as text: {"env": "NAME"}',
                    {"field": field},
                )
    return layer


def _live_boot_mode(results: dict) -> str:
    stdout = str(results.get("boot_mode", {}).get("stdout", "")).strip()
    return "uefi" if stdout == "uefi" else "bios"


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
    if inventory is None:
        raise AgentError(INVENTORY_MISSING, "No inventory exists; run detect_hardware with refresh first.", {})
    results = {item.get("label"): item for item in inventory.get("results", [])}
    identity = results.get("disk_identity")
    if identity is None or identity.get("returncode") != 0:
        raise AgentError(INVENTORY_MISSING, "The latest inventory has no disk identity; refresh it.", {})
    disks = [disk["path"] for disk in parse_disk_identities(identity.get("stdout", ""))]
    live_boot = _live_boot_mode(results)

    inferred: dict = {"boot_mode": live_boot}
    if len(disks) == 1:
        inferred["disk"] = disks[0]

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
    if sources.get("boot_mode") != "inferred" and install["boot_mode"] != live_boot:
        raise AgentError(
            CONFIG_CONFLICT,
            f"boot_mode {install['boot_mode']} was requested but the live system booted in {live_boot} mode.",
            {"requested": install["boot_mode"], "live": live_boot},
        )
    if "disk" not in install:
        if not disks:
            raise AgentError(INVENTORY_MISSING, "The inventory lists no disks.", {})
        raise AgentError(DISK_AMBIGUOUS, "Several disks are present; choose one with the disk setting.",
                         {"candidates": disks})

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

    install["partition_table"], install["partition_layout"] = build_layout(
        install["disk"], install["boot_mode"], install.get("swap_mb", 2048), install.get("root_filesystem", "ext4")
    )
    install["disk_must_be_erased"] = True
    install["target_is_disposable"] = target.get("target_is_disposable") is True
    install["disk_erase_authorized"] = target.get("disk_erase_authorized") is True
    for key in ("partition_table", "partition_layout", "disk_must_be_erased", "target_is_disposable",
                "disk_erase_authorized"):
        sources[key] = "derived"

    validated_settings(target, install)
    return {"install": install, "sources": sources, "warnings": warnings}
