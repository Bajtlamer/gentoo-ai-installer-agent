"""Phase programs for provision plans. They run as root on an installed, running system.

Every apply program starts with set -eu and is idempotent: files are rewritten, blocks are replaced, emerge
uses --noreplace. Nothing here touches a disk, mounts or unmounts anything, or reboots (a test scans every
program for such commands). Values are quoted with shlex.quote and come from validated settings.
"""

from __future__ import annotations

import shlex

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.provision import validated_provision, validated_tuning


q = shlex.quote

PHASES = ("preflight", "portage-tuning", "portage-sync", "nvidia-config", "nvidia-drivers", "nvidia-verify",
          "gpu-online")
# phase -> (destructive: changes the system, run: executed by the worker; False means verify_install only)
PHASE_FLAGS = {
    "preflight": (False, True),
    "portage-tuning": (True, True),
    "portage-sync": (True, True),
    "nvidia-config": (True, True),
    "nvidia-drivers": (True, True),
    "nvidia-verify": (False, True),
    "gpu-online": (False, False),
}

MAKE_CONF = "/etc/portage/make.conf"
GRUB_DEFAULTS = "/etc/default/grub"
LICENSE_FILE = "/etc/portage/package.license/gentoo-agent-nvidia"
USE_FILE = "/etc/portage/package.use/gentoo-agent-nvidia"
NVIDIA_PACKAGE = "x11-drivers/nvidia-drivers"
LICENSE_PACKAGE = NVIDIA_PACKAGE
NVIDIA_USE = "kernel-open dist-kernel"
TUNING_BEGIN = "# gentoo-agent portage-tuning begin"
TUNING_END = "# gentoo-agent portage-tuning end"
GRUB_BEGIN = "# gentoo-agent nvidia begin"
GRUB_END = "# gentoo-agent nvidia end"
GRUB_LINE = 'GRUB_CMDLINE_LINUX="${GRUB_CMDLINE_LINUX:+$GRUB_CMDLINE_LINUX }nvidia-drm.modeset=1"'
MIN_FREE_KIB = 8 * 1024 * 1024


def _program(*lines: str) -> str:
    return "\n".join(["set -eu", *lines])


def _block_apply(path: str, begin: str, end: str, lines: list[str]) -> list[str]:
    """Replace the block between the markers (or add it): the phase stays idempotent.

    Portable (no sed -i). Refuses before any write when the begin and end markers
    are unbalanced, and never deletes an unterminated block.
    """
    refusal = f"refusing to edit {path}: the plan block has a begin marker without an end marker (or the reverse)"
    awk = ("$0 == b {inblock = 1; held = $0; next} "
           "inblock {held = held ORS $0; if ($0 == e) {inblock = 0; held = \"\"}; next} "
           "{print} END {if (inblock) print held}")
    return [
        f"touch {q(path)}",
        f'if test "$(grep -Fxc {q(begin)} {q(path)})" != "$(grep -Fxc {q(end)} {q(path)})"; '
        f"then printf '%s\\n' {q(refusal)} >&2; exit 1; fi",
        f'test -z "$(tail -c1 {q(path)})" || printf \'\\n\' >> {q(path)}',
        f'tmp="$(mktemp {q(path + ".XXXXXX")})"',
        f"awk -v b={q(begin)} -v e={q(end)} {q(awk)} {q(path)} > \"$tmp\"",
        "printf '%s\\n' " + " ".join(q(line) for line in [begin, *lines, end]) + ' >> "$tmp"',
        f'cat "$tmp" > {q(path)}',
        'rm -f "$tmp"',
    ]


def _block_check(path: str, begin: str, end: str, lines: list[str]) -> list[str]:
    return [f"grep -Fqx {q(line)} {q(path)}" for line in [begin, *lines, end]]


def _preflight() -> str:
    return _program(
        'test "$(id -u)" -eq 0',
        "ip route | grep -q default",
        "command -v emerge >/dev/null",
        'test "$(findmnt -rn -o FSTYPE /boot)" = vfat',
        f"test \"$(df -Pk / | awk 'NR==2 {{print $4}}')\" -ge {MIN_FREE_KIB}",
        'test -d "/usr/src/linux-$(uname -r)"',
        "printf 'preflight-ok\\n'",
    )


def _tuning_lines(provision: dict) -> list[str]:
    tuning = validated_tuning(provision.get("tuning"))
    return [
        f'MAKEOPTS="-j{tuning["jobs"]}"',
        f'EMERGE_DEFAULT_OPTS="--jobs={tuning["emerge_jobs"]} --load-average={tuning["load_average"]}"',
    ]


def _tuning_apply(provision: dict) -> str:
    return _program(*_block_apply(MAKE_CONF, TUNING_BEGIN, TUNING_END, _tuning_lines(provision)))


