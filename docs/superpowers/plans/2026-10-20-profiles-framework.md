# Provision Profiles Framework (plan 2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put provision plans behind a profile registry and a hardware profile derived from the first inventory, with no change to what the `nvidia-driver` profile generates.

**Architecture:** Four new read-only inventory commands feed a pure hardware profile (`core/hardware_profile.py`). A registry (`core/profiles.py`) maps a profile id to `applicable(hardware)`, `validate(settings)`, and the phase generators; `create_provision_plan`, `verify_provision_programs`, and `detect_provision` dispatch through it. The existing nvidia programs stay text-identical, pinned by the existing tests. The desktop-plasma purpose is plan 2b, written after a real inventory with the new commands (it needs the confirmed OpenRC service commands, `binrepos.conf`, the profile list, and the konsole/dolphin atoms).

**Tech Stack:** Python 3.12 standard library, unittest. Spec: `docs/superpowers/specs/2026-10-20-profiles-and-plasma-design.md`.

Binding rules (AGENTS.md, CLAUDE.md): `core/` never prints, prompts, or exits; every value from the target is untrusted and parsed strictly; no guessing of values; no Claude attribution lines in commits; never run `approve`; never touch `approvals/`, `plans/`, `config/`.

Baseline: `python -m unittest discover -s tests` is green (684 tests, MCP tests skip without the extra) before Task 1.

---

## File Structure

- Modify `gentoo_agent/core/ssh.py`: four commands in `PROVISION_READ_ONLY_COMMANDS`.
- Modify `gentoo_agent/core/provision_facts.py`: vendors AMD and Intel in `parse_gpus`; parsers `parse_arch`, `parse_portage_profile`, `parse_portage_profiles`, `parse_binrepos`; `summarize_provision_inventory` gains optional keys.
- Create `gentoo_agent/core/hardware_profile.py`: pure `video_cards()` and `hardware_profile()`.
- Create `gentoo_agent/core/profiles.py`: `Profile`, `PROFILES`, `get_profile()`, `profile_overview()`.
- Modify `gentoo_agent/core/provision_plan.py`: dispatch through the registry.
- Modify `gentoo_agent/mcp/server.py` and `gentoo_agent/cli.py`: `detect_provision` and `provision inspect` show `profiles`; `create_provision_plan` validates the profile id through the registry.
- Modify `docs/guides/provision.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`.
- Tests: `tests/test_hardware_profile.py` (new), `tests/test_profiles.py` (new), additions to `tests/test_provision_real_data.py`, `tests/provision_helpers.py`.

---

### Task 1: New read-only commands and strict parsers

**Files:**
- Modify: `gentoo_agent/core/ssh.py` (`PROVISION_READ_ONLY_COMMANDS`)
- Modify: `gentoo_agent/core/provision_facts.py`
- Modify: `tests/provision_helpers.py`
- Test: `tests/test_hardware_profile.py` (create)

Commands (exact text):

```python
    "arch": "uname -m",
    "portage_profile": "eselect profile show",
    "portage_profiles": "eselect profile list",
    "binrepos": "cat /etc/portage/binrepos.conf/*.conf 2>/dev/null || true",
```

Real output shapes (from the Gentoo wiki; the real server confirms them in the manual step before plan 2b):

```
$ eselect profile show
Current /etc/portage/make.profile symlink:
  default/linux/amd64/23.0
$ eselect profile list
Available profile symlink targets:
  [1]   default/linux/amd64/23.0 (stable) *
  [2]   default/linux/amd64/23.0/desktop (stable)
  [3]   default/linux/amd64/23.0/desktop/plasma (stable)
```

Parsing rules, all strict:

- `parse_arch(stdout)` returns `"amd64"` for `x86_64`, `"arm64"` for `aarch64`, anything else raises `INVENTORY_MISSING` (`_unusable("arch")`).
- `parse_portage_profile(stdout)` returns the profile name: the one non-header line, stripped, matching `[a-z0-9][a-z0-9/._+-]{0,127}`; anything else raises `_unusable("portage_profile")`.
- `parse_portage_profiles(stdout)` returns a list of `{"index": int, "name": str, "stability": str, "current": bool}` from lines matching `\s*\[(\d+)\]\s+(NAME)\s+\((stable|dev|exp)\)(?: (\*))?`; the header line `Available profile symlink targets:` is skipped; any other line raises `_unusable("portage_profiles")`; zero entries raises it too.
- `parse_binrepos(stdout)` returns `{"uris": [...]}`: the `sync-uri = <uri>` values (https only, regex `https://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]{1,200}`), ignoring blank lines, comments, `[section]` headers, and other keys; an unparsable `sync-uri` line raises `_unusable("binrepos")`; an empty file gives `{"uris": []}`.

