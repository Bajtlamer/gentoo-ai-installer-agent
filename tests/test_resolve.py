import json
import unittest
from pathlib import Path

from gentoo_agent.core.errors import (
    CONFIG_CONFLICT,
    CONFIG_INVALID,
    DISK_AMBIGUOUS,
    INVENTORY_MISSING,
    AgentError,
)
from gentoo_agent.core.resolve import SETTABLE, resolve_config
from tests.helpers import (
    BASIC_DATA_TYPE,
    ESP_TYPE,
    identity_result,
    partition,
    partitions_result,
    windows_bios_disk,
    windows_disk,
)


REPO = Path(__file__).resolve().parents[1]
TARGET = {"name": "gentoo", "host": "192.0.2.10", "port": 22, "user": "root",
          "identity_file": "~/.ssh/id_test", "target_is_disposable": True, "disk_erase_authorized": True}
DEFAULTS = json.loads((REPO / "config" / "install.default.json").read_text(encoding="utf-8"))


def inventory(boot: str = "uefi", disks: tuple[str, ...] = ("/dev/nvme0n1",), extra: dict | None = None,
              storage: dict | None = None, machine: str = "x86_64", partitions: dict | None = None) -> dict:
    """extra maps a disk path to additional lsblk fields (rm, tran, ro, size);
    partitions replaces the default partitions result (every disk empty)."""
    devices = [{"path": path, "type": "disk", "size": 512110190592, "model": "NVMe", "serial": f"S{index}", "wwn": None,
                **(extra or {}).get(path, {})}
               for index, path in enumerate(disks)]
    results = [
        {"label": "boot_mode", "returncode": 0, "stdout": boot if boot == "uefi" else "bios-or-unknown"},
        {"label": "disk_identity", "returncode": 0, "stdout": json.dumps({"blockdevices": devices})},
        identity_result(machine),
        partitions or partitions_result(*disks),
    ]
    if storage is not None:
        results.append({"label": "storage_json", "returncode": 0, "stdout": json.dumps(storage)})
    return {"results": results}


def mounted(path: str, mountpoint: str) -> dict:
    return {"blockdevices": [{"name": path[5:], "path": path, "type": "disk", "mountpoints": [None], "children": [
        {"name": path[5:] + "1", "path": path + "1", "type": "part", "mountpoints": [mountpoint]},
    ]}]}


def resolve(overrides=None, fallbacks=None, **kwargs):
    return resolve_config(TARGET, overrides or {}, fallbacks or {}, DEFAULTS, kwargs.get("inv", inventory()),
                          profile=kwargs.get("profile"))