def _tuning_check(provision: dict) -> str:
    tuning = validated_tuning(provision.get("tuning"))
    return _program(
        *_block_check(MAKE_CONF, TUNING_BEGIN, TUNING_END, _tuning_lines(provision)),
        f'test "$(portageq envvar MAKEOPTS)" = -j{tuning["jobs"]}',
        f'test "$(portageq envvar EMERGE_DEFAULT_OPTS)" = '
        f'{q("--jobs=" + str(tuning["emerge_jobs"]) + " --load-average=" + str(tuning["load_average"]))}',
        "printf 'portage-tuning-ok\\n'",
    )


def _sync_apply() -> str:
    return _program("emerge-webrsync")


def _sync_check() -> str:
    return _program(
        "test -n \"$(find /var/db/repos/gentoo/metadata -maxdepth 1 -name timestamp.chk -mtime -1)\"",
        "printf 'portage-sync-ok\\n'",
    )


def _license_line(provision: dict) -> str:
    return f"{LICENSE_PACKAGE} " + " ".join(validated_provision(provision)["accept_licenses"])


def _config_apply(provision: dict) -> str:
    return _program(
        "mkdir -p /etc/portage/package.license /etc/portage/package.use",
        f"printf '%s\\n' {q(_license_line(provision))} > {q(LICENSE_FILE)}",
        f"printf '%s\\n' {q(NVIDIA_PACKAGE + ' ' + NVIDIA_USE)} > {q(USE_FILE)}",
        *_block_apply(GRUB_DEFAULTS, GRUB_BEGIN, GRUB_END, [GRUB_LINE]),
        'eselect kernel set "linux-$(uname -r)"',
    )


def _config_check(provision: dict) -> str:
    return _program(
        f"test -s {q(LICENSE_FILE)}",
        f"grep -Fqx {q(_license_line(provision))} {q(LICENSE_FILE)}",
        f"grep -Fqx {q(NVIDIA_PACKAGE + ' ' + NVIDIA_USE)} {q(USE_FILE)}",
        *_block_check(GRUB_DEFAULTS, GRUB_BEGIN, GRUB_END, [GRUB_LINE]),
        'test "$(readlink -f /usr/src/linux)" = "/usr/src/linux-$(uname -r)"',
        "printf 'nvidia-config-ok\\n'",
    )


def _drivers_apply() -> str:
    return _program(f"emerge --noreplace --verbose {NVIDIA_PACKAGE}", "grub-mkconfig -o /boot/grub/grub.cfg")


def _drivers_check() -> str:
    return _program(
        "ls -d /var/db/pkg/x11-drivers/nvidia-drivers-* >/dev/null",
        "find \"/lib/modules/$(uname -r)\" -name 'nvidia*.ko*' | grep -q .",
        "grep -q 'nvidia-drm.modeset=1' /boot/grub/grub.cfg",
        "printf 'nvidia-drivers-ok\\n'",
    )


def _verify_apply(message: str) -> str:
    return _program(f"printf '%s\\n' {q(message)} >&2", "exit 1")


def _verify_check() -> str:
    return _program(
        'modinfo -k "$(uname -r)" nvidia >/dev/null',
        "modprobe -c | grep -Eq '^blacklist nouveau$'",
        "printf 'nvidia-verify-ok\\n'",
    )


def _online_check() -> str:
    return _program(
        'PATH="$PATH:/opt/bin"',
        "lsmod | grep -q '^nvidia '",
        "nvidia-smi -L | grep -q NVIDIA",
        'test "$(cat /sys/module/nvidia_drm/parameters/modeset)" = Y',
        "printf 'gpu-online-ok\\n'",
    )


def command_for(phase: str, target: dict, provision: dict) -> str:
    """The apply program of a phase (target is part of the generator interface, like phases.command_for)."""
    programs = {
        "preflight": lambda: _preflight(),
        "portage-tuning": lambda: _tuning_apply(provision),
        "portage-sync": lambda: _sync_apply(),
        "nvidia-config": lambda: _config_apply(provision),
        "nvidia-drivers": lambda: _drivers_apply(),
        "nvidia-verify": lambda: _verify_apply("nvidia-verify has nothing to apply; read the check output"),
        "gpu-online": lambda: _verify_apply("gpu-online is only checked once the server has been restarted (verify_install)"),
    }
    if phase not in programs:
        raise AgentError(CONFIG_INVALID, f"Unknown provision phase: {phase}", {"phase": phase})
    return programs[phase]()


def check_command_for(phase: str, target: dict, provision: dict) -> str:
    """The read-only check of a phase. Exit 0 means the phase is complete."""
    checks = {
        "preflight": lambda: _preflight(),
        "portage-tuning": lambda: _tuning_check(provision),
        "portage-sync": lambda: _sync_check(),
        "nvidia-config": lambda: _config_check(provision),
        "nvidia-drivers": lambda: _drivers_check(),
        "nvidia-verify": lambda: _verify_check(),
        "gpu-online": lambda: _online_check(),
    }
    if phase not in checks:
        raise AgentError(CONFIG_INVALID, f"Unknown provision phase: {phase}", {"phase": phase})
    return checks[phase]()