`summarize_provision_inventory` gets four optional keys that are `None` when the inventory predates the commands (the label is absent) and raise `INVENTORY_MISSING` when the label is present but unusable: `architecture`, `portage_profile`, `portage_profiles`, `binrepos`. Existing keys and their values do not change.

- [ ] **Step 1: Write the failing tests** in `tests/test_hardware_profile.py` (class `NewCommandParserTests`): one test per parser for the accepted real shape (above), and per rejection: `parse_arch("riscv64\n")`, a profile name with a space or a newline injection (`"default/linux/amd64/23.0\nrm -rf /"`), a profile list line without a stability, a `binrepos` `sync-uri = http://...` (not https), and summarize with the labels absent (keys are `None`) and with `arch` present but `returncode` 1 (raises `INVENTORY_MISSING`). Add helper results `arch_result()`, `portage_profile_result()`, `portage_profiles_result()`, `binrepos_result()` to `tests/provision_helpers.py` following `kernel_result()`, and an `AMD_GPU_SYSFS` / `INTEL_GPU_SYSFS` line for Task 2 (format as `GPU_SYSFS`, vendor `0x1002` device `0x744c`, vendor `0x8086` device `0x46a6`, class `0x030000`).
- [ ] **Step 2: Run** `python -m unittest tests.test_hardware_profile -v`. Expected: FAIL (parsers missing).
- [ ] **Step 3: Implement** the commands in `ssh.py` and the parsers in `provision_facts.py`. Extend `VENDORS = {"10de": "nvidia", "1002": "amd", "8086": "intel"}` and `VENDOR_NAMES = {"10de": "NVIDIA", "1002": "AMD", "8086": "Intel"}`; the name stays `"<Vendor> display controller"`. Existing callers that test `vendor == "nvidia"` keep working; grep for `"other"` in tests and update only assertions about AMD or Intel ids if there are any.
- [ ] **Step 4: Run** the full suite. Expected: all green, including the existing provision tests (no program text changed).
- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/ssh.py gentoo_agent/core/provision_facts.py tests/provision_helpers.py tests/test_hardware_profile.py
git commit -m "Add read-only architecture, Portage profile, and binrepos facts to the provision inventory"
```

---

### Task 2: The hardware profile (pure)

**Files:**
- Create: `gentoo_agent/core/hardware_profile.py`
- Test: `tests/test_hardware_profile.py` (add `HardwareProfileTests`)

Interface:

```python
"""The hardware profile: what the first inventory says the machine is, as data a profile can decide on."""

from __future__ import annotations

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError

VIDEO_CARDS_BY_VENDOR = {"nvidia": ("nvidia",), "amd": ("amdgpu", "radeonsi"), "intel": ("intel",)}


def video_cards(gpus: list[dict]) -> list[str]:
    """VIDEO_CARDS values for the GPUs, sorted and without duplicates; an unknown vendor raises CONFIG_INVALID.

    No GPU gives an empty list. The vendor map is fixed here, never taken from the target.
    """


def hardware_profile(facts: dict) -> dict:
    """facts is the result of summarize_provision_inventory. Returns:
    {"architecture": "amd64"|"arm64"|None, "threads": int, "memory_gib": int,
     "gpus": [...same dicts...], "gpu_vendors": sorted unique vendor ids,
     "video_cards": [...]  or None when a vendor is unknown (see unknown_vendors),
     "unknown_vendors": [pci vendor ids that have no mapping],
     "virtual": bool}
    """
