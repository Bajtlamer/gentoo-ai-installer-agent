"""Phase programs of the desktop-plasma profile. They run as root on an installed, running system.

Every apply program starts with set -eu and is idempotent: files are rewritten, blocks are replaced, emerge
uses --noreplace, rc-update is guarded by a test. Nothing here touches a disk, mounts or unmounts anything,
starts a service, or reboots (a test scans every program for such commands). Values are quoted with
shlex.quote and come from validated settings. Checks are read-only.
"""

from __future__ import annotations

import re

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError
from gentoo_agent.core.provision import validated_desktop
from gentoo_agent.core.provision_phases import _block_apply, _block_check, _program, _verify_apply, _why, q


PHASES = ("preflight", "plasma-profile", "plasma-config", "plasma-update", "plasma-install", "plasma-services",
          "plasma-verify", "desktop-online")
# phase -> (destructive: changes the system, run: executed by the worker; False means verify_install only)
PHASE_FLAGS = {
    "preflight": (False, True),
    "plasma-profile": (True, True),
    "plasma-config": (True, True),
    "plasma-update": (True, True),
    "plasma-install": (True, True),
    "plasma-services": (True, True),
    "plasma-verify": (False, True),
    "desktop-online": (False, False),
}

MAKE_CONF = "/etc/portage/make.conf"
DESKTOP_BEGIN = "# gentoo-agent desktop begin"
DESKTOP_END = "# gentoo-agent desktop end"
USE_FILE = "/etc/portage/package.use/gentoo-agent-desktop"
DM_CONF = "/etc/conf.d/display-manager"
DM_BEGIN = "# gentoo-agent display-manager begin"
DM_END = "# gentoo-agent display-manager end"
PACKAGES = ("kde-plasma/plasma-meta", "kde-apps/konsole", "kde-apps/dolphin", "x11-misc/sddm",
            "gui-libs/display-manager-init", "sys-auth/elogind")
EMERGE = "emerge --getbinpkg --nospinner --verbose"
NVIDIA_USE_LINE = "x11-drivers/nvidia-drivers wayland"
MIN_FREE_KIB = 20 * 1024 * 1024
WAYLAND_SESSION = "/usr/share/wayland-sessions/plasma.desktop"
RESOLVE_FAILED_DESKTOP = ("plasma-config: Portage cannot resolve the Plasma packages with these settings; run "
                          "emerge --pretend by hand to see which USE change is missing")

_VIDEO_CARD = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_PLASMA_PROFILE = re.compile(r"default/linux/amd64/[0-9]+\.[0-9]+/desktop/plasma")


def _settings(provision: object) -> dict:
    """The validated settings the programs are built from; the stored block carries more than the operator set."""
    base = validated_desktop(provision)
    cards = provision.get("video_cards")
    if (not isinstance(cards, list) or not 1 <= len(cards) <= 8 or len(set(map(str, cards))) != len(cards)
            or not all(isinstance(card, str) and _VIDEO_CARD.fullmatch(card) for card in cards)):
        raise AgentError(CONFIG_INVALID, "video_cards must be 1 to 8 distinct VIDEO_CARDS names.",
                         {"field": "video_cards"})
    target = provision.get("plasma_profile")
    if not isinstance(target, str) or not _PLASMA_PROFILE.fullmatch(target):
        raise AgentError(CONFIG_INVALID, "plasma_profile must be default/linux/amd64/<version>/desktop/plasma.",
                         {"field": "plasma_profile"})
    nvidia = provision.get("nvidia")
    if not isinstance(nvidia, bool):
        raise AgentError(CONFIG_INVALID, "nvidia must be true or false.", {"field": "nvidia"})
    return {**base, "video_cards": list(cards), "plasma_profile": target, "nvidia": nvidia}


def _package_pattern(package: str) -> str:
    category, name = package.split("/")
    return q(f"/var/db/pkg/{category}/{name}-") + "[0-9]*"


def _installed_checks() -> list[str]:
    return [f"ls -d {_package_pattern(package)} >/dev/null" for package in PACKAGES]


