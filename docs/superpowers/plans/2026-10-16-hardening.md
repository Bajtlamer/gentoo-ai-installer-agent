# Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the known safety and correctness gaps found in plans 1-3 before the first full installation on a real virtual machine.

**Architecture:** Each task is a focused change to one area: generated shell programs (`core/phases.py`), configuration validation (`core/validate.py`), plan creation (`core/install_plan.py`), execution (`core/runner.py`), SSH error mapping (`core/ssh.py`, `core/inventory.py`, `core/disks.py`), run liveness (`core/runs.py`, `jobs/`), and a stdio smoke test of the MCP server. Tasks 1 to 3 change generated commands and therefore the golden files; every other task leaves them byte-identical.

**Tech Stack:** Python 3.12+ standard library (`unittest`, `fcntl`, `shlex`), optional `mcp==2.2.0` for the MCP tests (run them with `.venv/bin/python`).

---

## Ground rules for every task

- Read `AGENTS.md` and `CLAUDE.md` first. Never contact a real host, never run `approve`, never write under `approvals/`, `plans/`, `runs/`, `secrets/`, or `config/*.json` of the repository (tests use `tests/helpers.temp_home()`).
- `gentoo_agent/core/` must not print, prompt, or exit (`tests/test_core_purity.py`).
- Every value in a generated program is quoted with `shlex.quote` (`q()` in `phases.py`).
- No em dashes and no emoji in code comments, messages, or docs.
- Full suite: `.venv/bin/python -m unittest discover -s tests` (create the venv with `python3 -m venv .venv && .venv/bin/pip install -e '.[mcp]'` if it is missing). Expected before Task 1: `Ran 270 tests ... OK`.
- Regenerate golden files only in the tasks that say so: `GOLDEN_UPDATE=1 .venv/bin/python -m unittest tests.test_golden_commands`, then read the diff of `tests/golden/*.txt` and confirm that only the intended lines changed.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## File map

| File | Tasks | Change |
|---|---|---|
| `gentoo_agent/core/phases.py` | 1, 2, 3 | `set -eu` in every apply program; OpenRC stage only; static network block in `/etc/dhcpcd.conf` |
| `gentoo_agent/core/validate.py` | 2, 3 | `init_system` must be `openrc`; `network_static.interface` |
| `gentoo_agent/core/resolve.py` | 2 | comment only |
| `gentoo_agent/core/approval.py` | 3 | interface in the dialog text |
| `gentoo_agent/init_config.py`, `gentoo_agent/prepare.py` | 2, 3, 4 | no systemd choice; interface prompt; erase authorization saved in `target.json` |
| `gentoo_agent/core/install_plan.py` | 5 | review, inventory age, expected disk checks |
| `gentoo_agent/core/runner.py` | 6 | drift check before each destructive apply |
| `gentoo_agent/core/errors.py`, `ssh.py`, `inventory.py`, `disks.py` | 7 | `HOST_KEY_UNKNOWN` |
| `gentoo_agent/core/runs.py`, `jobs/worker.py`, `jobs/launcher.py` | 8 | per-run worker flock; reaper thread |
| `tests/test_mcp_stdio.py` | 9 | new stdio smoke test |
| `tests/golden/configs/*.json`, `tests/golden/*.txt` | 1, 2, 3, 5 | fixtures and regenerated programs |
| docs | 10 | reference, guides, roadmap, troubleshooting |

---

### Task 1: Every apply program stops on its first error

The configuration apply program is `chroot ...` followed by `touch <marker>` with no outer `set -e`, so a failure inside the chroot still writes the completion marker and the program exits 0. The authentication program without passwords has the same shape. Kernel and bootloader are a single chroot line today; give them the same header so the rule is uniform.

