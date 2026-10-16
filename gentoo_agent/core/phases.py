"""Per-phase shell program generators: apply programs and read-only checks.

Programs are built only from validated configuration values and quoted with
shlex; there is no free-form remote command input. The runner executes them.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from gentoo_agent.core.arch import STAGE_BASE, STAGE_LIST, architecture_spec
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.validate import validated_settings


MOUNT_ROOT = "/mnt/gentoo"
DHCPCD_CONF = "/etc/dhcpcd.conf"
DHCPCD_BEGIN = "# gentoo-agent static network begin"
DHCPCD_END = "# gentoo-agent static network end"
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


def disk_partition(disk: str, number: int) -> str:
    return f"{disk}{'p' if disk[-1:].isdigit() else ''}{number}"


def public_key_for_target(target: dict) -> str:
    identity = target.get("identity_file")
    if not identity:
        raise AgentError(CONFIG_INVALID, "target.identity_file is required for installed SSH access", {"field": "identity_file"})
    private_path = Path(identity).expanduser()
    candidates = [Path(str(private_path) + ".pub")]
    if private_path.suffix:
        candidates.append(private_path.with_suffix(".pub"))
    public_path = next((path for path in candidates if path.exists()), None)
    if public_path is None:
        raise AgentError(
            CONFIG_INVALID,
            f"Missing SSH public key next to {private_path}; expected {candidates[0]}",
            {"field": "identity_file"},
        )
    public_key = public_path.read_text(encoding="utf-8").strip()
    if not public_key or "\n" in public_key:
        raise AgentError(CONFIG_INVALID, f"SSH public key must contain exactly one line: {public_path}", {"field": "identity_file"})
    return public_key


def partition_script(target: dict, install: dict) -> str:
    disk, layout = validated_settings(target, install)
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
        f"test \"$(uname -m)\" = {q(architecture_spec(install.get('architecture'))['machine'])}",
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


def storage_command(target: dict, install: dict) -> str:
    disk, _ = validated_settings(target, install)
    script = partition_script(target, install)
    encoded = q(script)
    _, layout = validated_settings(target, install)
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


def filesystem_command(target: dict, install: dict) -> str:
    disk, layout = validated_settings(target, install)
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
    architecture = install.get("architecture")
    architecture_spec(architecture)
    stage_list = STAGE_LIST.format(arch=architecture)
    stage_base = STAGE_BASE.format(arch=architecture)
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
        interface = str(static.get("interface", ""))
        address = str(static.get("address", ""))
        gateway = str(static.get("gateway", ""))
        dns = " ".join(str(item) for item in static.get("dns", []))
        # Quote each dhcpcd.conf line exactly once, as a whole.
        block = " ".join(q(line) for line in (
            DHCPCD_BEGIN,
            "interface " + interface,
            "static ip_address=" + address,
            "static routers=" + gateway,
            "static domain_name_servers=" + dns,
            DHCPCD_END,
        ))
        network_setup = "\n".join([
            "emerge --noreplace --verbose net-misc/dhcpcd",
            f"touch {DHCPCD_CONF}",
            # Drop the block a previous attempt wrote, so a rerun leaves exactly one.
            f"sed -i {q('/^' + DHCPCD_BEGIN + '$/,/^' + DHCPCD_END + '$/d')} {DHCPCD_CONF}",
            f"printf '%s\\n' {block} >> {DHCPCD_CONF}",
            "rc-update add dhcpcd default",
        ])
    inside = "\n".join([
        f"printf '%s\\n' {hostname} > /etc/hostname",
        f"ln -snf {q('/usr/share/zoneinfo/' + str(timezone_value))} /etc/localtime",
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
    return "\n".join(["set -eu", chroot(inside), f"touch {q(MOUNT_ROOT + '/.gentoo-agent-configuration-complete')}"])


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
    marker = f"touch {q(MOUNT_ROOT + '/.gentoo-agent-authentication-complete')}"
    if not (install.get("set_root_password", True) or create_user):
        return "\n".join(["set -eu", chroot("\n".join(inside)), marker])
    # The runner sends chpasswd records on stdin. Capture them before anything
    # else runs, give every other command /dev/null as stdin, and pipe the
    # records only to chpasswd. Never echo the variable or enable tracing.
    return "\n".join([
        "set -eu",
        "records=$(cat)",
        "exec </dev/null",
        chroot("\n".join(inside)),
        f"printf '%s\\n' \"$records\" | chroot {q(MOUNT_ROOT)} chpasswd",
        marker,
    ])


def kernel_command(install: dict) -> str:
    inside = "\n".join([
        "mkdir -p /etc/portage/package.use",
        "printf '%s\\n' 'sys-kernel/installkernel dracut' > /etc/portage/package.use/gentoo-agent-installkernel",
        "emerge --noreplace --verbose sys-kernel/gentoo-kernel-bin",
        "test -n \"$(find /boot -maxdepth 1 -type f -name 'vmlinuz*' -print -quit)\"",
    ])
    return "\n".join(["set -eu", chroot(inside)])


def bootloader_command(target: dict, install: dict) -> str:
    disk, _ = validated_settings(target, install)
    if install["boot_mode"] == "bios":
        inside = "\n".join([
            "emerge --noreplace --verbose sys-boot/grub",
            f"grub-install --target=i386-pc {q(disk)}",
            "grub-mkconfig -o /boot/grub/grub.cfg",
        ])
    else:
        spec = architecture_spec(install.get("architecture"))
        inside = "\n".join([
            "emerge --noreplace --verbose sys-boot/grub",
            f"grub-install --target={q(spec['grub_efi_target'])} --efi-directory=/boot --bootloader-id=Gentoo",
            "grub-mkconfig -o /boot/grub/grub.cfg",
        ])
    return "\n".join(["set -eu", chroot(inside)])


def validation_command(target: dict, install: dict) -> str:
    disk, layout = validated_settings(target, install)
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


def storage_check_command(target: dict, install: dict) -> str:
    disk, layout = validated_settings(target, install)
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


def filesystem_check_command(target: dict, install: dict) -> str:
    _, layout = validated_settings(target, install)
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
    lines = [
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
    ]
    if install.get("network_method", "dhcp") == "static":
        static = install.get("network_static") or {}
        conf = q(MOUNT_ROOT + DHCPCD_CONF)
        lines += [
            f"grep -Fqx {q('interface ' + str(static.get('interface', '')))} {conf}",
            f"grep -Fqx {q('static ip_address=' + str(static.get('address', '')))} {conf}",
        ]
    lines.append("printf 'configuration-ok\\n'")
    return "\n".join(lines)


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
        binary = architecture_spec(install.get("architecture"))["grub_efi_binary"]
        lines.append(f"test -e {q(MOUNT_ROOT + '/boot/EFI/Gentoo/' + binary)}")
    lines.append("printf 'bootloader-ok\\n'")
    return "\n".join(lines)


def check_command_for(phase: str, target: dict, install: dict) -> str:
    if phase == "preflight":
        return "\n".join(preflight_commands(target, install))
    if phase == "storage":
        return storage_check_command(target, install)
    if phase == "filesystems":
        return filesystem_check_command(target, install)
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
        return validation_command(target, install)
    raise AgentError(CONFIG_INVALID, f"Unknown installation phase: {phase}", {"phase": phase})


def command_for(phase: str, target: dict, install: dict) -> str:
    if phase == "preflight":
        return "\n".join(preflight_commands(target, install))
    if phase == "storage":
        return storage_command(target, install)
    if phase == "filesystems":
        return filesystem_command(target, install)
    if phase == "base-system":
        return base_system_command(install)
    if phase == "configuration":
        return configuration_command(target, install)
    if phase == "authentication":
        return authentication_command(target, install)
    if phase == "kernel":
        return kernel_command(install)
    if phase == "bootloader":
        return bootloader_command(target, install)
    if phase == "validation":
        return validation_command(target, install)
    raise AgentError(CONFIG_INVALID, f"Unknown installation phase: {phase}", {"phase": phase})