def _preflight(settings: dict) -> str:
    lines = [
        _why('test "$(id -u)" -eq 0', "must run as root"),
        _why("ip route | grep -q default", "no default route"),
        _why("command -v emerge >/dev/null", "emerge not found"),
        _why("command -v eselect >/dev/null", "eselect not found"),
        _why("command -v rc-update >/dev/null", "rc-update not found"),
        _why('test "$(findmnt -rn -o FSTYPE /boot)" = vfat', "/boot is not a mounted vfat filesystem"),
        _why(f"test \"$(df -Pk / | awk 'NR==2 {{print $4}}')\" -ge {MIN_FREE_KIB}", "less than 20 GiB free on /"),
    ]
    if settings["nvidia"]:
        lines.append(_why('(PATH="$PATH:/opt/bin"; nvidia-smi -L | grep -q NVIDIA)',
                          "the NVIDIA driver is not active; apply the nvidia-driver plan and restart first"))
    return _program(*lines, "printf 'preflight-ok\\n'")


def _profile_apply(settings: dict) -> str:
    target = settings["plasma_profile"]
    missing = f"plasma-profile: the profile {target} is no longer offered by eselect profile list"
    return _program(
        f"eselect profile list | grep -Fq -- {q(target + ' ')} || "
        f"{{ printf '%s\\n' {q(missing)} >&2; exit 1; }}",
        f"eselect profile set {q(target)}",
    )


def _profile_check(settings: dict) -> str:
    return _program(
        f"test \"$(eselect profile show | tail -n 1 | tr -d ' ')\" = {q(settings['plasma_profile'])}",
        "printf 'plasma-profile-ok\\n'",
    )


def _make_conf_lines(settings: dict) -> list[str]:
    return ['VIDEO_CARDS="' + " ".join(settings["video_cards"]) + '"']


def _use_lines(settings: dict) -> list[str]:
    return [*([NVIDIA_USE_LINE] if settings["nvidia"] else []), *settings["package_use"]]


def _config_apply(settings: dict) -> str:
    lines = _use_lines(settings)
    write_use = (("printf '%s\\n' " + " ".join(q(line) for line in lines) + f" > {q(USE_FILE)}") if lines
                 else f": > {q(USE_FILE)}")
    return _program(
        "mkdir -p /etc/portage/package.use",
        *_block_apply(MAKE_CONF, DESKTOP_BEGIN, DESKTOP_END, _make_conf_lines(settings)),
        write_use,
    )


def _config_check(settings: dict) -> str:
    return _program(
        *_block_check(MAKE_CONF, DESKTOP_BEGIN, DESKTOP_END, _make_conf_lines(settings)),
        f"test -f {q(USE_FILE)}",
        *(f"grep -Fqx {q(line)} {q(USE_FILE)}" for line in _use_lines(settings)),
        f"test \"$(portageq envvar VIDEO_CARDS)\" = {q(' '.join(settings['video_cards']))}",
        f"emerge --pretend --getbinpkg --nospinner --autounmask-write=n {' '.join(PACKAGES)} >/dev/null || "
        f"{{ printf '%s\\n' {q(RESOLVE_FAILED_DESKTOP)} >&2; exit 1; }}",
        "printf 'plasma-config-ok\\n'",
    )


def _update_apply() -> str:
    return _program(f"{EMERGE} --update --deep --newuse @world")


def _update_check() -> str:
    # An interrupted run resumes: the check lists what is still to be merged, so only that is redone.
    # The pending list is captured first; a pipe straight into grep would hide a failing emerge.
    failed = "plasma-update: emerge --pretend failed; run it by hand to see why"
    pending = "plasma-update: packages of @world are still to be updated"
    return _program(
        "out=\"$(emerge --pretend --getbinpkg --update --deep --newuse --nospinner --quiet @world 2>/dev/null)\" || "
        f"{{ printf '%s\\n' {q(failed)} >&2; exit 1; }}",
        "if printf '%s\\n' \"$out\" | grep -Eq '^\\[(ebuild|binary)'; then "
        f"printf '%s\\n' {q(pending)} >&2; exit 1; fi",
        "printf 'plasma-update-ok\\n'",
    )


