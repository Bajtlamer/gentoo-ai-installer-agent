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
NVIDIA_USE = "dist-kernel -tools"
TUNING_BEGIN = "# gentoo-agent portage-tuning begin"
TUNING_END = "# gentoo-agent portage-tuning end"
GRUB_BEGIN = "# gentoo-agent nvidia begin"
GRUB_END = "# gentoo-agent nvidia end"
GRUB_LINE = 'GRUB_CMDLINE_LINUX="${GRUB_CMDLINE_LINUX:+$GRUB_CMDLINE_LINUX }nvidia-drm.modeset=1"'
RESOLVE_FAILED = ("nvidia-config: Portage cannot resolve x11-drivers/nvidia-drivers with these licenses and USE "
                  "settings; run emerge --pretend by hand to see what is missing")
MIN_FREE_KIB = 8 * 1024 * 1024


def _program(*lines: str) -> str:
    return "\n".join(["set -eu", *lines])


def _block_apply(path: str, begin: str, end: str, lines: list[str]) -> list[str]:
    """Replace the block between the markers (or add it): the phase stays idempotent.

    Portable (no sed -i). Refuses before any write when the begin and end markers
    are unbalanced, repeated or in the wrong order, and never deletes an unterminated block.
    """
    mismatch = f"refusing to edit {path}: the begin and end markers do not match, or there is more than one plan block"
    order = f"refusing to edit {path}: the end marker comes before the begin marker"
    awk = ("$0 == b {inblock = 1; held = $0; next} "
           "inblock {held = held ORS $0; if ($0 == e) {inblock = 0; held = \"\"}; next} "
           "{print} END {if (inblock) print held}")
    return [
        f"touch {q(path)}",
        f'b="$(grep -Fxc {q(begin)} {q(path)} || true)"; e="$(grep -Fxc {q(end)} {q(path)} || true)"',
        f'if test "$b" != "$e" || test "$b" -gt 1; then printf \'%s\\n\' {q(mismatch)} >&2; exit 1; fi',
        f'if test "$b" -eq 1 && test "$(grep -Fxn {q(begin)} {q(path)} | cut -d: -f1)" -gt '
        f'"$(grep -Fxn {q(end)} {q(path)} | cut -d: -f1)"; then printf \'%s\\n\' {q(order)} >&2; exit 1; fi',
        f'test -z "$(tail -c1 {q(path)})" || printf \'\\n\' >> {q(path)}',
        f'tmp="$(mktemp {q(path + ".XXXXXX")})"',
        'trap ' + q('rm -f "$tmp"') + ' EXIT',
        f"awk -v b={q(begin)} -v e={q(end)} {q(awk)} {q(path)} > \"$tmp\"",
        "printf '%s\\n' " + " ".join(q(line) for line in [begin, *lines, end]) + ' >> "$tmp"',
        f'cat "$tmp" > {q(path)}',
        'rm -f "$tmp"',
    ]


def _block_check(path: str, begin: str, end: str, lines: list[str]) -> list[str]:
    return [f"grep -Fqx {q(line)} {q(path)}" for line in [begin, *lines, end]]


def _why(condition: str, reason: str) -> str:
    return f"{condition} || {{ printf '%s\\n' {q('preflight: ' + reason)} >&2; exit 1; }}"


def _preflight() -> str:
    return _program(
        _why('test "$(id -u)" -eq 0', "must run as root"),
        _why("ip route | grep -q default", "no default route"),
        _why("command -v emerge >/dev/null", "emerge not found"),
        _why('test "$(findmnt -rn -o FSTYPE /boot)" = vfat', "/boot is not a mounted vfat filesystem"),
        _why(f"test \"$(df -Pk / | awk 'NR==2 {{print $4}}')\" -ge {MIN_FREE_KIB}", "less than 8 GiB free on /"),
        _why('test -d "/usr/src/linux-$(uname -r)"', "the kernel sources /usr/src/linux-<release> are missing"),
        'case "$(uname -r)" in *-gentoo-dist*) ;; *) printf \'%s\\n\' '
        "'preflight: the running kernel is not a Gentoo distribution kernel' >&2; exit 1;; esac",
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
        "test -n \"$(find /var/db/repos/gentoo/metadata -maxdepth 1 -name timestamp.chk -mtime -2)\"",
        "printf 'portage-sync-ok\\n'",
    )


def _license_line(provision: dict) -> str:
    return f"{NVIDIA_PACKAGE} " + " ".join(validated_provision(provision)["accept_licenses"])


def _use_lines(provision: dict) -> list[str]:
    return [NVIDIA_PACKAGE + " " + NVIDIA_USE, *validated_provision(provision)["package_use"]]


def _config_apply(provision: dict) -> str:
    return _program(
        "mkdir -p /etc/portage/package.license /etc/portage/package.use",
        f"printf '%s\\n' {q(_license_line(provision))} > {q(LICENSE_FILE)}",
        "printf '%s\\n' " + " ".join(q(line) for line in _use_lines(provision)) + f" > {q(USE_FILE)}",
        *_block_apply(GRUB_DEFAULTS, GRUB_BEGIN, GRUB_END, [GRUB_LINE]),
        'eselect kernel set "linux-$(uname -r)"',
    )


def _config_check(provision: dict) -> str:
    return _program(
        f"test -s {q(LICENSE_FILE)}",
        f"grep -Fqx {q(_license_line(provision))} {q(LICENSE_FILE)}",
        *(f"grep -Fqx {q(line)} {q(USE_FILE)}" for line in _use_lines(provision)),
        *_block_check(GRUB_DEFAULTS, GRUB_BEGIN, GRUB_END, [GRUB_LINE]),
        'test "$(readlink -f /usr/src/linux)" = "/usr/src/linux-$(uname -r)"',
        f"emerge --pretend --nospinner --autounmask-write=n {NVIDIA_PACKAGE} >/dev/null || "
        f"{{ printf '%s\\n' {q(RESOLVE_FAILED)} >&2; exit 1; }}",
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
    # The open kernel modules report a GPL license, the proprietary one reports NVIDIA (Blackwell needs the
    # open modules; to be confirmed on the real server).
    return _program(
        'PATH="$PATH:/opt/bin"',
        "lsmod | grep -q '^nvidia '",
        "modinfo -F license nvidia | grep -q GPL",
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