class ResolveTests(unittest.TestCase):
    def test_spec_example(self):
        result = resolve(
            {"username": "Alex", "user_password": {"env": "ALEX_PW"}, "boot_mode": "uefi", "bootloader": "grub"},
            {"username": "gentoo", "hostname": "server", "timezone": "Europe/Prague"},
        )
        install, sources = result["install"], result["sources"]
        self.assertEqual(install["username"], "alex")
        self.assertIn("username Alex was lowercased to alex", result["warnings"])
        self.assertEqual(install["user_password_env"], "ALEX_PW")
        self.assertEqual(install["password_source"], "environment")
        self.assertEqual(install["hostname"], "server")
        self.assertEqual(sources["hostname"], "fallback")
        self.assertEqual(sources["username"], "override")
        self.assertEqual(sources["user_password_env"], "override")
        self.assertEqual(install["disk"], "/dev/nvme0n1")
        self.assertEqual(sources["disk"], "inferred")
        self.assertEqual(install["partition_table"], "gpt")
        self.assertEqual(install["partition_layout"][0]["device"], "/dev/nvme0n1p1")
        self.assertTrue(install["disk_must_be_erased"])
        self.assertNotIn("user_password", install)
        self.assertNotIn("user_password", sources)

    def test_precedence(self):
        result = resolve({"hostname": "a"}, {"hostname": "c"}, profile={"hostname": "b"})
        self.assertEqual(result["install"]["hostname"], "a")
        result = resolve({}, {"hostname": "c"}, profile={"hostname": "b"})
        self.assertEqual((result["install"]["hostname"], result["sources"]["hostname"]), ("b", "profile"))
        result = resolve({}, {})
        self.assertEqual(result["sources"]["hostname"], "default")

    def test_plain_text_password_is_rejected(self):
        for value in ("secret", {"env": "lowercase"}, {"value": "x"}, {"env": "OK", "extra": 1}):
            with self.subTest(value=value), self.assertRaises(AgentError) as caught:
                resolve({"user_password": value})
            self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_passwords_only_in_overrides_or_profile(self):
        with self.assertRaises(AgentError) as caught:
            resolve({}, {"root_password": {"env": "ROOT_PW"}})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"partition_layout": []})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(caught.exception.details["keys"], ["partition_layout"])

    def test_requested_boot_mode_must_match_the_live_system(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"boot_mode": "uefi"}, inv=inventory(boot="bios"))
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)
        self.assertEqual(caught.exception.details, {"requested": "uefi", "live": "bios"})

    def test_boot_mode_is_inferred_from_the_live_system(self):
        result = resolve({}, inv=inventory(boot="bios", disks=("/dev/sda",)))
        self.assertEqual(result["install"]["boot_mode"], "bios")
        self.assertEqual(result["sources"]["boot_mode"], "inferred")
        self.assertEqual(result["install"]["partition_table"], "mbr")

    def test_several_disks_need_a_choice(self):
        with self.assertRaises(AgentError) as caught:
            resolve({}, inv=inventory(disks=("/dev/sda", "/dev/sdb")))
        self.assertEqual(caught.exception.code, DISK_AMBIGUOUS)
        self.assertEqual(caught.exception.details["candidates"], ["/dev/sda", "/dev/sdb"])
        result = resolve({"disk": "/dev/sdb"}, inv=inventory(disks=("/dev/sda", "/dev/sdb")))
        self.assertEqual(result["install"]["disk"], "/dev/sdb")

    def test_missing_inventory(self):
        with self.assertRaises(AgentError) as caught:
            resolve_config(TARGET, {}, {}, DEFAULTS, None)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_unsupported_bootloader(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"bootloader": "systemd-boot"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_only_openrc_is_supported(self):
        for layer in ("overrides", "profile"):
            with self.subTest(layer=layer), self.assertRaises(AgentError) as caught:
                if layer == "overrides":
                    resolve({"init_system": "systemd"})
                else:
                    resolve(profile={"init_system": "systemd"})
            self.assertEqual(caught.exception.code, CONFIG_INVALID)
            self.assertEqual(caught.exception.message,
                             "Only OpenRC is supported in this version (init_system must be openrc).")
            self.assertEqual(caught.exception.details, {"field": "init_system"})
        with self.assertRaises(AgentError) as caught:
            resolve_config(TARGET, {}, {}, {**DEFAULTS, "init_system": "systemd"}, inventory())
        self.assertEqual(caught.exception.details, {"field": "init_system"})
        self.assertEqual(resolve({"init_system": "openrc"})["install"]["init_system"], "openrc")

    def test_erase_flags_come_from_the_target(self):
        result = resolve_config({**TARGET, "disk_erase_authorized": False}, {}, {}, DEFAULTS, inventory())
        self.assertFalse(result["install"]["disk_erase_authorized"])

    def test_wrong_types_are_rejected_per_layer(self):
        cases = {
            "username": 5, "hostname": ["h"], "timezone": {"a": 1}, "boot_mode": True, "init_system": 1,
            "network_method": None, "disk": ["/dev/sda"], "bootloader": {}, "swap_mb": "2048",
            "create_user": "yes", "configure_sudo": 1, "set_root_password": None,
            "allow_root_password_ssh": "false", "install_user_ssh_key": [], "user_groups": "wheel",
            "network_static": "10.0.0.2/24", "architecture": 64,
        }
        for key, value in cases.items():
            for layer in ("overrides", "profile"):
                with self.subTest(key=key, layer=layer):
                    with self.assertRaises(AgentError) as caught:
                        if layer == "overrides":
                            resolve({key: value})
                        else:
                            resolve(profile={key: value})
                    self.assertEqual(caught.exception.code, CONFIG_INVALID)
                    self.assertEqual(caught.exception.details, {"layer": layer, "field": key})
        for key, value in (("swap_mb", True), ("user_groups", ["wheel", 5]), ("swap_mb", 1.5)):
            with self.subTest(key=key, value=value):
                with self.assertRaises(AgentError) as caught:
                    resolve({key: value})
                self.assertEqual(caught.exception.details, {"layer": "overrides", "field": key})

    def test_malformed_inventory_is_missing(self):
        for value in ([], "x", 5, {"results": "x"}, {"results": {}}, {"results": [5, None]}, {}):
            with self.subTest(value=value):
                with self.assertRaises(AgentError) as caught:
                    resolve({}, inv=value)
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)
        good = inventory()["results"]
        for stdout in ("[]", "5", "null", '{"blockdevices": 5}', '{"blockdevices": [5]}',
                       '{"blockdevices": [{"type": "disk", "path": "/dev/sda", "size": "big"}]}', 5, None):
            with self.subTest(stdout=stdout):
                broken = {"results": [good[0], {"label": "disk_identity", "returncode": 0, "stdout": stdout}]}
                with self.assertRaises(AgentError) as caught:
                    resolve({}, inv=broken)
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_hostile_values_raise_only_agent_errors(self):
        hostile = [None, [], {}, 5, -1, 10**30, True, 1.5, "x" * 10000, "a\nb", "", "../..",
                   {"a": {"b": {"c": []}}}, [{"a": 1}], ["wheel", None], {"env": "ALEX_PW"},
                   {"address": "10.0.0.2/24 x", "gateway": [], "dns": {}}]
        keys = sorted(SETTABLE) + ["user_password", "root_password", "unknown_key"]
        for key in keys:
            for value in hostile:
                for layer in ("overrides", "profile", "fallbacks"):
                    with self.subTest(key=key, value=repr(value)[:40], layer=layer):
                        try:
                            if layer == "overrides":
                                resolve({key: value})
                            elif layer == "profile":
                                resolve(profile={key: value})
                            else:
                                resolve({}, {key: value})
                        except AgentError:
                            pass
        for layer_value in (None, [], 5, "x", {"a": None}):
            with self.subTest(layer=repr(layer_value)):
                try:
                    resolve_config(TARGET, layer_value, layer_value, DEFAULTS, inventory(), profile=layer_value)
                except AgentError:
                    pass

    def test_architecture_is_inferred_from_the_live_system(self):
        result = resolve_config(TARGET, {}, {}, DEFAULTS, inventory(machine="aarch64"))
        self.assertEqual(result["install"]["architecture"], "arm64")
        self.assertEqual(result["sources"]["architecture"], "inferred")
        self.assertEqual(result["install"]["profile"], "gentoo-arm64-openrc")

    def test_architecture_override_must_match_the_live_system(self):
        with self.assertRaises(AgentError) as caught:
            resolve_config(TARGET, {"architecture": "arm64"}, {}, DEFAULTS, inventory(machine="x86_64"))
        self.assertEqual(caught.exception.code, CONFIG_CONFLICT)

    def test_architecture_cannot_be_a_fallback(self):
        with self.assertRaises(AgentError) as caught:
            resolve_config(TARGET, {}, {"architecture": "amd64"}, DEFAULTS, inventory())
        self.assertEqual(caught.exception.details, {"layer": "fallbacks", "field": "architecture"})

    def test_fallbacks_cannot_choose_disk_or_boot_mode(self):
        for key, value in (("disk", "/dev/nvme0n1"), ("boot_mode", "uefi")):
            with self.subTest(key=key):
                with self.assertRaises(AgentError) as caught:
                    resolve({}, {key: value})
                self.assertEqual(caught.exception.code, CONFIG_INVALID)
                self.assertEqual(caught.exception.details.get("field"), key)

    def test_boot_mode_requires_evidence(self):
        disk = inventory()["results"][1]
        for boot in (None, {"label": "boot_mode", "returncode": 1, "stdout": "uefi"},
                     {"label": "boot_mode", "returncode": 0, "stdout": ""},
                     {"label": "boot_mode", "returncode": 0, "stdout": "efi"},
                     {"label": "boot_mode", "returncode": 0, "stdout": 5},
                     {"label": "boot_mode", "stdout": "uefi"}):
            with self.subTest(boot=boot):
                results = [disk] if boot is None else [boot, disk]
                with self.assertRaises(AgentError) as caught:
                    resolve({}, inv={"results": results})
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)
                self.assertEqual(caught.exception.message, "Boot mode is unknown; refresh the inventory.")
                with self.assertRaises(AgentError) as caught:
                    resolve({"boot_mode": "bios"}, inv={"results": results})
                self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_inferred_disk_warns_and_lists_exclusions(self):
        inv = inventory(disks=("/dev/nvme0n1", "/dev/sdb", "/dev/sdc", "/dev/sdd", "/dev/sde"),
                        extra={"/dev/sdb": {"tran": "usb"}, "/dev/sdc": {"rm": True},
                               "/dev/sdd": {"ro": "1"}},
                        storage=mounted("/dev/sde", "/mnt/data"))
        result = resolve({}, inv=inv)
        self.assertEqual(result["install"]["disk"], "/dev/nvme0n1")
        self.assertEqual(result["sources"]["disk"], "inferred")
        warning = next(w for w in result["warnings"] if w.startswith("disk /dev/nvme0n1 was inferred"))
        self.assertTrue(warning.startswith("disk /dev/nvme0n1 was inferred as the only eligible disk; excluded: "))
        for path in ("/dev/sdb", "/dev/sdc", "/dev/sdd", "/dev/sde"):
            self.assertIn(path, warning)
        self.assertIn("/mnt/data", warning)
        single = resolve({})
        self.assertIn("disk /dev/nvme0n1 was inferred as the only eligible disk; excluded: none", single["warnings"])

    def test_no_eligible_disk_is_ambiguous(self):
        inv = inventory(disks=("/dev/sda", "/dev/sdb"), extra={"/dev/sda": {"rm": 1}, "/dev/sdb": {"tran": "usb"}})
        with self.assertRaises(AgentError) as caught:
            resolve({}, inv=inv)
        self.assertEqual(caught.exception.code, DISK_AMBIGUOUS)
        self.assertEqual(caught.exception.details["candidates"], [])
        self.assertEqual([item["path"] for item in caught.exception.details["excluded"]], ["/dev/sda", "/dev/sdb"])
        self.assertTrue(all(item["reason"] for item in caught.exception.details["excluded"]))

    def test_explicit_disk_must_exist_and_be_eligible(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"disk": "/dev/sdz"})
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        inv = inventory(disks=("/dev/sda", "/dev/sdb"), extra={"/dev/sdb": {"tran": "usb"}})
        for kwargs in ({"overrides": {"disk": "/dev/sdb"}}, {"profile": {"disk": "/dev/sdb"}}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(AgentError) as caught:
                    resolve(kwargs.get("overrides"), inv=inv, profile=kwargs.get("profile"))
                self.assertEqual(caught.exception.code, CONFIG_CONFLICT)
                self.assertIn("usb", caught.exception.message)
        result = resolve({"disk": "/dev/sda"}, inv=inv)
        self.assertEqual(result["install"]["disk"], "/dev/sda")
        self.assertFalse(any("was inferred" in warning for warning in result["warnings"]))

    def test_unsafe_password_env_names_are_rejected(self):
        for name in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "TERM", "LANG", "TMPDIR",
                     "EDITOR", "GENTOO_AGENT_HOME", "SSH_AUTH_SOCK", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
                     "PYTHONPATH", "GIT_ASKPASS", "GITHUB_TOKEN", "MY_SECRET", "OPENAI_API_KEY", "X_API_KEY",
                     "SSH_PRIVATE", "PRIVATE_KEY", "AWS_SECRET_ACCESS_KEY", "MY_AWS_PW", "ANTHROPIC_KEY", "OPENAI_X",
                     "SHLVL", "HOSTNAME", "USERNAME", "LC_ALL", "TZ", "DISPLAY", "DATABASE_URL",
                     "GOOGLE_APPLICATION_CREDENTIALS", "PASSWORD", "SSH_PASSWORD", "GIT_PASSWORD", "OPENAI_PW",
                     "MY_SECRET_PASSWORD", "GITHUB_TOKEN_PASSWORD"):
            for field in ("user_password", "root_password"):
                with self.subTest(name=name, field=field):
                    with self.assertRaises(AgentError) as caught:
                        resolve({field: {"env": name}})
                    self.assertEqual(caught.exception.code, CONFIG_INVALID)
                    self.assertIn(
                        'Password variable names must end in _PASSWORD or _PW, for example {"env": "ALEX_PW"}.',
                        caught.exception.message,
                    )
        for name in ("ALEX_PW", "GENTOO_USER_PASSWORD", "GENTOO_ROOT_PASSWORD", "ROOT_PW"):
            with self.subTest(name=name):
                self.assertEqual(resolve({"user_password": {"env": name}})["install"]["user_password_env"], name)
                self.assertEqual(resolve(profile={"root_password": {"env": name}})["install"]["root_password_env"], name)

    def test_swap_must_be_below_half_the_disk(self):
        inv = inventory(extra={"/dev/nvme0n1": {"size": 8 * 1024 ** 3}})
        with self.assertRaises(AgentError) as caught:
            resolve({"swap_mb": 4096}, inv=inv)
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
        self.assertEqual(caught.exception.details.get("field"), "swap_mb")
        self.assertEqual(resolve({"swap_mb": 4095}, inv=inv)["install"]["swap_mb"], 4095)

    def test_zero_swap_has_no_swap_partition(self):
        layout = resolve({"swap_mb": 0})["install"]["partition_layout"]
        self.assertEqual([item["type"] for item in layout], ["efi-system", "linux-root"])