def _install_apply() -> str:
    return _program(f"{EMERGE} --noreplace {' '.join(PACKAGES)}")


def _install_check() -> str:
    return _program(*_installed_checks(), "printf 'plasma-install-ok\\n'")


_DM_LINES = ["CHECKVT=7", 'DISPLAYMANAGER="sddm"']
_RUNLEVELS = (("elogind", "boot"), ("display-manager", "default"))


def _runlevel_present(service: str, level: str) -> str:
    return f"rc-update show {level} | grep -Eq '(^| ){service} '"


def _services_apply() -> str:
    # rc-update add is not guarded with || true: the test first, so a real failure still stops the phase.
    # Nothing is started; the restart of the server starts the services.
    return _program(
        *_block_apply(DM_CONF, DM_BEGIN, DM_END, _DM_LINES),
        *(f"{_runlevel_present(service, level)} || rc-update add {service} {level}"
          for service, level in _RUNLEVELS),
    )


def _services_checks() -> list[str]:
    return [*_block_check(DM_CONF, DM_BEGIN, DM_END, _DM_LINES),
            *(_runlevel_present(service, level) for service, level in _RUNLEVELS)]


def _services_check() -> str:
    return _program(*_services_checks(), "printf 'plasma-services-ok\\n'")


NO_NVIDIA_MODULE = "plasma-verify: no NVIDIA kernel module for the newest installed kernel in /lib/modules"


def _nvidia_module_checks() -> list[str]:
    # The world update may have installed a newer distribution kernel; its NVIDIA modules must exist.
    return [
        'newest="$(ls -1v /lib/modules | tail -n 1)"',
        "find \"/lib/modules/$newest\" -name 'nvidia*.ko*' | grep -q . || "
        f"{{ printf '%s\\n' {q(NO_NVIDIA_MODULE)} >&2; exit 1; }}",
    ]


def _verify_check(settings: dict) -> str:
    return _program(*_installed_checks(), f"test -f {q(WAYLAND_SESSION)}", *_services_checks(),
                    *(_nvidia_module_checks() if settings["nvidia"] else []),
                    "printf 'plasma-verify-ok\\n'")


def _online_check() -> str:
    # The exact output of rc-service status is confirmed on the first real run.
    return _program(
        "rc-service display-manager status | grep -q started",
        "pgrep -x sddm >/dev/null",
        f"test -f {q(WAYLAND_SESSION)}",
        "printf 'desktop-online-ok\\n'",
    )


def command_for(phase: str, target: dict, provision: dict) -> str:
    """The apply program of a phase (target is part of the generator interface)."""
    if phase not in PHASE_FLAGS:
        raise AgentError(CONFIG_INVALID, f"Unknown provision phase: {phase}", {"phase": phase})
    settings = _settings(provision)
    programs = {
        "preflight": lambda: _preflight(settings),
        "plasma-profile": lambda: _profile_apply(settings),
        "plasma-config": lambda: _config_apply(settings),
        "plasma-update": lambda: _update_apply(),
        "plasma-install": lambda: _install_apply(),
        "plasma-services": lambda: _services_apply(),
        "plasma-verify": lambda: _verify_apply("plasma-verify has nothing to apply; read the check output"),
        "desktop-online": lambda: _verify_apply(
            "desktop-online is only checked once the server has been restarted (verify_install)"),
    }
    return programs[phase]()


def check_command_for(phase: str, target: dict, provision: dict) -> str:
    """The read-only check of a phase. Exit 0 means the phase is complete."""
    if phase not in PHASE_FLAGS:
        raise AgentError(CONFIG_INVALID, f"Unknown provision phase: {phase}", {"phase": phase})
    settings = _settings(provision)
    checks = {
        "preflight": lambda: _preflight(settings),
        "plasma-profile": lambda: _profile_check(settings),
        "plasma-config": lambda: _config_check(settings),
        "plasma-update": lambda: _update_check(),
        "plasma-install": lambda: _install_check(),
        "plasma-services": lambda: _services_check(),
        "plasma-verify": lambda: _verify_check(settings),
        "desktop-online": lambda: _online_check(),
    }
    return checks[phase]()
