# Desktop Plasma Purpose (plan 2b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `desktop-plasma` profile: KDE Plasma (Wayland session offered, OpenRC) on an installed system, as its own provision plan with its own approval.

**Architecture:** Builds on plan 2a (registry, hardware profile). The registry entry gains `build` (plan settings from facts) and `summary` (approval lines) hooks so the plan, the approval dialog, `verify_provision_programs`, and `verify_install` all dispatch through the profile. The programs live in `core/desktop_phases.py`.

**Tech Stack:** Python 3.12 standard library, unittest. Spec: `docs/superpowers/specs/2026-10-20-profiles-and-plasma-design.md`. Read it, `core/provision_phases.py` (the patterns: `_program`, `_block_apply`, `_block_check`, `_why`) and `core/profiles.py` first.

Binding rules (AGENTS.md, CLAUDE.md): `core/` never prints, prompts, or exits; values from the target are untrusted and strictly parsed; nothing is guessed; no Claude attribution in commits; never run `approve`; never touch `approvals/`, `plans/`, `config/`; no program may touch a disk, mount, unmount, or reboot.

Baseline: `.venv/bin/python -m unittest discover -s tests` is green (706 tests) before Task 1.

## Facts verified on the real server 10.0.1.17 (read-only SSH, 2026-10-20) and on the Gentoo wiki

