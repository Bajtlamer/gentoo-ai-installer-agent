"""Shared fixtures for provision plan tests. The values are samples, not claims about real hardware."""

from gentoo_agent.core.config import now

HOSTKEY = "SHA256:" + "A" * 43
ROOT_UUID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
PROVISION_TARGET = {"name": "gentoo-server2", "host": "192.0.2.10", "port": 22, "user": "root",
                    "identity_file": "~/.ssh/id_test", "known_hosts_file": ""}

# Sample values for tests; none of them is a claim about real hardware (device ids, host key, thread count, kernel).
GPU_SYSFS = (
    "0000:00:00.0 0x060000 0x1022 0x14d8\n"
    "0000:01:00.0 0x030000 0x10de 0x2f04\n"
    "0000:01:00.1 0x040300 0x10de 0x22eb\n"
)
AMD_GPU_SYSFS = "0000:03:00.0 0x030000 0x1002 0x744c\n"
INTEL_GPU_SYSFS = "0000:00:02.0 0x030000 0x8086 0x46a6\n"

# Real output of the inventory commands on the first real server (Gentoo, OpenRC), collected 2026-10-19.
REAL_CPU_STDOUT = (
    'threads=20\n'
    'Architecture:                            x86_64\n'
    'CPU op-mode(s):                          32-bit, 64-bit\n'
    'Address sizes:                           46 bits physical, 48 bits virtual\n'
    'Byte Order:                              Little Endian\n'
    'CPU(s):                                  20\n'
    'On-line CPU(s) list:                     0-19\n'
    'Vendor ID:                               GenuineIntel\n'
    'Model name:                              13th Gen Intel(R) Core(TM) i5-13500\n'
    'CPU family:                              6\n'
    'Model:                                   191\n'
    'Thread(s) per core:                      2\n'
    'Core(s) per socket:                      14\n'
    'Socket(s):                               1\n'
    'Stepping:                                2\n'
    'Microcode version:                       0x3e\n'
    'CPU(s) scaling MHz:                      20%\n'
    'CPU max MHz:                             4800.0000\n'
    'CPU min MHz:                             800.0000\n'
    'BogoMIPS:                                4993.00\n'
    'Flags:                                   fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush dts acpi mmx fxsr sse sse2 ss ht tm pbe syscall nx pdpe1gb rdtscp lm constant_tsc art arch_perfmon pebs bts rep_good nopl xtopology nonstop_tsc cpuid aperfmperf tsc_known_freq pni pclmulqdq dtes64 monitor ds_cpl vmx smx est tm2 ssse3 sdbg fma cx16 xtpr pdcm pcid sse4_1 sse4_2 x2apic movbe popcnt tsc_deadline_timer aes xsave avx f16c rdrand lahf_lm abm 3dnowprefetch cpuid_fault ssbd ibrs ibpb stibp ibrs_enhanced tpr_shadow flexpriority ept vpid ept_ad fsgsbase tsc_adjust bmi1 avx2 smep bmi2 erms invpcid rdseed adx smap clflushopt clwb intel_pt sha_ni xsaveopt xsavec xgetbv1 xsaves split_lock_detect user_shstk avx_vnni dtherm ida arat pln pts hwp hwp_notify hwp_act_window hwp_epp hwp_pkg_req hfi vnmi umip pku ospke waitpkg gfni vaes vpclmulqdq rdpid movdiri movdir64b fsrm md_clear serialize pconfig arch_lbr ibt flush_l1d arch_capabilities\n'
    'Virtualization:                          VT-x\n'
    'L1d cache:                               544 KiB (14 instances)\n'
    'L1i cache:                               704 KiB (14 instances)\n'
    'L2 cache:                                11.5 MiB (8 instances)\n'
    'L3 cache:                                24 MiB (1 instance)\n'
    'NUMA node(s):                            1\n'
    'NUMA node0 CPU(s):                       0-19\n'
    'Vulnerability Gather data sampling:      Not affected\n'
    'Vulnerability Ghostwrite:                Not affected\n'
    'Vulnerability Indirect target selection: Not affected\n'
    'Vulnerability Itlb multihit:             Not affected\n'
    'Vulnerability L1tf:                      Not affected\n'
    'Vulnerability Mds:                       Not affected\n'
    'Vulnerability Meltdown:                  Not affected\n'
    'Vulnerability Mmio stale data:           Not affected\n'
    'Vulnerability Old microcode:             Not affected\n'
    'Vulnerability Reg file data sampling:    Mitigation; Clear Register File\n'
    'Vulnerability Retbleed:                  Not affected\n'
    'Vulnerability Spec rstack overflow:      Not affected\n'
    'Vulnerability Spec store bypass:         Mitigation; Speculative Store Bypass disabled via prctl\n'
    'Vulnerability Spectre v1:                Mitigation; usercopy/swapgs barriers and __user pointer sanitization\n'
    'Vulnerability Spectre v2:                Mitigation; Enhanced / Automatic IBRS; IBPB conditional; PBRSB-eIBRS SW sequence; BHI BHI_DIS_S\n'
    'Vulnerability Srbds:                     Not affected\n'
    'Vulnerability Tsa:                       Not affected\n'
    'Vulnerability Tsx async abort:           Not affected\n'
    'Vulnerability Vmscape:                   Mitigation; IBPB before exit to userspace\n'
)

REAL_MEMORY_STDOUT = (
    'MemTotal:       32598200 kB\n'
)