```

Details: a GPU with `vendor == "other"` is an unknown vendor; `hardware_profile` never raises for it (it records `unknown_vendors` and `video_cards: None`), `video_cards()` raises. `virtual` is true when every GPU has vendor id in `{"1234", "15ad", "1af4", "1b36"}` (QEMU/Bochs, VMware, virtio, Red Hat); this needs the PCI vendor id in the GPU dict, so extend `parse_gpus` to also return `"vendor_id": vendor` (a new key; existing dicts gain it, so update fixtures that compare whole GPU dicts, and `plan["provision"]["gpu"]` will carry the key: check `tests/test_provision_*.py` for equality on it and update them; the NVIDIA programs do not read it).

- [ ] **Step 1: Write failing tests** (`HardwareProfileTests`): the real server facts (RTX 5070 `10de:2c05`, 24 threads) give `video_cards == ["nvidia"]`, `gpu_vendors == ["nvidia"]`, `virtual is False`; AMD gives `["amdgpu", "radeonsi"]`; Intel gives `["intel"]`; AMD plus NVIDIA (hybrid) gives `["amdgpu", "nvidia", "radeonsi"]`; no GPU gives `[]`; a `10ec` display class device gives `video_cards is None` and `unknown_vendors == ["10ec"]`, and `video_cards(gpus)` raises `AgentError` with code `CONFIG_INVALID`; a virtio GPU (`1af4`) gives `virtual is True` and is an unknown vendor.
- [ ] **Step 2: Run** `python -m unittest tests.test_hardware_profile -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `hardware_profile.py` and the `vendor_id` key.
- [ ] **Step 4: Run** the full suite. Expected: green.
- [ ] **Step 5: Commit** `git commit -m "Add the hardware profile with VIDEO_CARDS derived from GPU vendors"`.

---

### Task 3: The registry and dispatch with no behavior change

**Files:**
- Create: `gentoo_agent/core/profiles.py`
- Modify: `gentoo_agent/core/provision_plan.py`
- Test: `tests/test_profiles.py` (create)

Interface:

```python
@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    applicable: Callable[[dict], str | None]       # hardware profile -> None, or the reason it does not apply
    validate: Callable[[object], dict]              # raw settings -> validated settings (profile, ... )
    phases: tuple[str, ...]
    flags: dict[str, tuple[bool, bool]]             # phase -> (destructive, run)
    command_for: Callable[[str, dict, dict], str]
    check_command_for: Callable[[str, dict, dict], str]


PROFILES: dict[str, Profile]                        # {"nvidia-driver": ...}

def get_profile(profile_id: object) -> Profile      # unknown id raises CONFIG_INVALID, field "profile"
def profile_overview(hardware: dict) -> list[dict]  # [{"id", "title", "applicable": bool, "reason": str|None}]
```

The nvidia-driver entry wraps the existing `validated_provision`, `PHASES`, `PHASE_FLAGS`, `command_for`, and `check_command_for` unchanged. Its `applicable` returns `None` with exactly one NVIDIA GPU, else `f"Expected exactly one NVIDIA GPU on the target, found {n}."` (the existing text).

`create_provision_plan` becomes:

1. `profile = get_profile(settings.get("profile") if isinstance(settings, dict) else None)`; `settings = profile.validate(settings)`;
2. `hardware = hardware_profile(facts)`; `reason = profile.applicable(hardware)`; if set, raise `AgentError(CONFIG_INVALID, reason, {"profile": profile.id})`;
3. the `gpu` block stays as today for nvidia (`nvidia[0]`); plan building uses `profile.phases`, `profile.flags`, `profile.command_for`, `profile.check_command_for`.

`verify_provision_programs` picks the profile from `plan["provision"]["profile"]` through `get_profile`. Plan JSON for the nvidia-driver profile must stay byte-identical for the same inputs apart from the new `vendor_id` key inside `gpu` (Task 2); that key changes `plan_hash`, which is acceptable because no stored plan is migrated (old plans fail `verify_provision_programs`? No: it compares programs, not the gpu dict; confirm with the test in step 1).

