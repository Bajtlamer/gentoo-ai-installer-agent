"""Guarded Gentoo installation phases.

The command in this module is deliberately conservative:

* ``install --dry-run`` is the default and never connects to the target.
* ``install --execute`` requires a reviewed configuration, three matching
  execution flags, an exact confirmation phrase, and a disposable target
  when disk erasure is requested.
* Commands are generated only from the validated installation configuration;
  there is no free-form remote command input.
* The target is never rebooted automatically.
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import secrets
import re
import shlex
import subprocess
import sys
import threading
import queue
import time
from pathlib import Path

from gentoo_agent.core.config import install_config, load_json, now, save_json, target_config
from gentoo_agent.core.paths import CONFIG, LOG_DIR, ROOT, STATE_DIR
from gentoo_agent.core.ssh import ssh_base


MOUNT_ROOT = "/mnt/gentoo"
PHASES = [
    "preflight",
    "storage",
    "filesystems",
    "base-system",
    "configuration",
    "authentication",
    "kernel",
    "bootloader",
    "validation",
]


def q(value: str) -> str:
    return shlex.quote(value)


def remote_shell(
    target: dict,
    label: str,
    command: str,
    timeout: int = 3600,
    stream: bool = False,
    input_data: str | None = None,
) -> dict:
    """Run one generated shell program and return an auditable result."""
    remote_command = "sh -c " + q(command)
    if stream:
        process = subprocess.Popen(
            ssh_base(target) + ["--", remote_command],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if input_data is not None else None,
            text=True,
            bufsize=1,
        )
        if input_data is not None:
            assert process.stdin is not None
            process.stdin.write(input_data)
            process.stdin.close()
        output: list[str] = []
        lines: queue.Queue[str] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            process.stdout.close()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            while reader.is_alive() or not lines.empty():
                if time.monotonic() - started > timeout:
                    process.kill()
                    raise subprocess.TimeoutExpired(remote_command, timeout)
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                output.append(line)
                print(f"    {line}", end="", flush=True)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        reader.join(timeout=1)
        returncode = process.wait(timeout=5)
        return {
            "label": label,
            "command": command,
            "returncode": returncode,
            "stdout": "".join(output),
            "stderr": "",
            "finished_at": now(),
        }
    completed = subprocess.run(
        ssh_base(target) + ["--", remote_command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        input=input_data,
        timeout=timeout,
        check=False,
    )
    return {
        "label": label,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "finished_at": now(),
    }


def load_plan() -> dict:
    return load_json(CONFIG / "plan.json")


def state_path() -> Path:
    return STATE_DIR / "state.json"


def load_state() -> dict:
    path = state_path()
    return load_json(path) if path.exists() else {
        "schema_version": 1,
        "completed_phases": [],
    }


def save_state(state: dict) -> None:
    state["updated_at"] = now()
    save_json(state_path(), state)


def disk_partition(disk: str, number: int) -> str:
    return f"{disk}{'p' if disk[-1:].isdigit() else ''}{number}"


def validated_settings(target: dict, install: dict) -> tuple[str, list[dict]]:
    disk = install.get("disk", "")
    if not re.fullmatch(r"/dev/[A-Za-z0-9._+/-]+", disk):
        raise SystemExit(f"Unsafe disk path in configuration: {disk!r}")
    layout = install.get("partition_layout")
    if not isinstance(layout, list) or not layout:
        raise SystemExit("partition_layout is empty")
    for item in layout:
        device = item.get("device", "")
        if not re.fullmatch(r"/dev/[A-Za-z0-9._+/-]+", device):
            raise SystemExit(f"Unsafe partition path in configuration: {device!r}")
    if target.get("user") != "root":
        raise SystemExit("The installation executor currently requires SSH user root.")
    if install.get("boot_mode") == "uefi" and install.get("partition_table") != "gpt":
        raise SystemExit("UEFI requires GPT.")
    if install.get("root_filesystem") != "ext4":
        raise SystemExit("The executor currently supports ext4 root filesystems only.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", str(install.get("hostname", ""))):
        raise SystemExit("hostname must be a simple DNS-compatible hostname")
    if not re.fullmatch(r"[A-Za-z0-9._+/-]+", str(install.get("timezone", ""))):
        raise SystemExit("timezone contains unsafe characters")
    create_user = install.get("create_user", True)
    set_root_password = install.get("set_root_password", True)
    install_user_ssh_key = install.get("install_user_ssh_key", True)
    configure_sudo = install.get("configure_sudo", True)
    allow_root_password_ssh = install.get("allow_root_password_ssh", False)
    if not isinstance(create_user, bool):
        raise SystemExit("create_user must be boolean")
    if not isinstance(set_root_password, bool):
        raise SystemExit("set_root_password must be boolean")
    if not isinstance(install_user_ssh_key, bool):
        raise SystemExit("install_user_ssh_key must be boolean")
    if not isinstance(configure_sudo, bool):
        raise SystemExit("configure_sudo must be boolean")
    if not isinstance(allow_root_password_ssh, bool):
        raise SystemExit("allow_root_password_ssh must be boolean")
    generate_missing_passwords = install.get("generate_missing_passwords", True)
    if not isinstance(generate_missing_passwords, bool):
        raise SystemExit("generate_missing_passwords must be boolean")
    if install_user_ssh_key and not create_user:
        raise SystemExit("install_user_ssh_key requires create_user=true")
    password_source = install.get("password_source", "environment")
    if password_source not in {"prompt", "environment", "generate"}:
        raise SystemExit("password_source must be 'prompt', 'environment', or 'generate'")
    if password_source == "environment":
        for field in ("root_password_env", "user_password_env"):
            env_name = str(install.get(field, ""))
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", env_name):
                raise SystemExit(f"{field} must contain a valid uppercase environment-variable name")
    if not create_user and not set_root_password:
        raise SystemExit("at least one installed password must be configured")
    if create_user:
        username = str(install.get("username", ""))
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) or username == "root":
            raise SystemExit("username must be a non-root Linux account name")
        groups = install.get("user_groups", ["wheel"])
        if not isinstance(groups, list) or any(
            not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(group)) for group in groups
        ):
            raise SystemExit("user_groups must contain simple Linux group names")
        if configure_sudo and "wheel" not in [str(group) for group in groups]:
            raise SystemExit("configure_sudo requires wheel in user_groups")
        shell = str(install.get("user_shell", "/bin/bash"))
        if shell != "/bin/bash":
            raise SystemExit("user_shell must be /bin/bash; other shells are not installed by this workflow")
    return disk, layout


def public_key_for_target(target: dict) -> str:
    identity = target.get("identity_file")
    if not identity:
        raise SystemExit("target.identity_file is required for installed SSH access")
    private_path = Path(identity).expanduser()
    candidates = [Path(str(private_path) + ".pub")]
    if private_path.suffix:
        candidates.append(private_path.with_suffix(".pub"))
    public_path = next((path for path in candidates if path.exists()), None)
    if public_path is None:
        raise SystemExit(f"Missing SSH public key next to {private_path}; expected {candidates[0]}")
    public_key = public_path.read_text(encoding="utf-8").strip()
    if not public_key or "\n" in public_key:
        raise SystemExit(f"SSH public key must contain exactly one line: {public_path}")
    return public_key


def partition_script(install: dict) -> str:
    disk, layout = validated_settings(target_config(), install)
    label = "dos" if install["partition_table"] == "mbr" else install["partition_table"]
    lines = [f"label: {label}", "unit: sectors"]
    for item in layout:
        device = item["device"]
        size = item.get("size_mib")
        kind = item.get("type")
        if install["partition_table"] == "mbr":
            code = "82" if kind == "linux-swap" else "83"
        else:
            code = "U" if kind == "efi-system" else ("S" if kind == "linux-swap" else "L")
        size_clause = f"size={size}M, " if size else ""
        lines.append(f"{device} : {size_clause}type={code}")
    return "\n".join(lines) + "\n"


def preflight_commands(target: dict, install: dict) -> list[str]:
    disk, _ = validated_settings(target, install)
    expected_boot = install["boot_mode"]
    commands = [
        "set -eu",
        f"disk={q(disk)}",
        "test \"$(id -u)\" -eq 0",
        "test -b \"$disk\"",
        "test \"$(lsblk -dn -o TYPE \"$disk\")\" = disk",
        f"test \"$(if [ -d /sys/firmware/efi ]; then printf uefi; else printf bios; fi)\" = {q(expected_boot)}",
        "command -v lsblk >/dev/null",
        "command -v fdisk >/dev/null",
        "command -v sfdisk >/dev/null",
        "command -v wipefs >/dev/null",
        "command -v mkfs.ext4 >/dev/null",
        "command -v mkswap >/dev/null",
        "command -v mount >/dev/null",
        "command -v tar >/dev/null",
        "command -v wget >/dev/null",
        "command -v sha256sum >/dev/null",
        "command -v chroot >/dev/null",
        "ip route | grep -q default",
        f"printf 'preflight-ok boot={q(expected_boot)} disk={q(disk)}\\n'",
    ]
    expected_model = install.get("expected_disk_model")
    if expected_model:
        commands.insert(-1, f"test \"$(lsblk -dn -o MODEL \"$disk\")\" = {q(expected_model)}")
    expected_size = install.get("expected_disk_size_gib")
    if expected_size:
        expected_bytes = int(float(expected_size) * 1024 ** 3)
        commands.insert(-1, "actual_bytes=$(lsblk -b -dn -o SIZE \"$disk\")")
        commands.insert(-1, f"test $(( actual_bytes > {expected_bytes - 1024 ** 3} && actual_bytes < {expected_bytes + 1024 ** 3} )) -eq 1")
    return commands


def storage_command(install: dict) -> str:
    disk, _ = validated_settings(target_config(), install)
    script = partition_script(install)
    encoded = q(script)
    _, layout = validated_settings(target_config(), install)
    root = next(item["device"] for item in layout if item.get("mount") == "/")
    swap = next((item["device"] for item in layout if item.get("type") == "linux-swap"), None)
    recovery_checks = [
        f"test \"$(findmnt -rn -o SOURCE -T {q(MOUNT_ROOT)})\" = {q(root)}",
    ]
    if swap:
        recovery_checks.append(
            f"swapon --show=NAME --noheadings | awk -v expected={q(swap)} '$1 == expected {{found=1}} END {{exit found ? 0 : 1}}'"
        )
    recovery_checks_text = "\n".join(recovery_checks)
    return "\n".join([
        "set -eu",
        f"disk={q(disk)}",
        "test -b \"$disk\"",
        "if lsblk -nr -o MOUNTPOINTS \"$disk\" | grep -q '[^[:space:]]' || swapon --show=NAME --noheadings | awk -v disk=\"$disk\" '$1 != disk && index($1, disk) == 1 && substr($1, length(disk) + 1) ~ /^p?[0-9]+$/ {found=1} END {exit found ? 0 : 1}'; then",
        f"  {recovery_checks_text}",
        f"  printf %s {encoded} | sfdisk --no-reread --force \"$disk\"",
        "else",
        "  wipefs -a -f \"$disk\"",
        f"  printf %s {encoded} | sfdisk --wipe always \"$disk\"",
        "fi",
        "partprobe \"$disk\" || true",
        "udevadm settle || true",
        "lsblk -o NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS \"$disk\"",
    ])


def filesystem_command(install: dict) -> str:
    disk, layout = validated_settings(target_config(), install)
    root = next(item["device"] for item in layout if item.get("mount") == "/")
    swap = next((item["device"] for item in layout if item.get("type") == "linux-swap"), None)
    efi = next((item["device"] for item in layout if item.get("type") == "efi-system"), None)
    lines = ["set -eu", f"root_device={q(root)}", f"mount_root={q(MOUNT_ROOT)}", "mkdir -p \"$mount_root\""]
    lines += [
        "if ! blkid -o value -s TYPE \"$root_device\" 2>/dev/null | grep -qx ext4; then mkfs.ext4 -F \"$root_device\"; fi",
        "if ! findmnt -rn -S \"$root_device\" >/dev/null; then mount \"$root_device\" \"$mount_root\"; fi",
    ]
    if swap:
        lines += [
            f"swap_device={q(swap)}",
            "if ! blkid -o value -s TYPE \"$swap_device\" 2>/dev/null | grep -qx swap; then mkswap -f \"$swap_device\"; fi",
            "swapon \"$swap_device\" || true",
        ]
    if efi:
        lines += [
            f"efi_device={q(efi)}",
            "mkdir -p \"$mount_root/boot\"",
            "if ! blkid -o value -s TYPE \"$efi_device\" 2>/dev/null | grep -Eq 'vfat|fat32'; then mkfs.vfat -F 32 \"$efi_device\"; fi",
            "if ! findmnt -rn -T \"$mount_root/boot\" >/dev/null; then mount \"$efi_device\" \"$mount_root/boot\"; fi",
        ]
    lines += ["findmnt -D \"$mount_root\"", "blkid"]
    return "\n".join(lines)


def chroot(command: str) -> str:
    return f"chroot {q(MOUNT_ROOT)} /bin/bash -c {q('set -e\n' + command)}"


def base_system_command(install: dict) -> str:
    variant = "openrc" if install.get("init_system") == "openrc" else "systemd"
    stage_list = f"https://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3-amd64-{variant}.txt"
    stage_base = "https://distfiles.gentoo.org/releases/amd64/autobuilds/"
    inside = "\n".join([
        "mkdir -p /var/db/repos/gentoo",
        "emerge-webrsync",
    ])
    return "\n".join([
        "set -eu",
        "mkdir -p /mnt/gentoo/dev /mnt/gentoo/proc /mnt/gentoo/sys /mnt/gentoo/run",
        f"stage_list={q(stage_list)}",
        f"stage_base={q(stage_base)}",
        "if [ ! -e /mnt/gentoo/etc/gentoo-release ]; then",
        "  stage_path=$(wget -qO- \"$stage_list\" | awk '$1 ~ /\\.tar\\.xz$/ {print $1; exit}')",
        "  test -n \"$stage_path\"",
        "  stage_name=${stage_path##*/}",
        "  stage_file=/mnt/gentoo/$stage_name",
        "  wget -O \"$stage_file\" \"${stage_base}${stage_path}\"",
        "  wget -O \"${stage_file}.sha256\" \"${stage_base}${stage_path}.sha256\"",
        "  (cd /mnt/gentoo && sha256sum -c \"${stage_name}.sha256\")",
        "  tar -xpf \"$stage_file\" -C /mnt/gentoo --xattrs-include='*.*' --numeric-owner",
        "  rm -f \"$stage_file\" \"${stage_file}.sha256\"",
        "fi",
        "rm -f /mnt/gentoo/etc/resolv.conf",
        "cp -L /etc/resolv.conf /mnt/gentoo/etc/resolv.conf",
        "mount --rbind /dev /mnt/gentoo/dev",
        "mount --make-rslave /mnt/gentoo/dev",
        "mount -t proc /proc /mnt/gentoo/proc || true",
        "mount --rbind /sys /mnt/gentoo/sys",
        "mount --make-rslave /mnt/gentoo/sys",
        "mount --rbind /run /mnt/gentoo/run || true",
        "mount --make-rslave /mnt/gentoo/run || true",
        chroot(inside),
        "touch /mnt/gentoo/.gentoo-agent-base-complete",
    ])


def configuration_command(target: dict, install: dict) -> str:
    hostname = q(install["hostname"])
    timezone_value = install["timezone"]
    network = install.get("network_method", "dhcp")
    public_key = public_key_for_target(target)
    key_line = q(public_key)
    root_login_policy = "yes" if install.get("allow_root_password_ssh", False) else "prohibit-password"
    ssh_setup = "\n".join([
        "emerge --noreplace --verbose net-misc/openssh",
        "install -d -m 700 /root/.ssh",
        f"printf '%s\\n' {key_line} > /root/.ssh/authorized_keys",
        "chmod 600 /root/.ssh/authorized_keys",
        f"printf '%s\\n' 'PermitRootLogin {root_login_policy}' 'PubkeyAuthentication yes' 'PasswordAuthentication yes' 'UsePAM yes' > /etc/ssh/sshd_config.d/gentoo-agent.conf",
        "rc-update add sshd default",
    ])
    if network == "dhcp":
        network_setup = "emerge --noreplace --verbose net-misc/dhcpcd\nrc-update add dhcpcd default"
    else:
        static = install.get("network_static") or {}
        address = q(static.get("address", ""))
        gateway = q(static.get("gateway", ""))
        dns_lines = "\\n".join(q(item) for item in static.get("dns", []))
        network_setup = "\n".join([
            "emerge --noreplace --verbose net-misc/dhcpcd",
            "install -d /etc/dhcpcd.conf.d",
            f"printf '%s\\n' 'interface ens33' 'static ip_address={address}' 'static routers={gateway}' > /etc/dhcpcd.conf.d/gentoo-agent.conf",
            f"printf '%s\\n' {dns_lines} | sed 's#^#static domain_name_servers #' >> /etc/dhcpcd.conf.d/gentoo-agent.conf",
            "rc-update add dhcpcd default",
        ])
    inside = "\n".join([
        f"printf '%s\\n' {hostname} > /etc/hostname",
        f"ln -snf /usr/share/zoneinfo/{timezone_value} /etc/localtime",
        "printf '%s\\n' 'en_US.UTF-8 UTF-8' > /etc/locale.gen",
        "locale-gen",
        "eselect locale set en_US.utf8 || true",
        f"root_uuid=$(blkid -s UUID -o value {q(next(item['device'] for item in install['partition_layout'] if item.get('mount') == '/'))})",
        f"swap_uuid=$(blkid -s UUID -o value {q(next((item['device'] for item in install['partition_layout'] if item.get('type') == 'linux-swap'), ''))} 2>/dev/null || true)",
        "printf '%s\\n' \"UUID=$root_uuid / ext4 noatime 0 1\" > /etc/fstab",
        "if [ -n \"$swap_uuid\" ]; then printf '%s\\n' \"UUID=$swap_uuid none swap sw 0 0\" >> /etc/fstab; fi",
        network_setup,
        ssh_setup,
        "env-update",
        "source /etc/profile || true",
    ])
    return "\n".join([chroot(inside), f"touch {q(MOUNT_ROOT + '/.gentoo-agent-configuration-complete')}"])


def authentication_check_command(target: dict, install: dict) -> str:
    public_key = public_key_for_target(target)
    create_user = install.get("create_user", True)
    set_root_password = install.get("set_root_password", True)
    install_user_ssh_key = install.get("install_user_ssh_key", True)
    configure_sudo = install.get("configure_sudo", True)
    root_login_policy = "yes" if install.get("allow_root_password_ssh", False) else "prohibit-password"
    lines = [
        "set -eu",
        f"test -e {q(MOUNT_ROOT + '/.gentoo-agent-authentication-complete')}",
        f"test -x {q(MOUNT_ROOT + '/usr/sbin/sshd')}",
        f"test -s {q(MOUNT_ROOT + '/root/.ssh/authorized_keys')}",
        f"grep -Fqx {q(public_key)} {q(MOUNT_ROOT + '/root/.ssh/authorized_keys')}",
        f"grep -Fqx 'PermitRootLogin {root_login_policy}' {q(MOUNT_ROOT + '/etc/ssh/sshd_config.d/gentoo-agent.conf')}",
        f"test -e {q(MOUNT_ROOT + '/etc/runlevels/default/sshd')}",
        f"test -e {q(MOUNT_ROOT + '/etc/runlevels/default/dhcpcd')}",
    ]
    if set_root_password:
        lines.append(
            f"awk -F: '$1 == \"root\" && $2 !~ /^(!|\\*|$)/ {{found=1}} END {{exit found ? 0 : 1}}' {q(MOUNT_ROOT + '/etc/shadow')}"
        )
    if create_user:
        username = q(str(install.get("username", "")))
        lines += [
            f"awk -F: -v user={username} '$1 == user {{found=1}} END {{exit found ? 0 : 1}}' {q(MOUNT_ROOT + '/etc/passwd')}",
            f"test -d {q(MOUNT_ROOT + '/home/' + str(install.get('username', '')))}",
            f"awk -F: -v user={username} '$1 == user && $2 !~ /^(!|\\*|$)/ {{found=1}} END {{exit found ? 0 : 1}}' {q(MOUNT_ROOT + '/etc/shadow')}",
        ]
        if install_user_ssh_key:
            user_home = MOUNT_ROOT + "/home/" + str(install.get("username", ""))
            lines += [
                f"test -s {q(user_home + '/.ssh/authorized_keys')}",
                f"grep -Fqx {q(public_key)} {q(user_home + '/.ssh/authorized_keys')}",
            ]
        if configure_sudo:
            lines += [
                f"test -x {q(MOUNT_ROOT + '/usr/bin/sudo')}",
                f"grep -Fqx '%wheel ALL=(ALL:ALL) ALL' {q(MOUNT_ROOT + '/etc/sudoers')}",
            ]
    lines.append("printf 'authentication-ok\\n'")
    return "\n".join(lines)


def authentication_command(target: dict, install: dict) -> str:
    create_user = install.get("create_user", True)
    username = str(install.get("username", "gentoo"))
    user_shell = str(install.get("user_shell", "/bin/bash"))
    groups = [str(group) for group in install.get("user_groups", ["wheel"])]
    install_user_ssh_key = install.get("install_user_ssh_key", True)
    configure_sudo = install.get("configure_sudo", True)
    inside = ["set -eu"]
    if create_user:
        group_args = f" -G {q(','.join(groups))}" if groups else ""
        inside += [
            f"if ! getent passwd {q(username)} >/dev/null; then useradd -m -s {q(user_shell)}{group_args} {q(username)}; else usermod -s {q(user_shell)} {q(username)}; fi",
        ]
        if groups:
            inside.append(f"usermod -aG {q(','.join(groups))} {q(username)}")
        if install_user_ssh_key:
            public_key = q(public_key_for_target(target))
            inside += [
                f"install -d -m 700 -o {q(username)} -g {q(username)} /home/{q(username)}/.ssh",
                f"printf '%s\\n' {public_key} > /home/{q(username)}/.ssh/authorized_keys",
                f"chown {q(username)}:{q(username)} /home/{q(username)}/.ssh/authorized_keys",
                f"chmod 600 /home/{q(username)}/.ssh/authorized_keys",
            ]
    if configure_sudo:
        inside += [
            "emerge --noreplace --verbose app-admin/sudo",
            "sudo_rule='%wheel ALL=(ALL:ALL) ALL'",
            "if ! grep -Fqx \"$sudo_rule\" /etc/sudoers; then printf '%s\\n' \"$sudo_rule\" >> /etc/sudoers; fi",
            "/usr/sbin/visudo -cf /etc/sudoers",
        ]
    if install.get("set_root_password", True) or create_user:
        inside.append("chpasswd")
    return "\n".join([
        chroot("\n".join(inside)),
        f"touch {q(MOUNT_ROOT + '/.gentoo-agent-authentication-complete')}",
    ])


def prompt_authentication_input(install: dict) -> tuple[str | None, dict[str, str]]:
    password_source = install.get("password_source", "environment")
    generated: dict[str, str] = {}

    def obtain(label: str, env_field: str) -> str:
        if password_source == "environment":
            env_name = str(install.get(env_field, ""))
            value = os.environ.get(env_name)
            if value is None:
                if not install.get("generate_missing_passwords", True):
                    raise SystemExit(f"Missing required password environment variable: {env_name}")
                value = secrets.token_urlsafe(24)
                generated[label] = value
            if not value or "\r" in value or "\n" in value:
                raise SystemExit(f"Password from {env_name} is empty or contains a newline")
            return value
        if password_source == "generate":
            value = secrets.token_urlsafe(24)
            generated[label] = value
            return value
        first = getpass.getpass(f"New Gentoo {label} password: ")
        second = getpass.getpass(f"Repeat Gentoo {label} password: ")
        if not first or first != second:
            raise SystemExit(f"Passwords for {label} were empty or did not match.")
        return first

    records: list[str] = []
    if install.get("set_root_password", True):
        records.append(f"root:{obtain('root', 'root_password_env')}")
    if install.get("create_user", True):
        username = install.get("username", "gentoo")
        records.append(f"{username}:{obtain(str(username), 'user_password_env')}")
    return ("\n".join(records) + "\n" if records else None), generated


def kernel_command(install: dict) -> str:
    inside = "\n".join([
        "mkdir -p /etc/portage/package.use",
        "printf '%s\\n' 'sys-kernel/installkernel dracut' > /etc/portage/package.use/gentoo-agent-installkernel",
        "emerge --noreplace --verbose sys-kernel/gentoo-kernel-bin",
        "test -n \"$(find /boot -maxdepth 1 -type f -name 'vmlinuz*' -print -quit)\"",
    ])
    return chroot(inside)


def bootloader_command(install: dict) -> str:
    disk, _ = validated_settings(target_config(), install)
    if install["boot_mode"] == "bios":
        inside = "\n".join([
            "emerge --noreplace --verbose sys-boot/grub",
            f"grub-install --target=i386-pc {q(disk)}",
            "grub-mkconfig -o /boot/grub/grub.cfg",
        ])
    else:
        inside = "\n".join([
            "emerge --noreplace --verbose sys-boot/grub",
            "grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=Gentoo",
            "grub-mkconfig -o /boot/grub/grub.cfg",
        ])
    return chroot(inside)


def validation_command(install: dict) -> str:
    disk, layout = validated_settings(target_config(), install)
    root = next(item["device"] for item in layout if item.get("mount") == "/")
    checks = [
        "set -eu",
        f"test -b {q(root)}",
        f"test -f {q(MOUNT_ROOT + '/etc/fstab')}",
        f"test -f {q(MOUNT_ROOT + '/etc/hostname')}",
        f"test -e {q(MOUNT_ROOT + '/.gentoo-agent-authentication-complete')}",
        f"test -x {q(MOUNT_ROOT + '/usr/sbin/sshd')}",
        f"test -e {q(MOUNT_ROOT + '/etc/runlevels/default/sshd')}",
        f"test -e {q(MOUNT_ROOT + '/etc/runlevels/default/dhcpcd')}",
        f"test -s {q(MOUNT_ROOT + '/boot/grub/grub.cfg')}",
        f"test -n \"$(find {q(MOUNT_ROOT + '/boot')} -maxdepth 1 -type f -name 'vmlinuz*' -print -quit)\"",
        f"lsblk {q(disk)}",
        f"findmnt -D {q(MOUNT_ROOT)}",
        "printf 'validation-ok\\n'",
    ]
    return "\n".join(checks)


def storage_check_command(install: dict) -> str:
    disk, layout = validated_settings(target_config(), install)
    lines = [
        "set -eu",
        f"disk={q(disk)}",
        "test -b \"$disk\"",
        "test \"$(lsblk -dn -o TYPE \"$disk\")\" = disk",
        f"fdisk -l \"$disk\" | grep -Eq 'Disklabel type: {'dos' if install['partition_table'] == 'mbr' else 'gpt'}$'",
    ]
    for item in layout:
        device = item["device"]
        lines.append(
            f"lsblk -nr -o PATH,TYPE \"$disk\" | awk -v p={q(device)} '$1 == p && $2 == \"part\" {{found=1}} END {{exit found ? 0 : 1}}'"
        )
    lines.append("printf 'storage-ok\\n'")
    return "\n".join(lines)


def filesystem_check_command(install: dict) -> str:
    _, layout = validated_settings(target_config(), install)
    root = next(item["device"] for item in layout if item.get("mount") == "/")
    swap = next((item["device"] for item in layout if item.get("type") == "linux-swap"), None)
    efi = next((item["device"] for item in layout if item.get("type") == "efi-system"), None)
    lines = [
        "set -eu",
        f"test \"$(blkid -o value -s TYPE {q(root)})\" = ext4",
        f"test \"$(findmnt -rn -o SOURCE -T {q(MOUNT_ROOT)})\" = {q(root)}",
    ]
    if swap:
        lines.append(f"test \"$(blkid -o value -s TYPE {q(swap)})\" = swap")
    if efi:
        lines += [
            f"test \"$(blkid -o value -s TYPE {q(efi)})\" = vfat",
            f"test \"$(findmnt -rn -o SOURCE -T {q(MOUNT_ROOT + '/boot')})\" = {q(efi)}",
        ]
    lines.append("printf 'filesystems-ok\\n'")
    return "\n".join(lines)


def base_system_check_command() -> str:
    return "\n".join([
        "set -eu",
        f"test -x {q(MOUNT_ROOT + '/bin/bash')}",
        f"test -e {q(MOUNT_ROOT + '/etc/gentoo-release')}",
        f"test -e {q(MOUNT_ROOT + '/.gentoo-agent-base-complete')}",
        "printf 'base-system-ok\\n'",
    ])


def configuration_check_command(target: dict, install: dict) -> str:
    public_key = public_key_for_target(target)
    root_login_policy = "yes" if install.get("allow_root_password_ssh", False) else "prohibit-password"
    return "\n".join([
        "set -eu",
        f"test -e {q(MOUNT_ROOT + '/.gentoo-agent-configuration-complete')}",
        f"test -s {q(MOUNT_ROOT + '/etc/fstab')}",
        f"test -s {q(MOUNT_ROOT + '/etc/hostname')}",
        f"test -x {q(MOUNT_ROOT + '/usr/sbin/sshd')}",
        f"test -s {q(MOUNT_ROOT + '/root/.ssh/authorized_keys')}",
        f"grep -Fqx {q(public_key)} {q(MOUNT_ROOT + '/root/.ssh/authorized_keys')}",
        f"grep -Fqx 'PermitRootLogin {root_login_policy}' {q(MOUNT_ROOT + '/etc/ssh/sshd_config.d/gentoo-agent.conf')}",
        f"grep -Fqx 'PasswordAuthentication yes' {q(MOUNT_ROOT + '/etc/ssh/sshd_config.d/gentoo-agent.conf')}",
        f"grep -Fqx 'UsePAM yes' {q(MOUNT_ROOT + '/etc/ssh/sshd_config.d/gentoo-agent.conf')}",
        f"test -e {q(MOUNT_ROOT + '/etc/runlevels/default/sshd')}",
        f"test -e {q(MOUNT_ROOT + '/etc/runlevels/default/dhcpcd')}",
        "printf 'configuration-ok\\n'",
    ])


def kernel_check_command() -> str:
    return "\n".join([
        "set -eu",
        f"test -n \"$(find {q(MOUNT_ROOT + '/boot')} -maxdepth 1 -type f -name 'vmlinuz*' -print -quit)\"",
        "printf 'kernel-ok\\n'",
    ])


def bootloader_check_command(install: dict) -> str:
    lines = ["set -eu", f"test -s {q(MOUNT_ROOT + '/boot/grub/grub.cfg')}" ]
    if install["boot_mode"] == "bios":
        lines.append(f"test -e {q(MOUNT_ROOT + '/boot/grub/i386-pc/core.img')}")
    else:
        lines.append(f"test -e {q(MOUNT_ROOT + '/boot/EFI/Gentoo/grubx64.efi')}")
    lines.append("printf 'bootloader-ok\\n'")
    return "\n".join(lines)


def check_command_for(phase: str, target: dict, install: dict) -> str:
    if phase == "preflight":
        return "\n".join(preflight_commands(target, install))
    if phase == "storage":
        return storage_check_command(install)
    if phase == "filesystems":
        return filesystem_check_command(install)
    if phase == "base-system":
        return base_system_check_command()
    if phase == "configuration":
        return configuration_check_command(target, install)
    if phase == "authentication":
        return authentication_check_command(target, install)
    if phase == "kernel":
        return kernel_check_command()
    if phase == "bootloader":
        return bootloader_check_command(install)
    if phase == "validation":
        return validation_command(install)
    raise SystemExit(f"Unknown installation phase: {phase}")


def command_for(phase: str, target: dict, install: dict) -> str:
    if phase == "preflight":
        return "\n".join(preflight_commands(target, install))
    if phase == "storage":
        return storage_command(install)
    if phase == "filesystems":
        return filesystem_command(install)
    if phase == "base-system":
        return base_system_command(install)
    if phase == "configuration":
        return configuration_command(target, install)
    if phase == "authentication":
        return authentication_command(target, install)
    if phase == "kernel":
        return kernel_command(install)
    if phase == "bootloader":
        return bootloader_command(install)
    if phase == "validation":
        return validation_command(install)
    raise SystemExit(f"Unknown installation phase: {phase}")


def dry_run(target: dict, install: dict, phases: list[str]) -> None:
    print(f"Target: {target['user']}@{target['host']}:{target.get('port', 22)}")
    print(f"Disk:   {install['disk']} ({install.get('boot_mode')}/{install.get('partition_table')})")
    print("Mode:   DRY RUN - no SSH connection and no target changes")
    for phase in phases:
        print(f"\n===== {phase} =====")
        print(command_for(phase, target, install))


def execution_gate(target: dict, install: dict, plan: dict, confirmation: str) -> None:
    expected = f"INSTALL {target['name']} ON {install['disk']}"
    if confirmation != expected:
        raise SystemExit(f"Refusing execution. Use --confirm {expected!r} after review.")
    if install.get("review_status") != "approved_for_execution":
        raise SystemExit("Installation is not approved. Run the review/approval command first.")
    if not all(item.get("execution_enabled") is True for item in (target, install, plan)):
        raise SystemExit("Execution is disabled in target.json, install.json, or plan.json.")
    if install.get("disk_must_be_erased") and not (
        target.get("target_is_disposable")
        and target.get("disk_erase_authorized")
        and install.get("target_is_disposable")
        and install.get("disk_erase_authorized")
    ):
        raise SystemExit("Disk erase is not fully authorized for this target.")


def execute(target: dict, install: dict, phases: list[str]) -> int:
    state = load_state()
    state.update({"target": target["name"], "status": "running", "current_phase": phases[0]})
    state.setdefault("completed_phases", [])
    save_state(state)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = []
    generated_credentials: dict[str, str] = {}
    log_path = LOG_DIR / f"install-{target['name']}-{stamp}.json"

    def record(result: dict, kind: str) -> None:
        result["kind"] = kind
        results.append(result)
        save_json(log_path, {
            "target": target["name"],
            "started_at": stamp,
            "finished_at": now(),
            "phases": results,
        })

    try:
        for phase in phases:
            state["current_phase"] = phase
            save_state(state)
            print(f"===== checking {phase} =====", flush=True)
            print(f"  {now()} read-only state check", flush=True)
            check = remote_shell(target, f"{phase}-check", check_command_for(phase, target, install), stream=True)
            record(check, "check")
            if check["returncode"] == 0:
                print(f"{phase}: already complete; skipped")
                if phase not in state["completed_phases"]:
                    state["completed_phases"].append(phase)
                save_state(state)
                continue

            if phase in state["completed_phases"]:
                state["completed_phases"].remove(phase)
            print(f"===== applying {phase} (live output) =====", flush=True)
            print(f"  {now()} sending approved commands to target", flush=True)
            input_data = None
            if phase == "authentication":
                input_data, phase_generated = prompt_authentication_input(install)
                generated_credentials.update(phase_generated)
            result = remote_shell(
                target,
                phase,
                command_for(phase, target, install),
                stream=True,
                input_data=input_data,
            )
            record(result, "apply")
            if result["returncode"] != 0:
                state.update({"status": "failed", "last_error": f"{phase} apply returned {result['returncode']}"})
                save_state(state)
                print(result["stderr"], file=sys.stderr, end="")
                return result["returncode"] or 1

            print(f"===== verifying {phase} =====", flush=True)
            print(f"  {now()} read-only postcondition check", flush=True)
            verify = remote_shell(target, f"{phase}-verify", check_command_for(phase, target, install), stream=True)
            record(verify, "verify")
            if verify["returncode"] != 0:
                state.update({"status": "failed", "last_error": f"{phase} verification returned {verify['returncode']}"})
                save_state(state)
                print(verify["stderr"], file=sys.stderr, end="")
                return verify["returncode"] or 1
            if phase not in state["completed_phases"]:
                state["completed_phases"].append(phase)
            save_state(state)
        state.update({"status": "installed_pending_reboot", "current_phase": "validation", "last_error": None})
        save_state(state)
        print("Installation phases completed. The target was not rebooted.")
        if generated_credentials:
            print("Generated credentials (save them now; they are not stored by the agent):")
            for username, password in generated_credentials.items():
                print(f"  {username}: {password}")
        print("Review the target through the console before rebooting it.")
        return 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        state.update({"status": "failed", "last_error": str(exc)})
        save_state(state)
        raise SystemExit(f"Installation stopped: {exc}") from exc


def cmd_install(args) -> int:
    target = target_config()
    install = install_config()
    plan = load_plan()
    validated_settings(target, install)
    if getattr(args, "execute", False) and getattr(args, "dry_run", False):
        raise SystemExit("Choose either --execute or --dry-run, not both.")
    requested_phase = getattr(args, "from_phase", None)
    if requested_phase is None:
        requested_phase = PHASES[0]
        print("Starting from the beginning; completed phases will be checked and skipped.")
    if requested_phase not in PHASES:
        raise SystemExit(f"Unknown start phase: {requested_phase}")
    phases = PHASES[PHASES.index(requested_phase):]
    if not getattr(args, "execute", False):
        dry_run(target, install, phases)
        return 0
    execution_gate(target, install, plan, args.confirm)
    return execute(target, install, phases)


def approve_execution(_: object) -> int:
    target = target_config()
    install = install_config()
    plan = load_plan()
    expected = f"APPROVE INSTALL {target['name']} ON {install['disk']}"
    print("Review target.json, install.json, the latest inventory, and the dry-run output first.")
    if input(f"Type {expected}: ").strip() != expected:
        raise SystemExit("Approval not recorded.")
    install["review_status"] = "approved_for_execution"
    install["execution_enabled"] = True
    target["execution_enabled"] = True
    plan["execution_enabled"] = True
    plan["review_status"] = "approved_for_execution"
    save_json(CONFIG / "install.json", install)
    save_json(CONFIG / "target.json", target)
    save_json(CONFIG / "plan.json", plan)
    print("Execution enabled in all three local configuration files.")
    print(f"Start with: python tools/gentoo_agent.py install --execute --confirm {('INSTALL ' + target['name'] + ' ON ' + install['disk'])!r}")
    return 0