class DualBootResolveTests(unittest.TestCase):
    def two_disks(self, sda: dict | None = None) -> dict:
        return inventory(disks=("/dev/nvme0n1", "/dev/sda"),
                         partitions=partitions_result("/dev/nvme0n1", sda or windows_disk("/dev/sda")))

    def test_windows_on_another_disk_is_previewed_as_a_menu_entry(self):
        result = resolve({"disk": "/dev/nvme0n1"}, inv=self.two_disks())
        self.assertEqual(result["install"]["dual_boot"], "auto")
        self.assertEqual(result["sources"]["dual_boot"], "default")
        self.assertEqual(result["install"]["dual_boot_entries"], [
            {"os": "windows", "disk": "/dev/sda", "esp_uuid": "ABCD-1234",
             "loader": "/EFI/Microsoft/Boot/bootmgfw.efi"},
        ])
        self.assertEqual(result["sources"]["dual_boot_entries"], "derived")

    def test_dual_boot_off_previews_no_entries(self):
        result = resolve({"disk": "/dev/nvme0n1", "dual_boot": "off"}, inv=self.two_disks())
        self.assertEqual(result["install"]["dual_boot_entries"], [])
        self.assertEqual(result["sources"]["dual_boot"], "override")

    def test_bios_install_and_windows_in_bios_mode_preview_no_entries(self):
        bios = inventory(boot="bios", disks=("/dev/nvme0n1", "/dev/sda"),
                         partitions=partitions_result("/dev/nvme0n1", windows_disk("/dev/sda")))
        self.assertEqual(resolve({"disk": "/dev/nvme0n1"}, inv=bios)["install"]["dual_boot_entries"], [])
        legacy = self.two_disks(windows_bios_disk("/dev/sda"))
        self.assertEqual(resolve({"disk": "/dev/nvme0n1"}, inv=legacy)["install"]["dual_boot_entries"], [])

    def test_no_windows_previews_no_entries(self):
        self.assertEqual(resolve({})["install"]["dual_boot_entries"], [])

    def test_the_only_eligible_disk_is_not_inferred_when_it_holds_windows(self):
        for disk in (windows_disk("/dev/sda"), windows_bios_disk("/dev/sda")):
            inv = inventory(disks=("/dev/sda",), partitions=partitions_result(disk))
            with self.subTest(os=disk["children"][0]["parttype"]), self.assertRaises(AgentError) as caught:
                resolve({}, inv=inv)
            self.assertEqual(caught.exception.code, DISK_AMBIGUOUS)
            self.assertEqual(caught.exception.details["candidates"], ["/dev/sda"])
            self.assertIn("never chosen automatically", caught.exception.message)

    def test_the_only_eligible_disk_is_not_inferred_when_it_holds_windows_data(self):
        bitlocker = {"path": "/dev/sda", "type": "disk", "children": [
            partition("/dev/sda", 1, ESP_TYPE, "vfat", "1111-2222"), partition("/dev/sda", 2, BASIC_DATA_TYPE, "BitLocker")]}
        inv = inventory(disks=("/dev/sda",), partitions=partitions_result(bitlocker))
        with self.assertRaises(AgentError) as caught:
            resolve({}, inv=inv)
        self.assertEqual(caught.exception.code, DISK_AMBIGUOUS)
        self.assertEqual(caught.exception.details["existing_os"], {"/dev/sda": "windows-data"})
        self.assertIn("holds Windows data or boot files", caught.exception.message)

    def test_partitions_without_metadata_are_refused(self):
        bare = {"path": "/dev/nvme0n1", "type": "disk", "children": [partition("/dev/nvme0n1", 1, None)]}
        with self.assertRaises(AgentError) as caught:
            resolve({}, inv=inventory(partitions=partitions_result(bare)))
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)

    def test_a_windows_disk_named_explicitly_is_accepted_without_an_entry_for_itself(self):
        inv = inventory(disks=("/dev/sda",), partitions=partitions_result(windows_disk("/dev/sda")))
        result = resolve({"disk": "/dev/sda"}, inv=inv)
        self.assertEqual(result["install"]["disk"], "/dev/sda")
        self.assertEqual(result["install"]["dual_boot_entries"], [])

    def test_dual_boot_entries_cannot_be_set(self):
        for layer in ("overrides", "profile", "fallbacks"):
            with self.subTest(layer=layer), self.assertRaises(AgentError) as caught:
                value = [{"os": "windows", "disk": "/dev/sda", "esp_uuid": "ABCD-1234",
                          "loader": "/EFI/Microsoft/Boot/bootmgfw.efi"}]
                if layer == "overrides":
                    resolve({"dual_boot_entries": value})
                elif layer == "profile":
                    resolve(profile={"dual_boot_entries": value})
                else:
                    resolve({}, {"dual_boot_entries": value})
            self.assertEqual(caught.exception.code, CONFIG_INVALID)
            self.assertEqual(caught.exception.details["keys"], ["dual_boot_entries"])

    def test_dual_boot_must_be_auto_or_off(self):
        with self.assertRaises(AgentError) as caught:
            resolve({"dual_boot": "always"})
        self.assertEqual(caught.exception.details, {"field": "dual_boot"})

    def test_missing_partition_list_is_refused(self):
        inv = inventory()
        inv["results"] = [item for item in inv["results"] if item["label"] != "partitions"]
        with self.assertRaises(AgentError) as caught:
            resolve({}, inv=inv)
        self.assertEqual(caught.exception.code, INVENTORY_MISSING)


if __name__ == "__main__":
    unittest.main()