- Current Portage profile `default/linux/amd64/23.0` (OpenRC). `eselect profile list` has `default/linux/amd64/23.0/desktop/plasma` exactly once ( and `.../desktop/plasma/systemd`, `.../split-usr/desktop/plasma`).
- `/etc/portage/binrepos.conf/gentoo.conf`: `sync-uri = https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64`, `verify-signature = true`.
- Tree has `kde-plasma/plasma-meta` (6.7.5), `kde-apps/konsole`, `kde-apps/dolphin`, `x11-misc/sddm`, `gui-libs/display-manager-init`, `sys-auth/elogind`. No `/etc/conf.d/display-manager`, no desktop services installed yet.
- `emerge --pretend kde-plasma/plasma-meta` fails under the OLD profile (qtbase `wayland? ( opengl )`), so the resolution check runs only after the profile switch.
- SDDM wiki (https://wiki.gentoo.org/wiki/SDDM, retrieved 2026-10-20): OpenRC needs `gui-libs/display-manager-init`, `/etc/conf.d/display-manager` with `CHECKVT=7` and `DISPLAYMANAGER="sddm"`, `rc-update add elogind boot`, `rc-update add display-manager default`. A Wayland SDDM GREETER on OpenRC needs seatd/weston wrappers that must be reapplied after SDDM updates; this plan does not do that. The SDDM greeter stays on X11 (the default), and the Plasma Wayland session is offered in the session list.
- Elogind wiki: `rc-update add elogind boot`; with dbus built with the `elogind` flag, starting elogind brings up the system dbus, so no separate dbus runlevel entry.

## Design decisions made in this plan

- Greeter X11, session Wayland (above). The approval dialog says so.
- The plan edits: a `make.conf` block (`VIDEO_CARDS`), `/etc/portage/package.use/gentoo-agent-desktop`, the profile symlink, a `/etc/conf.d/display-manager` block, and the runlevels of `elogind` (boot) and `display-manager` (default). It never starts services (the reboot does).
- Applicability needs: architecture `amd64`; the current profile is exactly `default/linux/amd64/<version>` (plain OpenRC; any variant such as hardened, systemd, split-usr, llvm refuses with a reason) or already `<that>/desktop/plasma`; the target name `<base>/desktop/plasma` occurs exactly once in `portage_profiles`; every GPU vendor is known (`video_cards` is not `None`); at least one display GPU or an explicit virtual machine is not required (a headless server with no GPU refuses: "no display device").
- With an NVIDIA GPU the plan writes `x11-drivers/nvidia-drivers wayland` and the preflight requires the driver to be live (`nvidia-smi -L`).
- Settings: `profile`, `package_use` (0 to 8 operator lines, same rules as part 1, none for the nvidia-drivers package because the code owns its `wayland` line). No licenses (the NVIDIA license stays in the nvidia plan's file); `accept_licenses` is refused for this profile.

---

## File Structure

- Modify `gentoo_agent/core/hardware_profile.py`: `portage_profile`, `portage_profiles`, `binrepos` copied into the hardware profile.
- Modify `gentoo_agent/core/profiles.py`: `Profile` gains `build` and `summary`; nvidia entry moved to them; new `desktop-plasma` entry.
- Create `gentoo_agent/core/desktop_phases.py`: programs.
- Modify `gentoo_agent/core/provision.py`: `validated_desktop()`.
- Modify `gentoo_agent/core/provision_plan.py`, `gentoo_agent/core/approval.py`, `gentoo_agent/mcp/server.py`, `gentoo_agent/cli.py`: dispatch.
- Tests: `tests/test_desktop_profile.py`, `tests/test_desktop_phases.py` (new); fixtures in `tests/provision_helpers.py`; extend the program safety scan.
- Docs: `docs/guides/provision.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `CLAUDE.md`.

---

### Task 1: Registry hooks `build` and `summary` (no behavior change)

**Files:** `core/profiles.py`, `core/provision_plan.py`, `core/approval.py`, `mcp/server.py` (`verify_install`), tests in `tests/test_profiles.py`.

- `Profile` gets `build: Callable[[dict, dict, dict], dict]` (arguments: validated settings, `facts`, `hardware`; returns the `provision` block) and `summary: Callable[[dict], list[str]]` (the profile-specific lines of the approval dialog for a stored plan).
- Move the NVIDIA block building out of `create_provision_plan` into `_nvidia_build` in `profiles.py` (exactly: the `nvidia[0]` check is already in `applicable`; return `{**settings, "gpu": nvidia[0], "cpu": ..., "memory_gib": ..., "tuning": ...}` as today). Move the lines of `_provision_summary` that are specific to the NVIDIA plan (GPU, Portage, USE changes, Edits, Regenerates, Packages, Licenses, Kernel sources link) into `_nvidia_summary(plan) -> list[str]` in `approval.py`'s neighbor (put it in `profiles.py` only if no import cycle results; otherwise keep it in `approval.py` and let the registry reference it lazily). `_provision_summary` keeps the common lines (plan, hash, kind, disk erase, changing phases, warnings, target, host, network, reboot) and appends `get_profile(...).summary(plan)`.
- `verify_install` in `mcp/server.py` uses `get_profile(plan["provision"]["profile"]).check_command_for(...)`.
- [ ] **Step 1:** Add a test that `_provision_summary` output for the existing fixture plan is byte-identical before and after (capture it first as a literal in the test, taken from the current code), and a test that `verify_install`'s expectation goes through the registry (patch `profiles.check_command_for` like the earlier test does).
- [ ] **Step 2:** Run it, implement, run the full suite (`.venv/bin/python`). The existing approval tests must pass unchanged.
- [ ] **Step 3:** Commit `Dispatch plan building and the approval summary through the profile`.

---

### Task 2: Plasma facts in the hardware profile and the applicability rule

**Files:** `core/hardware_profile.py`, `core/profiles.py`, `core/provision.py`, `tests/test_desktop_profile.py` (create), `tests/provision_helpers.py`.

```python
# hardware_profile(): also copy "portage_profile", "portage_profiles", "binrepos" (None when absent)

# profiles.py
PLASMA_SUFFIX = "/desktop/plasma"
PLAIN_PROFILE = re.compile(r"default/linux/amd64/[0-9]+\.[0-9]+")

def plasma_profile(hardware: dict) -> tuple[str | None, str | None]:
    """(target profile name, refusal reason). Exactly one is None."""
```

`plasma_profile` rules, in order, each a distinct reason text: architecture is not `amd64`; `portage_profile` is `None` (inventory has no profile facts: "collect the provision inventory again (refresh=true)"); the current profile ends with `/desktop/plasma` (then base is it minus the suffix) or fully matches `PLAIN_PROFILE`, else "only the plain OpenRC profile default/linux/amd64/<version> is supported, found <name>" (the name is shown through the strict regex so it is safe); the target `base + "/desktop/plasma"` must occur exactly once among the `name` fields of `portage_profiles` ("the Plasma profile <name> is not offered by this system" / "offered more than once"). The desktop `applicable` additionally refuses: `video_cards is None` ("a display device has an unknown vendor"), no GPU in `gpus` ("no display device was detected"), and `gpus` empty is the only no-GPU refusal (a virtual GPU with a known mapping is not possible; virtual vendors are unknown vendors and refuse).

`validated_desktop(settings)` in `provision.py`: requires `profile == "desktop-plasma"`; `accept_licenses` must be absent or empty, else `CONFIG_INVALID` field `accept_licenses`; `package_use` through the existing `_validated_package_use`; returns `{"profile", "package_use"}`.

`build` for desktop returns `{**settings, "gpus": hardware["gpus"], "video_cards": hardware["video_cards"], "nvidia": "nvidia" in hardware["gpu_vendors"], "portage_profile": current, "plasma_profile": target, "binrepos": hardware["binrepos"] or {"uris": []}, "cpu": facts["cpu"], "memory_gib": facts["memory_gib"]}` (no `tuning`; the nvidia plan owns it).

- [ ] **Step 1:** Tests with real fixtures: add `REAL_PORTAGE_PROFILE_STDOUT` and `REAL_PORTAGE_PROFILES_STDOUT` (the real output captured above, including the full list head) and the real `binrepos` text to `tests/provision_helpers.py` plus `desktop_inventory(**replace)` that extends `provision_inventory` with `arch`, `portage_profile`, `portage_profiles`, `binrepos` results. Cases: the real data gives target `default/linux/amd64/23.0/desktop/plasma`, applicable; already on the plasma profile is applicable; a `.../systemd` current profile refuses; `.../hardened` refuses; arm64 refuses; no `portage_profile` facts refuses with the collect-again reason; the plasma entry missing refuses; duplicated refuses; AMD-only inventory is applicable with `video_cards == ["amdgpu","radeonsi"]` and `nvidia` false; no GPU refuses; `accept_licenses` non-empty refuses; operator `package_use` with an nvidia-drivers line refuses.
- [ ] **Step 2:** Run (FAIL), implement, run the full suite.
- [ ] **Step 3:** Commit `Add the desktop-plasma applicability rule and settings`.

---

### Task 3: Phase programs of `desktop-plasma`

**Files:** `core/desktop_phases.py` (create), `core/profiles.py` (register), `tests/test_desktop_phases.py` (create).

Phases and flags (`(destructive, run)`): `preflight (False, True)`, `plasma-profile (True, True)`, `plasma-config (True, True)`, `plasma-update (True, True)`, `plasma-install (True, True)`, `plasma-services (True, True)`, `plasma-verify (False, True)`, `desktop-online (False, False)`.

Constants: `MAKE_CONF`, `DESKTOP_BEGIN = "# gentoo-agent desktop begin"`, `DESKTOP_END = "# gentoo-agent desktop end"`, `USE_FILE = "/etc/portage/package.use/gentoo-agent-desktop"`, `DM_CONF = "/etc/conf.d/display-manager"`, `DM_BEGIN = "# gentoo-agent display-manager begin"`, `DM_END = "# gentoo-agent display-manager end"`, `PACKAGES = ("kde-plasma/plasma-meta", "kde-apps/konsole", "kde-apps/dolphin", "x11-misc/sddm", "gui-libs/display-manager-init", "sys-auth/elogind")`, `EMERGE = "emerge --getbinpkg --nospinner --verbose"`. Reuse `_program`, `_block_apply`, `_block_check`, `_why`, `q` from `provision_phases` (import them; do not duplicate).

Programs (every apply starts with `set -eu` via `_program`; every value from settings is quoted):

1. `preflight` (apply and check are the same program): root, default route, `emerge`, `eselect`, `rc-update` present, at least 20 GiB free on `/` (the plan's `MIN_FREE_KIB` for desktop is `20 * 1024 * 1024`), and when `provision["nvidia"]` is true: `PATH="$PATH:/opt/bin"; nvidia-smi -L | grep -q NVIDIA` with the reason "the NVIDIA driver is not active; apply the nvidia-driver plan and restart first". Ends with `printf 'preflight-ok\n'`.
2. `plasma-profile`: apply `eselect profile set <plasma_profile>` (quoted) only after verifying the name is still in `eselect profile list` output (`eselect profile list | grep -Fq -- "<name> "` with a trailing space, else exit 1 with a reason); check `test "$(eselect profile show | tail -n 1 | tr -d ' ')" = <plasma_profile>`.
3. `plasma-config`: apply writes the `make.conf` block with the single line `VIDEO_CARDS="<space joined video_cards>"`, writes `USE_FILE` with the lines (`x11-drivers/nvidia-drivers wayland` only when `nvidia`, then the operator lines; when there are no lines the file is written empty with `: > file`), no other change. Check: block present (`_block_check`), the use lines present, `portageq envvar VIDEO_CARDS` equals the value, and the resolution check `emerge --pretend --getbinpkg --nospinner --autounmask-write=n <PACKAGES>` succeeding, otherwise print `RESOLVE_FAILED_DESKTOP = "plasma-config: Portage cannot resolve the Plasma packages with these settings; run emerge --pretend by hand to see which USE change is missing"` to stderr and exit 1.
4. `plasma-update`: apply `emerge --getbinpkg --update --deep --newuse --nospinner --verbose @world`; check `! emerge --pretend --getbinpkg --update --deep --newuse --nospinner --quiet @world 2>/dev/null | grep -Eq '^\[(ebuild|binary)'` then `printf 'plasma-update-ok\n'`. Note in a comment that an interrupted run resumes because the check lists what is left.
5. `plasma-install`: apply `emerge --getbinpkg --noreplace --nospinner --verbose <PACKAGES>`; check that `ls -d /var/db/pkg/<category>/<name>-[0-9]*` succeeds for every package.
6. `plasma-services`: apply the `DM_CONF` block with the lines `CHECKVT=7` and `DISPLAYMANAGER="sddm"`, then `rc-update add elogind boot`, `rc-update add display-manager default` (rc-update is idempotent: it prints that the service is already added; guard with `|| true` only for the "already" case is NOT allowed, so first test `rc-update show boot | grep -Fq elogind || rc-update add elogind boot`, same for default). Never `rc-service ... start`. Check: block present, `rc-update show boot | grep -Eq '(^| )elogind '`, `rc-update show default | grep -Eq '(^| )display-manager '`.
7. `plasma-verify` apply prints the standard "nothing to apply" message and exits 1 like `_verify_apply`; check: all packages installed (as in 5), `test -f /usr/share/wayland-sessions/plasma.desktop`, `test -f /usr/share/xsessions/plasmax11.desktop || true` is NOT added (only the Wayland session file is required), and the services check of 6.
8. `desktop-online` (`run: False`): check `rc-service display-manager status | grep -q started`, `pgrep -x sddm >/dev/null`, `test -f /usr/share/wayland-sessions/plasma.desktop`, `printf 'desktop-online-ok\n'`. Add a comment that the exact status output is confirmed on the first real run.

The registry entry: `id "desktop-plasma"`, `applicable` from Task 2, `validate validated_desktop`, `build` from Task 2, `summary` from Task 4, generators `command_for` and `check_command_for` with the same error on an unknown phase as `provision_phases`.

- [ ] **Step 1: Tests.** For every phase and both kinds: `sh -n` passes on the program (copy the `SyntaxTests` approach of `tests/test_provision_phases.py`); the program text contains no `mkfs`, `wipefs`, `sfdisk`, `parted`, `mount `, `umount`, `reboot`, `shutdown`, `poweroff`, `rc-service` start and no `*/*`; settings with a hostile `package_use` line (`x; rm -rf /`) are refused before a program is generated; the NVIDIA lines appear only when `nvidia` is true; AMD (`VIDEO_CARDS="amdgpu radeonsi"`) has no nvidia line and no `nvidia-smi` in preflight; two settings differing in one `package_use` line give different `plan_hash` values; `verify_provision_programs` rejects a plan whose stored `plasma-config` apply was edited; block behavior test of the `DM_CONF` block on a temp file using the same method as `BlockBehaviorTests`.
- [ ] **Step 2:** Run (FAIL), implement, run the full suite (`.venv/bin/python`), including the extended safety scan if the existing scan enumerates programs from `PHASES` (extend it to iterate the registry).
- [ ] **Step 3:** Commit `Add the desktop-plasma phases`.

---

### Task 4: Plan creation, approval summary, MCP and CLI wiring

**Files:** `core/provision_plan.py`, `core/profiles.py` (`_desktop_summary`), `core/approval.py`, `mcp/server.py`, `cli.py`, tests.

- `create_provision_plan` already dispatches (plan 2a and Task 1); confirm a desktop plan is created from `desktop_inventory()` and stores `kind: provision`, `identity`, `provision` (with `plasma_profile`, `video_cards`, `nvidia`), phases, and that `verify_provision_programs` accepts it.
- `_desktop_summary(plan)` lines (values via `_shown`): `Purpose: desktop-plasma (KDE Plasma, Wayland session offered, SDDM greeter on X11)`; `GPU: ` one line per device with vendor and PCI id and `VIDEO_CARDS: <value>`; `Portage profile: <old> -> <new> (system-wide change)`; `World update: emerge --getbinpkg --update --deep --newuse @world (system-wide, can take hours; binary packages where the USE flags match, otherwise compiled)`; `Binary host: <uris or none>`; `USE changes: ...`; `Edits: make.conf (plan block), package.use gentoo-agent-desktop, /etc/conf.d/display-manager (plan block), the profile symlink, runlevels elogind (boot) and display-manager (default)`; `Packages: <PACKAGES>`; `Requires: the NVIDIA driver already active` when `nvidia`; `Services are enabled, not started: the reboot starts them`. The common lines still say reboot required.
- MCP `create_provision_plan(accept_licenses: list[str] | None = None, package_use: list[str] | None = None, profile: str = "nvidia-driver")`: passes `accept_licenses or []` only when given (for `nvidia-driver` the empty list is refused by the existing validation, as today); update the docstring and the instructions text with the desktop workflow: `detect_provision(refresh=true)` lists the profiles; `create_provision_plan(profile="desktop-plasma", package_use=[...])`; the human approves; `start_install`; after the HUMAN reboots, `verify_install`. `package_use` lines come only from the human or from `emerge --pretend` output the human shows.
- CLI: `provision plan --profile desktop-plasma` (the `--accept-license` option becomes optional; the nvidia profile still refuses a missing one through validation).
- [ ] **Step 1:** Tests: desktop plan creation and summary content (assert the profile transition line, the VIDEO_CARDS line, the services line, no em dashes), MCP `create_provision_plan` with `profile="desktop-plasma"` and no licenses (needs the venv), the nvidia call without licenses still refused, CLI parser accepts `--profile desktop-plasma` without `--accept-license`.
- [ ] **Step 2:** Run (FAIL), implement, full suite with `.venv/bin/python`.
- [ ] **Step 3:** Commit `Create, summarize, and expose desktop-plasma plans`.

---

### Task 5: Runner and end-to-end behavior with a fake target

**Files:** `tests/test_execute_plan.py` (extend), runner only if a test shows it needs a change.

- [ ] **Step 1:** Add tests that run a desktop plan through `execute_plan` with a fake `remote_shell` (follow how the nvidia provision tests do it): phases run in order; a phase whose check passes is skipped; `desktop-online` is not run (`run: false`); a failing `plasma-config` check after apply stops the run with the output tail; identity drift (changed `root_uuid`) before a destructive phase stops it; a resume after a failure at `plasma-update` skips the finished phases.
- [ ] **Step 2:** Run, fix the runner only if a test exposes a real gap (report it), full suite.
- [ ] **Step 3:** Commit `Test running a desktop plan end to end with a fake target`.

---

### Task 6: Documentation

**Files:** `docs/guides/provision.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `CLAUDE.md`.

- [ ] **Step 1:** Document the `desktop-plasma` workflow for the operator (prerequisite: the nvidia-driver plan applied and the server restarted; detect, plan, approve, start, restart, verify), the hardware-to-profile mapping, the greeter-on-X11 decision and why (the Wayland greeter wrappers on OpenRC are fragile and wiped by SDDM updates), the facts and sources above with the retrieval date 2026-10-20, what is confirmed only by the first real run (the exact `rc-service display-manager status` output, that the Wayland session starts on driver 595), and the risks (hours of compilation, USE changes after the profile switch supplied by the human from `emerge --pretend`). ROADMAP: mark `desktop-plasma` implemented (not yet run on a real server) and list the rest (GNOME, other GPUs, other display managers, a Wayland greeter, vLLM).
- [ ] **Step 2:** Full suite. No em dashes or emoji in the added text.
- [ ] **Step 3:** Commit `Document the desktop-plasma profile`.

---

## Self-Review

Spec coverage: registry dispatch for every consumer (Task 1, 4); hardware to profile mapping and strict profile choice from the target's own list (Task 2); eight phases including the pretend check after the profile switch and `desktop-online` as `run: false` (Task 3); approval summary with mapping, system-wide change line, binary-then-source line (Task 4); safety scan and `sh -n` extended (Task 3); runner behavior (Task 5); docs (Task 6). Resolved from the spec's open items: service commands and the display-manager variable (SDDM wiki), `binrepos.conf` content and profile list (server), atoms (server tree). Left to the first real run, by design: the live session on NVIDIA 595 and the `rc-service display-manager status` wording.

Placeholders: none. Names used across tasks (`build`, `summary`, `plasma_profile`, `validated_desktop`, `desktop_inventory`, `PACKAGES`, `DM_CONF`, `USE_FILE`) are defined once and used consistently. `accept_licenses` handling is stated in Task 2 (refused) and Task 4 (optional parameter).