REAL_GPU_STDOUT = (
    '0000:00:00.0 0x060000 0x8086 0x4640\n'
    '0000:00:01.0 0x060400 0x8086 0x460d\n'
    '0000:00:02.0 0x038000 0x8086 0x4680\n'
    '0000:00:06.0 0x060400 0x8086 0x464d\n'
    '0000:00:14.0 0x0c0330 0x8086 0x7a60\n'
    '0000:00:14.2 0x050000 0x8086 0x7a27\n'
    '0000:00:14.3 0x028000 0x8086 0x7a70\n'
    '0000:00:15.0 0x0c8000 0x8086 0x7a4c\n'
    '0000:00:15.1 0x0c8000 0x8086 0x7a4d\n'
    '0000:00:15.2 0x0c8000 0x8086 0x7a4e\n'
    '0000:00:15.3 0x0c8000 0x8086 0x7a4f\n'
    '0000:00:16.0 0x078000 0x8086 0x7a68\n'
    '0000:00:17.0 0x010601 0x8086 0x7a62\n'
    '0000:00:19.0 0x0c8000 0x8086 0x7a7c\n'
    '0000:00:19.1 0x0c8000 0x8086 0x7a7d\n'
    '0000:00:1a.0 0x060400 0x8086 0x7a48\n'
    '0000:00:1c.0 0x060400 0x8086 0x7a38\n'
    '0000:00:1c.2 0x060400 0x8086 0x7a3a\n'
    '0000:00:1c.4 0x060400 0x8086 0x7a3c\n'
    '0000:00:1f.0 0x060100 0x8086 0x7a06\n'
    '0000:00:1f.3 0x040300 0x8086 0x7a50\n'
    '0000:00:1f.4 0x0c0500 0x8086 0x7a23\n'
    '0000:00:1f.5 0x0c8000 0x8086 0x7a24\n'
    '0000:01:00.0 0x030000 0x10de 0x2c05\n'
    '0000:01:00.1 0x040300 0x10de 0x22e9\n'
    '0000:02:00.0 0x010802 0x144d 0xa80c\n'
    '0000:03:00.0 0x010802 0x144d 0xa80c\n'
    '0000:05:00.0 0x020000 0x10ec 0x8125\n'
    '0000:06:00.0 0x020000 0x8086 0x10d3\n'
)

REAL_KERNEL_STDOUT = (
    '6.18.57-gentoo-dist-bin\n'
)

# The host_identity output that exposed the defect: /etc/machine-id does not exist on an OpenRC system.
REAL_BROKEN_IDENTITY_STDOUT = (
    'hostname=gentoo\n'
    'machine_id=hostkey=256 SHA256:UxiiAX2HWERC/ClCqsP0XE95WhO5KV99UB6HmMKksWY root@gentoo (ED25519)\n'
)


def result(label: str, stdout: str, returncode: int = 0) -> dict:
    return {"label": label, "command": "", "returncode": returncode, "stdout": stdout, "stderr": ""}


def host_identity_result(hostname: str = "gentoo", root_uuid: str = ROOT_UUID, hostkey: str = HOSTKEY) -> dict:
    return result("host_identity",
                  f"hostname={hostname}\nroot_uuid={root_uuid}\nhostkey=256 {hostkey} root@{hostname} (ED25519)\n")


def cpu_result(threads: int = 24) -> dict:
    return result("cpu", f"threads={threads}\nArchitecture:        x86_64\nModel name:          Example CPU 12-Core Processor\n")


def memory_result(kib: int = 32768000) -> dict:
    return result("memory", f"MemTotal:       {kib} kB\nSwapTotal:      16777212 kB\n")


def gpu_result(text: str = GPU_SYSFS) -> dict:
    return result("gpu", text)


def portage_conf_result(text: str = "") -> dict:
    return result("portage_conf", text)


def kernel_result(release: str = "6.18.57-gentoo-dist-bin") -> dict:
    return result("kernel", release + "\n")


def arch_result(machine: str = "x86_64") -> dict:
    return result("arch", machine + "\n")


def portage_profile_result(name: str = "default/linux/amd64/23.0") -> dict:
    return result("portage_profile", f"Current /etc/portage/make.profile symlink:\n  {name}\n")


def portage_profiles_result() -> dict:
    return result("portage_profiles",
                  "Available profile symlink targets:\n"
                  "  [1]   default/linux/amd64/23.0 (stable) *\n"
                  "  [2]   default/linux/amd64/23.0/desktop (stable)\n"
                  "  [3]   default/linux/amd64/23.0/desktop/plasma (stable)\n")


def binrepos_result(text: str = "[gentoobinhost]\npriority = 1\nsync-uri = https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64/\n") -> dict:
    return result("binrepos", text)


def provision_inventory(**replace: dict) -> dict:
    """A provision inventory collected now. replace maps a label to the result that replaces the default."""
    results = {
        "host_identity": host_identity_result(), "cpu": cpu_result(), "memory": memory_result(),
        "gpu": gpu_result(), "portage_conf": portage_conf_result(), "kernel": kernel_result(),
    }
    results.update(replace)
    return {"target": "gentoo-server2", "collected_at": now(), "results": list(results.values())}


def provision_settings(**replace) -> dict:
    settings = {"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2"]}
    settings.update(replace)
    return settings


def save_provision_plan(settings: dict | None = None, **inventory_replace: dict) -> dict:
    """Create and save a provision plan. Call inside tests.helpers.temp_home(). inventory_replace maps a label to the result that replaces the default."""
    from gentoo_agent.core.install_plan import save_install_plan
    from gentoo_agent.core.provision_plan import create_provision_plan

    return save_install_plan(
        create_provision_plan(PROVISION_TARGET, settings or provision_settings(), provision_inventory(**inventory_replace)))