- [ ] **Step 1: Write failing tests** (`tests/test_profiles.py`): `get_profile("nvidia-driver").id`; `get_profile("desktop-plasma")` raises `CONFIG_INVALID`; `profile_overview` for the real fixtures lists `nvidia-driver` applicable; for an AMD-only inventory it is not applicable with the reason text; `create_provision_plan` for an AMD-only inventory raises `CONFIG_INVALID` with that reason and no plan; the golden check: `create_provision_plan` on the existing fixtures gives phases whose `apply` and `check` strings equal the output of `provision_phases.command_for` and `check_command_for` for every phase (guards "text-identical"); a plan stored before this change (build one with `save_provision_plan`, delete `vendor_id` from `plan["provision"]["gpu"]`) still passes `verify_provision_programs`.
- [ ] **Step 2: Run** `python -m unittest tests.test_profiles -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `profiles.py` and the dispatch.
- [ ] **Step 4: Run** the full suite. Expected: green; in particular every existing `test_provision_*` and `test_execute_plan` test passes unchanged.
- [ ] **Step 5: Commit** `git commit -m "Route provision plans through a profile registry"`.

---

### Task 4: `detect_provision` and CLI show the profiles

**Files:**
- Modify: `gentoo_agent/core/profiles.py` (`provision_overview`)
- Modify: `gentoo_agent/mcp/server.py` (`detect_provision`, `create_provision_plan` docstring)
- Modify: `gentoo_agent/cli.py` (`cmd_provision_inspect`)
- Test: `tests/test_profiles.py`, MCP tests in the existing `tests/test_mcp_*` file that covers `detect_provision` (grep for it)

`provision_overview(inventory) -> dict`: `{**facts, "hardware": hardware_profile(facts), "profiles": profile_overview(hardware)}` where `facts = summarize_provision_inventory(inventory)`. `detect_provision` and `provision inspect` return it instead of the bare summary; existing keys are unchanged, so no consumer breaks. Update the MCP instructions text in `server.py` only where it names the nvidia profile as the single choice (keep the rule: license names and package.use lines come only from the human).

- [ ] **Step 1: Write failing tests**: `provision_overview` contains `profiles` with `nvidia-driver` applicable and `hardware["video_cards"] == ["nvidia"]`; the MCP `detect_provision` structured content carries `profiles` (skips without the extra, like the other MCP tests); the CLI test, if one exists for `provision inspect`, still passes.
- [ ] **Step 2: Run**; expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite, then `.venv/bin/python -m unittest discover -s tests -v` if the venv exists. Expected: green.
- [ ] **Step 5: Commit** `git commit -m "Report applicable profiles and the hardware profile from detect_provision"`.

---

### Task 5: Documentation

**Files:** `docs/guides/provision.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `CLAUDE.md` (module list: `hardware_profile.py`, `profiles.py`, the four commands).

- [ ] **Step 1:** Describe the hardware profile, the registry, the `profiles` field of `detect_provision`, the four new read-only commands, and that a profile applies only when its reason is `None`. State plainly that only `nvidia-driver` exists and that `desktop-plasma` follows in plan 2b.
- [ ] **Step 2:** In the ROADMAP section "Product requirement: hardware-driven, purpose-driven profiles", mark the framework done and list what remains (AMD, Intel, hybrid, virtual, no-GPU profiles; desktop purposes).
- [ ] **Step 3: Run** `python -m unittest discover -s tests`. Expected: green (the core purity and doc tests included).
- [ ] **Step 4: Commit** `git commit -m "Document the hardware profile and the profile registry"`.

---

### Manual step after Task 5 (operator action, not part of the subagent run)

On the real server 10.0.1.17, after the MCP server is restarted (see memory `mcp-server-reload`): `detect_provision(refresh=true)` collects the inventory with the four new commands. Check from the saved `inventory/provision-inventory-*.json` and ask the operator to run, in their own terminal on the target: `eselect profile list`, `cat /etc/portage/binrepos.conf/*.conf`, the atom check `emerge --pretend --getbinpkg kde-plasma/plasma-meta kde-apps/konsole kde-apps/dolphin`, and `rc-service --list | grep -E 'dbus|elogind|sddm|display'`. Their outputs fix the exact strings that plan 2b hard-codes, so nothing is guessed.

---

## Self-Review

Spec coverage: hardware profile as a pure function (Tasks 1 and 2); four new read-only commands (Task 1); fixed `VIDEO_CARDS` map with refusal of an unknown vendor (Task 2); registry with `applicable`, `validate`, phase generators (Task 3); `create_provision_plan` dispatch (Task 3); `detect_provision` with `profiles`, applicability, and reasons (Task 4); nvidia programs text-identical (Task 3 golden check); docs (Task 5). Not in this plan, by design: the `desktop-plasma` phases, the approval summary per profile, and the Plasma `package_use` rules (plan 2b, after real data).

Placeholders: none. Names used across tasks: `parse_arch`, `parse_portage_profile`, `parse_portage_profiles`, `parse_binrepos`, `video_cards`, `hardware_profile`, `get_profile`, `profile_overview`, `provision_overview`, `vendor_id` are each defined once and used consistently.