**Files:**
- Modify: `gentoo_agent/core/phases.py` (`configuration_command`, `authentication_command`, `kernel_command`, `bootloader_command`)
- Test: `tests/test_phases.py`
- Regenerate: `tests/golden/*.txt`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_phases.py` (before the `if __name__` block if there is one; add `import os`, `import subprocess`, `import tempfile` at the top):

```python
class ApplyProgramStrictnessTests(unittest.TestCase):
    def test_every_apply_program_starts_with_set_eu(self):
        for config_path in sorted((ROOT / "tests" / "golden" / "configs").glob("*.json")):
            install = json.loads(config_path.read_text(encoding="utf-8"))
            for phase in phases.PHASES:
                with self.subTest(config=config_path.stem, phase=phase):
                    with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
                        program = phases.command_for(phase, TARGET, install)
                    self.assertEqual(program.splitlines()[0], "set -eu")

    def test_failed_chroot_does_not_write_the_configuration_marker(self):
        with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
            program = phases.configuration_command(TARGET, fixture("bios_mbr_user_sudo"))
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            touched = bin_dir / "touched"
            (bin_dir / "chroot").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (bin_dir / "touch").write_text(f"#!/bin/sh\necho \"$@\" >> {shlex.quote(str(touched))}\n", encoding="utf-8")
            for name in ("chroot", "touch"):
                os.chmod(bin_dir / name, 0o755)
            result = subprocess.run(["/bin/sh", "-c", program], env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
                                    capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(touched.exists())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_phases.ApplyProgramStrictnessTests -v`
Expected: FAIL. `configuration`, `kernel`, and `bootloader` start with `chroot`, and the marker test sees `touched` written with exit code 0.

- [ ] **Step 3: Add `set -eu` to the four generators**

In `gentoo_agent/core/phases.py`:

`configuration_command`, last line:

```python
    return "\n".join(["set -eu", chroot(inside), f"touch {q(MOUNT_ROOT + '/.gentoo-agent-configuration-complete')}"])
```

`authentication_command`, the branch without passwords:

```python
    if not (install.get("set_root_password", True) or create_user):
        return "\n".join(["set -eu", chroot("\n".join(inside)), marker])
```

`kernel_command`, last line:

```python
    return "\n".join(["set -eu", chroot(inside)])
```

`bootloader_command`, last line:

```python
    return "\n".join(["set -eu", chroot(inside)])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_phases -v`
Expected: PASS.

- [ ] **Step 5: Regenerate the golden files and check the diff**

Run: `GOLDEN_UPDATE=1 .venv/bin/python -m unittest tests.test_golden_commands && git diff --stat tests/golden && git diff tests/golden | grep '^[-+]' | grep -v '^[-+][-+]' | sort | uniq -c`
Expected: only added `+set -eu` lines (configuration, kernel, bootloader in every file; authentication only where it had no `set -eu`).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m unittest discover -s tests`
Expected: OK.

- [ ] **Step 7: Commit**

```bash
git add gentoo_agent/core/phases.py tests/test_phases.py tests/golden
git commit -m "Stop every apply program on its first error

Configuration ran chroot and then touched its completion marker without
set -e, so a failure inside the chroot was reported as success.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Refuse systemd everywhere

Only `resolve_config` (MCP) refuses systemd. `validated_settings`, `init_config --init-system`, and `prepare_install.py` still accept it, while the configuration, authentication, and validation phases are OpenRC only (`rc-update`, `/etc/runlevels`). Refuse it at the single gate.

**Files:**
- Modify: `gentoo_agent/core/validate.py:11,127-129`, `gentoo_agent/core/phases.py:174-176`, `gentoo_agent/core/resolve.py:51-53`, `gentoo_agent/init_config.py`, `gentoo_agent/prepare.py:153`
- Rename: `tests/golden/configs/uefi_gpt_systemd_sudo_nokey.json` to `uefi_gpt_openrc_sudo_nokey.json`, `tests/golden/uefi_gpt_systemd_sudo_nokey.txt` to `uefi_gpt_openrc_sudo_nokey.txt`
- Test: `tests/test_validate.py`, `tests/test_phases.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_validate.py`, remove `("systemd", changed("uefi_gpt_user_sudo", init_system="systemd")),` from `VALID` and add to the invalid list (the list whose entries are `(name, install, field)`):

```python
    ("systemd", changed("uefi_gpt_user_sudo", init_system="systemd"), "init_system"),
```

Append to `tests/test_phases.py`:

```python
class InitSystemTests(unittest.TestCase):
    def test_base_system_always_uses_the_openrc_stage(self):
        program = phases.base_system_command(fixture("uefi_gpt_user_sudo"))
        self.assertIn("latest-stage3-amd64-openrc.txt", program)
        self.assertNotIn("systemd", program)

    def test_systemd_is_refused_by_the_gate(self):
        install = fixture("uefi_gpt_user_sudo")
        install["init_system"] = "systemd"
        with self.assertRaises(AgentError) as caught:
            phases.validated_settings(TARGET, install)
        self.assertEqual(caught.exception.details, {"field": "init_system"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_validate tests.test_phases.InitSystemTests -v`
Expected: FAIL (`systemd` is accepted).

- [ ] **Step 3: Implement**

`gentoo_agent/core/validate.py`:

```python
# Version 1 generates OpenRC programs only (rc-update, /etc/runlevels).
INIT_SYSTEMS = {"openrc"}
```

and in `validated_settings`:

```python
    init_system = install.get("init_system")
    if not isinstance(init_system, str) or init_system not in INIT_SYSTEMS:
        raise _invalid("init_system", "init_system must be 'openrc'; systemd is not supported in this version")
```

`gentoo_agent/core/phases.py`, `base_system_command`:

```python
def base_system_command(install: dict) -> str:
    stage_list = "https://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3-amd64-openrc.txt"
```

(delete the `variant = ...` line; the rest of the function is unchanged).

`gentoo_agent/core/resolve.py`, replace the comment above `SUPPORTED_INIT_SYSTEM`:

```python
# Version 1 generates OpenRC programs only; validated_settings() refuses
# systemd as well. Checking here gives the model a clearer early error.
```

`gentoo_agent/init_config.py`:

```python
    parser.add_argument("--init-system", choices=("openrc",), default="openrc",
                        help="Only openrc is supported in this version")
```

`gentoo_agent/prepare.py`, replace the `init_system = ask_choice(...)` line:

```python
    init_system = "openrc"
    print("Init system: openrc (systemd is not supported in this version)")
```

- [ ] **Step 4: Rename the systemd golden fixture**

```bash
git mv tests/golden/configs/uefi_gpt_systemd_sudo_nokey.json tests/golden/configs/uefi_gpt_openrc_sudo_nokey.json
git mv tests/golden/uefi_gpt_systemd_sudo_nokey.txt tests/golden/uefi_gpt_openrc_sudo_nokey.txt
```

In the renamed JSON set `"init_system": "openrc"`. Then search for the old name and update every hit:

Run: `grep -rn "uefi_gpt_systemd_sudo_nokey" tests gentoo_agent docs CLAUDE.md README.md`
Expected after editing: no output (old plan files under `docs/superpowers/plans/` are history; leave them).

- [ ] **Step 5: Regenerate the golden files and check the diff**

Run: `GOLDEN_UPDATE=1 .venv/bin/python -m unittest tests.test_golden_commands && git diff -M tests/golden`
Expected: only `uefi_gpt_openrc_sudo_nokey.txt` changes, and only the stage URL (`systemd` to `openrc`).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m unittest discover -s tests`
Expected: OK. If a test elsewhere built a systemd config as valid, change it to expect `CONFIG_INVALID` with `{"field": "init_system"}`.

- [ ] **Step 7: Commit**

```bash
git add -A gentoo_agent tests
git commit -m "Refuse systemd at the validation gate and in every front end

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Working static networking

Today the static settings go to `/etc/dhcpcd.conf.d/gentoo-agent.conf`, which Gentoo's dhcpcd does not read, and the interface is hardcoded as `ens33`. Write a marked block into `/etc/dhcpcd.conf` (replaced on every run, so the phase stays idempotent) and require the interface name in the configuration.

**Files:**
- Modify: `gentoo_agent/core/validate.py` (`_check_network`), `gentoo_agent/core/phases.py` (`configuration_command`, `configuration_check_command`), `gentoo_agent/core/approval.py:96-102`, `gentoo_agent/prepare.py:155-161`
- Modify fixture: `tests/golden/configs/uefi_gpt_root_only_static.json`
- Test: `tests/test_validate.py`, `tests/test_phases.py`, `tests/test_approval.py:214`

- [ ] **Step 1: Write the failing tests**

`tests/test_validate.py`: change `STATIC` to

```python
STATIC = {"interface": "enp1s0", "address": "192.168.10.20/24", "gateway": "192.168.10.1", "dns": ["1.1.1.1"]}
```

and add to the invalid list:

```python
    ("interface missing", static(interface=DROP), "network_static"),
    ("interface with space", static(interface="enp1s0 x"), "network_static"),
    ("interface injection", static(interface="eth0;reboot"), "network_static"),
    ("interface too long", static(interface="a" * 16), "network_static"),
    ("interface not a string", static(interface=1), "network_static"),
```

and to `VALID`:

```python
    ("static interface with dot", static(interface="eth0.10")),
```

`tests/test_phases.py`, in `ConfigurationQuotingTests` replace `network_tokens`, `test_static_network_lines_are_quoted_once`, and `test_hostile_static_values_stay_single_arguments` with:

```python
    def network_tokens(self, lines: list[str]) -> list[str]:
        line = next(line for line in lines if line.startswith("printf") and line.endswith(">> /etc/dhcpcd.conf"))
        return shlex.split(line)

    def test_static_network_lines_are_quoted_once(self):
        tokens = self.network_tokens(self.configuration())
        self.assertEqual(tokens[2:8], [
            "# gentoo-agent static network begin",
            "interface enp1s0",
            "static ip_address=192.168.10.20/24",
            "static routers=192.168.10.1",
            "static domain_name_servers=1.1.1.1 9.9.9.9",
            "# gentoo-agent static network end",
        ])

    def test_previous_block_is_removed_before_the_new_one_is_written(self):
        lines = self.configuration()
        sed = next(index for index, line in enumerate(lines) if line.startswith("sed -i"))
        write = next(index for index, line in enumerate(lines) if line.endswith(">> /etc/dhcpcd.conf"))
        self.assertLess(sed, write)
        self.assertEqual(shlex.split(lines[sed]), [
            "sed", "-i", "/^# gentoo-agent static network begin$/,/^# gentoo-agent static network end$/d",
            "/etc/dhcpcd.conf",
        ])
        self.assertFalse(any("dhcpcd.conf.d" in line for line in lines))

    def test_hostile_static_values_stay_single_arguments(self):
        hostile = "10.0.0.2/24 x; touch /PWNED"
        base = {"interface": "enp1s0", "address": "10.0.0.2/24", "gateway": "10.0.0.1", "dns": ["1.1.1.1"]}
        for change in (
            {"address": hostile},
            {"gateway": "a'b; touch /PWNED"},
            {"dns": ["1.1.1.1'; touch /PWNED; '"]},
            {"interface": "eth0'; touch /PWNED; '"},
        ):
            with self.subTest(change=change):
                tokens = self.network_tokens(self.configuration(network_static={**base, **change}))
                self.assertNotIn("touch", tokens)
                self.assertEqual(len(tokens), 10)
        tokens = self.network_tokens(self.configuration(network_static={**base, "address": hostile}))
        self.assertIn("static ip_address=" + hostile, tokens)

    def test_static_check_reads_dhcpcd_conf(self):
        install = fixture("uefi_gpt_root_only_static")
        with patch.object(phases, "public_key_for_target", return_value="ssh-ed25519 AAAATEST test"):
            check = phases.configuration_check_command(TARGET, install)
        self.assertIn("grep -Fqx 'interface enp1s0' /mnt/gentoo/etc/dhcpcd.conf", check)
        self.assertIn("grep -Fqx 'static ip_address=192.168.10.20/24' /mnt/gentoo/etc/dhcpcd.conf", check)
```

`tests/golden/configs/uefi_gpt_root_only_static.json`: set

```json
  "network_static": {"interface": "enp1s0", "address": "192.168.10.20/24", "gateway": "192.168.10.1", "dns": ["1.1.1.1", "9.9.9.9"]},
```

`tests/test_approval.py` near line 214: add `"interface": "enp1s0"` to the `network_static` dict there, and in the test that checks the static network line add:

```python
        self.assertIn("interface=enp1s0", text)
```

(`text` is whatever variable that test already uses for the summary; read the test first and use its name.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_validate tests.test_phases tests.test_approval -v`
Expected: FAIL (the interface key is rejected by the current key-set check, the program still writes `dhcpcd.conf.d`).

- [ ] **Step 3: Validate the interface**

`gentoo_agent/core/validate.py`, add near the other patterns:

```python
# Linux interface names: at most 15 characters (IFNAMSIZ - 1), no spaces or shell syntax.
INTERFACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
```

In `_check_network`, replace the message and key check, and check the interface right after:

```python
    message = ("network_static must be an object with interface (for example enp1s0), address (CIDR), "
               "gateway (IP address), and dns (a list of 1 to 3 IP addresses)")
    if not isinstance(static, dict) or set(static) != {"interface", "address", "gateway", "dns"}:
        raise _invalid("network_static", message)
    interface_name = static["interface"]
    if not isinstance(interface_name, str) or not INTERFACE_NAME.fullmatch(interface_name):
        raise _invalid("network_static", "network_static.interface must be a Linux interface name such as enp1s0")
```

- [ ] **Step 4: Generate the dhcpcd.conf block**

`gentoo_agent/core/phases.py`, add below `MOUNT_ROOT`:

```python
DHCPCD_CONF = "/etc/dhcpcd.conf"
DHCPCD_BEGIN = "# gentoo-agent static network begin"
DHCPCD_END = "# gentoo-agent static network end"
```

In `configuration_command` replace the whole `else:` branch of `if network == "dhcp":` with:

```python
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
```

In `configuration_check_command`, build the line list in a variable and add the static checks before the final `printf`:

```python
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
```

- [ ] **Step 5: Show the interface in the approval dialog**

`gentoo_agent/core/approval.py`, the static branch:

```python
        lines.append(
            f"Network: static interface={_shown(static.get('interface'))} address={_shown(static.get('address'))} "
            f"gateway={_shown(static.get('gateway'))} dns={_shown(dns)}"
        )
```

- [ ] **Step 6: Ask for the interface in prepare_install.py**

`gentoo_agent/prepare.py`, the static block in `build_install_config`:

```python
    if network_method == "static":
        static_network = {
            "interface": ask("Network interface on the installed system (see the inventory network section), for example enp1s0"),
            "address": ask("Static address in CIDR notation"),
            "gateway": ask("Default gateway"),
            "dns": [item.strip() for item in ask("DNS servers, comma separated").split(",") if item.strip()],
        }
```

- [ ] **Step 7: Run the tests, regenerate golden, check the diff**

Run: `.venv/bin/python -m unittest tests.test_validate tests.test_phases tests.test_approval -v`
Expected: PASS.

Run: `GOLDEN_UPDATE=1 .venv/bin/python -m unittest tests.test_golden_commands && git diff tests/golden`
Expected: only `uefi_gpt_root_only_static.txt` changes: the dhcpcd lines in the configuration apply and two new `grep -Fqx` lines in the configuration check.

Run: `grep -rn "network_static" config docs/CONFIGURATION.md tests/test_resolve.py`
Expected: note each hit; `config/*.example.json` entries that contain a static block must gain `"interface"` (Task 10 updates the docs).

- [ ] **Step 8: Run the full suite and commit**

Run: `.venv/bin/python -m unittest discover -s tests`
Expected: OK.

```bash
git add -A gentoo_agent tests config
git commit -m "Write static network settings to dhcpcd.conf for a named interface

dhcpcd on Gentoo does not read dhcpcd.conf.d, and the interface was
hardcoded as ens33. network_static now requires an interface name.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: prepare_install.py records the erase authorization in target.json

`create_install_plan` requires `target_is_disposable` and `disk_erase_authorized` in both `install.json` and `target.json`. `prepare_install.py` copies them from `target.json` into `install.json` but never sets them, so after the operator types `ERASE /dev/sda`, `plan create` still fails with `CONFIG_INVALID`. The typed confirmation is the operator's decision; record it in both files.

**Files:**
- Modify: `gentoo_agent/prepare.py` (`build_install_config`, `main`)
- Test: `tests/test_prepare.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_prepare.py` (add imports `from gentoo_agent.core.config import load_json, save_json` and `from tests.helpers import temp_home`):

```python
REPO = Path(__file__).resolve().parents[1]


class EraseAuthorizationTests(unittest.TestCase):
    def build(self, erase_answer: str) -> tuple[dict, dict]:
        target = {"name": "gentoo", "host": "192.0.2.10", "user": "root", "identity_file": "~/.ssh/id_test"}

        def fake_ask(prompt, default=None):
            if prompt.startswith("Type ERASE"):
                return erase_answer
            return default if default is not None else ""

        with temp_home() as home:
            save_json(home / "config" / "install.default.json", load_json(REPO / "config" / "install.default.json"))
            with patch.object(prepare, "choose_disk", return_value={"path": "/dev/sda", "model": "QEMU HARDDISK", "size": None}), \
                    patch.object(prepare, "ask", fake_ask), \
                    patch.object(prepare, "ask_choice", lambda prompt, choices, default: default), \
                    patch.object(prepare, "ask_int", lambda prompt, default: default), \
                    redirect_stdout(io.StringIO()):
                install = prepare.build_install_config(target, {"results": []})
        return target, install

    def test_typed_erase_confirmation_authorizes_both_files(self):
        target, install = self.build("ERASE /dev/sda")
        self.assertIs(target["target_is_disposable"], True)
        self.assertIs(target["disk_erase_authorized"], True)
        for field in ("disk_must_be_erased", "target_is_disposable", "disk_erase_authorized"):
            self.assertIs(install[field], True, field)

    def test_declined_confirmation_authorizes_nothing(self):
        target, install = self.build("CANCEL")
        self.assertNotIn("target_is_disposable", target)
        self.assertIs(install["disk_must_be_erased"], False)
        self.assertIs(install["disk_erase_authorized"], False)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_prepare.EraseAuthorizationTests -v`
Expected: FAIL on `target["target_is_disposable"]` (KeyError) in the first test. If `result_map({"results": []})` or `size_gib(None)` raises instead, read those helpers and adjust the fake inventory or disk in the test, not the code.

- [ ] **Step 3: Implement**

`gentoo_agent/prepare.py`, in `build_install_config` right after the `if preauthorized: ... else: ...` block:

```python
    if disk_must_be_erased:
        # Plan creation requires the authorization in target.json as well; the
        # typed ERASE line (or the existing pre-authorization) is that decision.
        target["target_is_disposable"] = True
        target["disk_erase_authorized"] = True
```

In `main`, right after `save_json(paths.config_dir() / "install.json", install)`:

```python
    save_json(paths.config_dir() / "target.json", target)
```

- [ ] **Step 4: Run the tests and the full suite**

Run: `.venv/bin/python -m unittest tests.test_prepare -v && .venv/bin/python -m unittest discover -s tests`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/prepare.py tests/test_prepare.py
git commit -m "Record the typed erase confirmation in target.json as well

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: plan create runs the review, refuses stale inventory, and checks the expected disk

`create_install_plan` runs only `validated_settings`. The broader review in `validate_installation` (required keys, disk present in the storage inventory, boot mode against the live boot) and the operator's `expected_disk_model` / `expected_disk_size_gib` are only checked later, by `validate` or by preflight at run time. Plans must also not be built from an old inventory.

**Files:**
- Modify: `gentoo_agent/core/install_plan.py`
- Modify: `tests/helpers.py` (new fixture helpers), the five `tests/golden/configs/*.json` (add `profile`, `bootloader`, `swap_mb`)
- Test: `tests/test_install_plan.py`, then fix fixtures in `tests/test_cli.py`, `tests/test_launcher.py`, `tests/test_mcp_server.py`, and any other test that calls `create_install_plan`

- [ ] **Step 1: Add fixture helpers**

Append to `tests/helpers.py`:

```python
def storage_result(*disks: str) -> dict:
    """A storage inventory result that lists the given disk paths."""
    lines = ["NAME PATH SIZE TYPE"] + [f"{path.rsplit('/', 1)[-1]} {path} 40G disk" for path in disks]
    return {"label": "storage", "returncode": 0, "stdout": "\n".join(lines) + "\n"}


def fresh_inventory(*results: dict) -> dict:
    """An inventory collected now, so plan creation accepts its age."""
    from gentoo_agent.core.config import now

    return {"target": "gentoo", "collected_at": now(), "results": list(results)}
```

- [ ] **Step 2: Complete the golden configs**

Add these keys to each file in `tests/golden/configs/` (they do not change any generated command):

```json
  "profile": "gentoo-amd64-openrc",
  "bootloader": "grub",
  "swap_mb": 2048,
```

Use `"swap_mb": 0` in `uefi_gpt_root_only_static.json` (no swap partition). For the others, use the `size_mib` of the `linux-swap` partition in that file.

Run: `.venv/bin/python -m unittest tests.test_golden_commands`
Expected: OK (golden text unchanged).

- [ ] **Step 3: Write the failing tests**

In `tests/test_install_plan.py`, change `inventory()` to return a fresh, complete inventory:

```python
from tests.helpers import fresh_inventory, storage_result, temp_home


def inventory(serial: str = "QM00001", collected_at: str | None = None) -> dict:
    stdout = json.dumps({"blockdevices": [
        {"path": "/dev/sda", "type": "disk", "size": 42949672960, "model": "QEMU HARDDISK", "serial": serial, "wwn": None},
    ]})
    data = fresh_inventory(
        {"label": "boot_mode", "returncode": 0, "stdout": "bios-or-unknown"},
        storage_result("/dev/sda"),
        {"label": "disk_identity", "returncode": 0, "stdout": stdout},
    )
    if collected_at is not None:
        data["collected_at"] = collected_at
    return data
```

Add to `CreatePlanTests`:

```python
    def test_review_errors_block_the_plan(self):
        # validated_settings() does not check the boot_mode value for BIOS tables; the review does.
        with self.assertRaises(AgentError) as caught:
            self.create(install={"boot_mode": "efi"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertIn("boot_mode must be 'uefi' or 'bios'", caught.exception.details["errors"])

    def test_review_needs_the_disk_in_the_storage_inventory(self):
        data = inventory()
        data["results"] = [item for item in data["results"] if item["label"] != "storage"]
        with self.assertRaises(AgentError) as caught:
            self.create(inventory=data)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertIn("configured disk /dev/sda was not found in the latest inventory", caught.exception.details["errors"])

    def test_stale_inventory_is_refused(self):
        for collected_at in ("2020-01-01T00:00:00+00:00", "not a date", ""):
            with self.subTest(collected_at=collected_at):
                with self.assertRaises(AgentError) as caught:
                    self.create(inventory=inventory(collected_at=collected_at))
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_expected_disk_model_must_match_the_inventory(self):
        with self.assertRaises(AgentError) as caught:
            self.create(install={"expected_disk_model": "Samsung SSD"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(caught.exception.details["field"], "expected_disk_model")

    def test_expected_disk_size_must_match_within_one_gib(self):
        self.create(install={"expected_disk_size_gib": 40})
        with self.assertRaises(AgentError) as caught:
            self.create(install={"expected_disk_size_gib": 60})
        self.assertEqual(caught.exception.details["field"], "expected_disk_size_gib")
```

- [ ] **Step 4: Run them to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_install_plan -v`
Expected: the five new tests FAIL; the existing ones still PASS.

- [ ] **Step 5: Implement**

`gentoo_agent/core/install_plan.py`: add `import datetime as dt`, import `validate_installation` next to `validated_settings`, and add:

```python
INVENTORY_MAX_AGE = dt.timedelta(hours=24)
GIB = 1024 ** 3


def _check_inventory_fresh(inventory: dict) -> None:
    collected = inventory.get("collected_at")
    try:
        moment = dt.datetime.fromisoformat(collected) if isinstance(collected, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None or dt.datetime.now(dt.timezone.utc) - moment > INVENTORY_MAX_AGE:
        raise AgentError(INVENTORY_MISSING, "The latest inventory is undated or older than 24 hours; run inspect again.",
                         {"collected_at": collected})


def _check_review(target: dict, install: dict, project_plan: dict, inventory: dict) -> None:
    review = validate_installation(target, install, project_plan, inventory)
    if review["errors"]:
        raise AgentError(CONFIG_INVALID, "The configuration does not pass review: " + "; ".join(review["errors"]),
                         {"errors": review["errors"]})


def _check_expected_disk(install: dict, disk: dict) -> None:
    """The operator's expected model and size, checked against the inventory before anything is approved.

    Preflight checks the same values on the live target; the size tolerance matches it (under 1 GiB).
    """
    model = install.get("expected_disk_model")
    if model and model != disk["model"]:
        raise AgentError(CONFIG_INVALID, f"Disk {disk['path']} is a {disk['model']!r}, not the expected {model!r}.",
                         {"field": "expected_disk_model"})
    size = install.get("expected_disk_size_gib")
    if size:
        if abs(disk["size_bytes"] - int(float(size) * GIB)) >= GIB:
            raise AgentError(CONFIG_INVALID, f"Disk {disk['path']} is {disk['size_bytes'] / GIB:.1f} GiB, "
                             f"not the expected {size} GiB.", {"field": "expected_disk_size_gib"})
```

and in `create_install_plan`, after the `INVENTORY_MISSING` check and `_check_erase_authorized`:

```python
    _check_inventory_fresh(inventory)
    _check_review(target, install, project_plan, inventory)
    disk = disk_from_inventory(inventory, install["disk"])
    _check_expected_disk(install, disk)
```

(the existing `disk = disk_from_inventory(...)` line moves into this block).

- [ ] **Step 6: Run the plan tests**

Run: `.venv/bin/python -m unittest tests.test_install_plan -v`
Expected: PASS.

- [ ] **Step 7: Fix the remaining fixtures**

Run: `.venv/bin/python -m unittest discover -s tests 2>&1 | grep -E "^(FAIL|ERROR):"`

For each failing test that builds an inventory by hand (`tests/test_cli.py` `test_plan_create_saves_a_plan_from_the_config`, `tests/test_launcher.py` `approved_plan`, `tests/test_mcp_server.py` `write_home` with `"collected_at": "x"`, and any other hit), build it with `fresh_inventory(...)` and include `storage_result(<disk>)`. For the MCP fixture keep its existing `boot_mode` result and add `storage_result(*disks)`. Do not weaken the new checks to make a fixture pass.

Run: `.venv/bin/python -m unittest discover -s tests`
Expected: OK.

- [ ] **Step 8: Commit**

```bash
git add gentoo_agent/core/install_plan.py tests
git commit -m "Review the configuration, inventory age, and expected disk at plan creation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Re-check the disk identity before every destructive apply

`execute_plan` checks the disk identity once, before the first phase. A run takes an hour or more, so check again right before each phase that will apply a destructive program (phases whose check already passes are skipped and need no check).

**Files:**
- Modify: `gentoo_agent/core/runner.py` (the phase loop)
- Test: `tests/test_execute_plan.py`

- [ ] **Step 1: Write the failing tests**

Append to `ExecutePlanTests` in `tests/test_execute_plan.py`:

```python
    def test_drift_is_rechecked_before_each_destructive_apply(self):
        with temp_home():
            shell = FakeShell({"preflight-check": 1, "authentication-check": 1, "kernel-check": 1})
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "running",
                                    "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", shell), patch.object(runner, "check_drift") as drift:
                result = runner.execute_plan(PLAN, run_id, RECORDS)
        self.assertEqual(result["status"], "installed_pending_reboot")
        # once at the start, then before authentication and kernel (preflight is not destructive)
        self.assertEqual(drift.call_count, 3)

    def test_skipped_phases_need_no_drift_check(self):
        with temp_home():
            run_id = runs.new_run_id()
            runs.write_run(run_id, {"run_id": run_id, "plan_id": PLAN["plan_id"], "status": "running",
                                    "pid": None, "started_at": runs.now()})
            with patch.object(runner, "remote_shell", FakeShell()), patch.object(runner, "check_drift") as drift:
                runner.execute_plan(PLAN, run_id, RECORDS)
        self.assertEqual(drift.call_count, 1)

    def test_drift_before_a_later_phase_stops_without_applying_it(self):
        drift = [None, None, AgentError(TARGET_DRIFT, "Disk /dev/sda does not match the approved plan.")]
        with temp_home():
            shell = FakeShell({"authentication-check": 1, "kernel-check": 1})
            _, result, record = self.run_plan(shell, drift=drift)
        labels = [call[0] for call in shell.calls]
        self.assertIn("authentication", labels)
        self.assertNotIn("kernel", labels)
        self.assertEqual(result["error"]["code"], TARGET_DRIFT)
        self.assertEqual(record["completed_phases"], ["preflight", "authentication"])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_execute_plan -v`
Expected: the first and third new tests FAIL (only one drift call today).

- [ ] **Step 3: Implement**

`gentoo_agent/core/runner.py`, in the loop between the skip branch and `emit(f"===== applying {current} =====")`:

```python
            if phase.get("destructive", True):
                emit(f"{current}: re-checking that the target disk still matches the plan")
                check_drift(target, plan["disks"])
```

Update the module docstring sentence about drift to: "after confirming the live disk still matches the plan, and again before every destructive apply."

- [ ] **Step 4: Run the tests and the full suite**

Run: `.venv/bin/python -m unittest tests.test_execute_plan -v && .venv/bin/python -m unittest discover -s tests`
Expected: OK. Tests that count `remote_shell` calls are unaffected, because `check_drift` is patched separately.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core/runner.py tests/test_execute_plan.py
git commit -m "Re-check the disk identity before every destructive apply

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: HOST_KEY_UNKNOWN for host key failures

With `StrictHostKeyChecking=yes`, an unknown or changed host key makes ssh exit 255 with `Host key verification failed.` on stderr. Today that is reported as `SSH_UNREACHABLE`, which sends the operator to check the network. The first real run hits this case, so give it its own code (already reserved in the spec).

**Files:**
- Modify: `gentoo_agent/core/errors.py`, `gentoo_agent/core/ssh.py`, `gentoo_agent/core/inventory.py`, `gentoo_agent/core/disks.py` (`read_live_identity`)
- Test: `tests/test_state_inventory.py`, `tests/test_disks.py`, `tests/test_errors.py` (if it lists all codes)

- [ ] **Step 1: Write the failing tests**

Append to the inventory test class in `tests/test_state_inventory.py` (it already patches `inventory.run_ssh_readonly`; reuse its `TARGET`):

```python
    def test_host_key_failure_is_reported_as_host_key_unknown(self):
        def refused(target, label, command):
            return {"label": label, "command": command, "returncode": 255, "stdout": "",
                    "stderr": "No ED25519 host key is known for 192.0.2.10 and you have requested strict checking.\r\n"
                              "Host key verification failed.\r\n"}

        with temp_home(), patch.object(inventory, "run_ssh_readonly", refused):
            with self.assertRaises(AgentError) as caught:
                inventory.collect_inventory(TARGET)
            self.assertEqual(list(paths.inventory_dir().glob("*.json")), [])
        self.assertEqual(caught.exception.code, "HOST_KEY_UNKNOWN")
        self.assertEqual(caught.exception.details["host"], TARGET["host"])
```

(import `AgentError` and `paths` at the top if the file does not already.)

Append to `tests/test_disks.py` in the class that tests `read_live_identity` / `check_drift`:

```python
    def test_host_key_failure_during_drift_check(self):
        refused = {"label": "disk_identity", "command": "", "returncode": 255, "stdout": "",
                   "stderr": "Host key verification failed.\n"}
        with patch.object(disks, "run_ssh_readonly", return_value=refused):
            with self.assertRaises(AgentError) as caught:
                disks.read_live_identity({"host": "192.0.2.10", "user": "root"}, "/dev/sda")
        self.assertEqual(caught.exception.code, "HOST_KEY_UNKNOWN")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_state_inventory tests.test_disks -v`
Expected: FAIL with `SSH_UNREACHABLE` instead of `HOST_KEY_UNKNOWN`.

- [ ] **Step 3: Implement**

`gentoo_agent/core/errors.py`, after `SSH_UNREACHABLE`:

```python
HOST_KEY_UNKNOWN = "HOST_KEY_UNKNOWN"
```

`gentoo_agent/core/ssh.py`, after `ssh_base`:

```python
HOST_KEY_FAILURE = "Host key verification failed"
HOST_KEY_MESSAGE = ("The target's SSH host key is unknown or has changed. Verify its fingerprint on the target "
                    "console, then add it to known_hosts (see docs/guides/cli-install.md).")


def host_key_failed(result: dict) -> bool:
    """True when ssh refused the connection because of the host key (strict checking)."""
    return result.get("returncode") == 255 and HOST_KEY_FAILURE in str(result.get("stderr") or "")
```

`gentoo_agent/core/inventory.py`: import `HOST_KEY_UNKNOWN` and `HOST_KEY_MESSAGE, host_key_failed`; inside the `if all(_unreachable(item) ...)` block, before the existing raise:

```python
        if any(host_key_failed(item) for item in results):
            raise AgentError(HOST_KEY_UNKNOWN, HOST_KEY_MESSAGE, {"host": target["host"]})
```

`gentoo_agent/core/disks.py`, `read_live_identity`, before the existing `if result["returncode"] != 0:` raise:

```python
    if host_key_failed(result):
        raise AgentError(HOST_KEY_UNKNOWN, HOST_KEY_MESSAGE, {"host": target["host"]})
```

(with the matching imports).

If `tests/test_errors.py` asserts the full set of codes, add `HOST_KEY_UNKNOWN` there.

- [ ] **Step 4: Run the tests and the full suite**

Run: `.venv/bin/python -m unittest tests.test_state_inventory tests.test_disks tests.test_errors -v && .venv/bin/python -m unittest discover -s tests`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add gentoo_agent/core tests
git commit -m "Report host key failures as HOST_KEY_UNKNOWN

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Worker liveness through a per-run flock, and reaped worker processes

Liveness is decided by probing the worker pid, which can be reused by an unrelated process after the worker dies. The worker now holds an exclusive `flock` on `runs/<run_id>/worker.lock` for its whole life; the kernel drops it when the process exits, however it exits. The pid probe stays as the fallback for the short window before the worker takes the lock and for old runs without the file. The long-lived MCP server also never waits on its `Popen` children; a daemon thread now reaps each one.

**Files:**
- Modify: `gentoo_agent/core/runs.py`, `gentoo_agent/jobs/worker.py` (`run`), `gentoo_agent/jobs/launcher.py` (`start_run`)
- Test: `tests/test_runs.py`, `tests/test_worker.py`, `tests/test_launcher.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runs.py` (add `import subprocess`, `import sys` at the top if missing):

```python
def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class WorkerLockTests(unittest.TestCase):
    def start(self, pid: int) -> str:
        run_id = runs.new_run_id()
        runs.acquire_lock(run_id, {"run_id": run_id, "plan_id": "a" * 16, "status": "running", "pid": pid,
                                   "started_at": runs.now()})
        return run_id

    def test_held_worker_lock_keeps_the_run_active_even_if_the_pid_is_gone(self):
        with temp_home():
            run_id = self.start(dead_pid())
            handle = runs.hold_worker_lock(run_id)
            try:
                self.assertEqual(runs.run_status(run_id)["status"], "running")
                with self.assertRaises(AgentError) as caught:
                    runs.acquire_lock(runs.new_run_id(), {"status": "starting", "pid": None, "started_at": runs.now()})
                self.assertEqual(caught.exception.code, "RUN_ALREADY_ACTIVE")
            finally:
                handle.close()

    def test_released_worker_lock_ends_the_run_even_if_the_pid_is_alive(self):
        with temp_home():
            run_id = self.start(os.getpid())
            runs.hold_worker_lock(run_id).close()
            self.assertEqual(runs.run_status(run_id)["status"], "interrupted")

    def test_a_second_worker_cannot_hold_the_same_run(self):
        with temp_home():
            run_id = self.start(os.getpid())
            handle = runs.hold_worker_lock(run_id)
            try:
                with self.assertRaises(AgentError) as caught:
                    runs.hold_worker_lock(run_id)
                self.assertEqual(caught.exception.code, "RUN_ALREADY_ACTIVE")
            finally:
                handle.close()

    def test_without_a_lock_file_the_pid_probe_decides(self):
        with temp_home():
            run_id = self.start(dead_pid())
            self.assertIsNone(runs.worker_lock_held(run_id))
            self.assertEqual(runs.run_status(run_id)["status"], "interrupted")
```

(`os`, `AgentError`, and `temp_home` are likely imported already; add them if not.)

Append to `WorkerTests` in `tests/test_worker.py` (it uses `approved_plan()` and `self.new_run(plan)`):

```python
    def test_worker_holds_its_lock_while_executing(self):
        seen = {}

        def fake_execute(plan, run_id, records, phase_ids=None):
            seen["held"] = runs.worker_lock_held(run_id)
            return {"status": "installed_pending_reboot", "authentication_applied": True}

        with temp_home():
            plan = approved_plan()
            run_id = self.new_run(plan)
            with patch.object(worker, "execute_plan", side_effect=fake_execute):
                worker.run(run_id, {"password_records": "root:x\n", "generated_credentials": {}})
            held_after = runs.worker_lock_held(run_id)
        self.assertIs(seen["held"], True)
        self.assertIs(held_after, False)
```

(`worker_lock_held` opens a second file description, so it sees the worker's lock even in the same process.)

In `tests/test_launcher.py`, give `FakeProcess` a `wait` method and assert the reaper calls it:

```python
    def wait(self, timeout=None):
        self.waited = True
        return 0
```

and in the test that starts a run with `FakeProcess`:

```python
        deadline = time.monotonic() + 5
        while not getattr(process, "waited", False) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(process.waited)
```

(`process` is whatever name that test uses for its `FakeProcess` instance; add `import time`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_runs.WorkerLockTests tests.test_worker tests.test_launcher -v`
Expected: FAIL with `AttributeError: ... has no attribute 'hold_worker_lock'` and the reaper assertion.

- [ ] **Step 3: Implement the lock in runs.py**

`gentoo_agent/core/runs.py`, after `pid_alive`:

```python
def worker_lock_path(run_id: str) -> Path:
    return run_dir(run_id) / "worker.lock"


def hold_worker_lock(run_id: str):
    """Take the run's worker lock and return the open file; the lock lasts until it is closed or the process exits."""
    if fcntl is None:
        raise AgentError(CONFIG_INVALID, "Background runs need macOS or Linux; on Windows use WSL.", {})
    path = worker_lock_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise AgentError(RUN_ALREADY_ACTIVE, f"Another worker already holds run {run_id}.", {"run_id": run_id}) from None
    return handle


def worker_lock_held(run_id: str) -> bool | None:
    """True while a worker holds the run's lock, False after it let go, None if no worker ever took it."""
    if fcntl is None:
        return None
    path = worker_lock_path(run_id)
    try:
        handle = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return None
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
```

In `_record_active`, replace the final `return pid_alive(pid)` with:

```python
    run_id = record.get("run_id")
    held = worker_lock_held(run_id) if isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id) else None
    if held is None:
        return pid_alive(pid)
    if not held:
        pid_alive(pid)  # reaps the worker if it was our child
    return held
```

Update the module docstring: "A worker holds runs/<run_id>/worker.lock (flock) for its whole life; liveness is read from that lock, with a pid probe only before the worker takes it."

- [ ] **Step 4: Take the lock in the worker**

`gentoo_agent/jobs/worker.py`, import `hold_worker_lock`, and in `run()` change the start of the outer `try` and the `finally`:

```python
    worker_lock = None
    try:
        try:
            worker_lock = hold_worker_lock(run_id)
            record = claim_run(run_id, os.getpid())
        except AgentError as error:
            # Not our run, or no longer startable: record nothing on it.
            return {"status": "failed", "error": error.to_dict(), "authentication_applied": False}
        ...  # unchanged
    finally:
        release_lock(run_id)
        if worker_lock is not None:
            worker_lock.close()
```

The lock is taken before `claim_run`, so a run is never `running` without a held lock.

- [ ] **Step 5: Reap the worker in the launcher**

`gentoo_agent/jobs/launcher.py`, add `import threading`, and after `process.stdin.close()` (still inside the `try`):

```python
        # Wait for the worker in the background so a long-lived front end (the MCP server) leaves no zombies.
        threading.Thread(target=process.wait, name=f"reap-{run_id}", daemon=True).start()
```

`Popen.wait` copes with a child that `pid_alive` already reaped (it treats `ECHILD` as exited).

- [ ] **Step 6: Run the tests and the full suite**

Run: `.venv/bin/python -m unittest tests.test_runs tests.test_worker tests.test_launcher -v && .venv/bin/python -m unittest discover -s tests`
Expected: OK. If an existing launcher test uses a fake process without `wait`, add the same `wait` method to it.

- [ ] **Step 7: Commit**

```bash
git add gentoo_agent/core/runs.py gentoo_agent/jobs tests
git commit -m "Decide worker liveness from a per-run flock and reap worker processes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Stdio smoke test of the MCP server

The MCP tests use the SDK's in-memory client, so nothing proves that `python -m gentoo_agent.mcp.server` starts as a real process and keeps stdout clean for the protocol. Add one test that launches it over stdio, initializes, lists tools, and calls a read-only tool.

**Files:**
- Create: `tests/test_mcp_stdio.py`

- [ ] **Step 1: Write the test**

```python
"""The MCP server as a real stdio process: starts, keeps stdout clean, answers."""

import importlib.util
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

from tests.helpers import temp_home

HAS_MCP = importlib.util.find_spec("mcp") is not None
REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(HAS_MCP, "needs the [mcp] extra")
class StdioSmokeTests(unittest.TestCase):
    def test_server_starts_over_stdio_and_answers(self):
        import anyio
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        from tests.test_mcp_server import EXPECTED_TOOLS

        with temp_home() as home:
            (home / "config").mkdir()
            shutil.copy(REPO / "config" / "plan.json", home / "config" / "plan.json")
            params = StdioServerParameters(
                command=sys.executable,
                args=["-P", "-m", "gentoo_agent.mcp.server"],
                env={**os.environ, "GENTOO_AGENT_HOME": str(home), "PYTHONPATH": str(REPO)},
                cwd=str(home),
            )

            async def go():
                with anyio.fail_after(30), open(os.devnull, "w", encoding="utf-8") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            phases = await session.call_tool("list_phases", {})
                            return tools, phases

            tools, phases = anyio.run(go)
        self.assertEqual({tool.name for tool in tools.tools}, EXPECTED_TOOLS)
        self.assertFalse(phases.is_error)
        self.assertIn("preflight", json.dumps(phases.structured_content))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m unittest tests.test_mcp_stdio -v`
Expected: PASS within a few seconds. If an attribute name differs in `mcp==2.2.0` (for example `isError` versus `is_error`), check it with `.venv/bin/python -c "from mcp.types import CallToolResult; print(CallToolResult.model_fields.keys())"` and use the field the SDK defines. If the server process fails to start, rerun with `errlog=sys.stderr` to see why, and fix the cause rather than the test.

Run: `python3 -m unittest tests.test_mcp_stdio -v` (system Python without the extra)
Expected: `skipped 'needs the [mcp] extra'`.

- [ ] **Step 3: Prove the test catches stdout pollution**

Temporarily add `print("hello")` as the first line of `main()` in `gentoo_agent/mcp/server.py`, run the test, and confirm it fails or times out. Remove the line again and confirm `git diff gentoo_agent/mcp/server.py` is empty.

- [ ] **Step 4: Run the full suite and commit**

Run: `.venv/bin/python -m unittest discover -s tests`
Expected: OK.

```bash
git add tests/test_mcp_stdio.py
git commit -m "Add a stdio smoke test of the MCP server process

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Documentation

Bring the docs in line with Tasks 1 to 9. No em dashes, no emoji. Verify every command and field you write against the code.

**Files:**
- Modify: `docs/CONFIGURATION.md`, `docs/TROUBLESHOOTING.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `docs/guides/cli-install.md`, `docs/guides/ai-install.md`, `docs/superpowers/specs/2026-10-15-mcp-server-design.md`, `CLAUDE.md`

- [ ] **Step 1: Configuration reference**

`docs/CONFIGURATION.md`:
- `init_system`: only `openrc`; systemd is refused by validation, `init_config.py`, and `prepare_install.py`.
- `network_static`: four required keys `interface`, `address`, `gateway`, `dns`; interface rules (letters, digits, `_`, `.`, `-`, at most 15 characters, first character a letter or digit); where to find the name (the `network` section of the inventory, `ip -brief address` on the live system); the block is written to `/etc/dhcpcd.conf` between `# gentoo-agent static network begin` and `# gentoo-agent static network end`. Remove the paragraph about `ens33` and `dhcpcd.conf.d`.
- Plan creation requirements: inventory younger than 24 hours, the `validate` review passing, and `expected_disk_model` / `expected_disk_size_gib` (within 1 GiB) matching the inventory when set.

- [ ] **Step 2: Troubleshooting**

`docs/TROUBLESHOOTING.md`:
- New entry `HOST_KEY_UNKNOWN`: cause (ssh exit 255 with "Host key verification failed"), fix (read the fingerprint on the console with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`, compare, add the key with `ssh-keyscan` to the configured known_hosts file only after the fingerprints match). Update the count of error codes in the intro if it states one.
- `INVENTORY_MISSING`: add the stale or undated inventory case.
- `CONFIG_INVALID`: add "The configuration does not pass review" and the expected disk mismatches.
- `TARGET_DRIFT`: now also raised before any destructive phase, mid-run.

- [ ] **Step 3: Architecture and roadmap**

`docs/ARCHITECTURE.md`: drift check before every destructive apply; the worker flock (`runs/<run_id>/worker.lock`) and the pid fallback; plan creation runs the review; the error code table gains `HOST_KEY_UNKNOWN`.

`docs/ROADMAP.md`: move the fixed items (static networking, `set -e`, systemd consistency, prepare authorization, plan-time review, per-phase drift, worker liveness, MCP smoke test, `HOST_KEY_UNKNOWN`) to "Done". Keep as known limitations: cancel cannot interrupt a running phase or the drift read; `PHASE_PRECONDITION_FAILED` reserved; MCP SDK argument errors are unstructured; Plan 4 hardware.

- [ ] **Step 4: Guides, spec, and CLAUDE.md**

- `docs/guides/cli-install.md`: remove the manual step that sets the disposable flags in `target.json` after `prepare_install.py`; mention `HOST_KEY_UNKNOWN` where the host key is added; mention the interface prompt for static networking.
- `docs/guides/ai-install.md`: if it shows a static `network_static` example, add `interface`.
- `docs/superpowers/specs/2026-10-15-mcp-server-design.md`: the "Reserved for later" line now lists only `PHASE_PRECONDITION_FAILED`.
- `CLAUDE.md`: in the `core/resolve.py` bullet replace "although `validated_settings()` still accepts it for the golden fixtures" with "and `validated_settings()` refuses it as well"; in the `core/runs.py` bullet add "Workers hold `runs/<run_id>/worker.lock` (flock) for their lifetime; liveness reads that lock"; in the `core/runner.py` bullet say the drift check also runs before each destructive apply; in the `core/install_plan.py` bullet say plan creation also runs `validate_installation()`, refuses inventories older than 24 hours, and checks the expected disk model and size.

- [ ] **Step 5: Check links and style**

Run: `grep -rn "—" README.md CONTRIBUTING.md CLAUDE.md docs --include=*.md | grep -v docs/background/ | grep -v docs/superpowers/plans/`
Expected: no output.

Run: `grep -rn "ens33\|dhcpcd.conf.d\|systemd" README.md docs/CONFIGURATION.md docs/guides docs/TROUBLESHOOTING.md docs/ARCHITECTURE.md`
Expected: only sentences that say systemd is not supported.

Check that every relative Markdown link in the edited files resolves (a short Python script over `](...)` targets, with anchors checked against headings, as in the documentation overhaul).

- [ ] **Step 6: Commit**

```bash
git add README.md CLAUDE.md docs
git commit -m "Document the hardening changes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Out of scope (kept as known limitations)

- Cancel during a running phase or during the drift read (needs a cancellable SSH session; separate design).
- `PHASE_PRECONDITION_FAILED` (no phase has explicit preconditions yet).
- Structured errors for MCP SDK argument validation (SDK limitation).
- Plan 4: hardware profile and tailored kernel.
