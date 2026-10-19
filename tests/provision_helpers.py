"""Shared fixtures for provision plan tests. The lspci text is a sample, not a claim about real device ids."""

from gentoo_agent.core.config import now

HOSTKEY = "SHA256:" + "A" * 43
MACHINE_ID = "0123456789abcdef0123456789abcdef"
PROVISION_TARGET = {"name": "gentoo-server2", "host": "192.0.2.10", "port": 22, "user": "root",
                    "identity_file": "~/.ssh/id_test", "known_hosts_file": ""}

LSPCI = (
    "01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GB205 [GeForce RTX 5070] [10de:2f04] (rev a1)\n"
    "\tSubsystem: Example Device [1043:89f3]\n"
    "\tKernel driver in use: nouveau\n"
    "\tKernel modules: nouveau\n"
    "01:00.1 Audio device [0403]: NVIDIA Corporation Device [10de:22eb] (rev a1)\n"
    "\tKernel driver in use: snd_hda_intel\n"
    "00:00.0 Host bridge [0600]: Example Corp Root Complex [1022:14d8]\n"
)


def result(label: str, stdout: str, returncode: int = 0) -> dict:
    return {"label": label, "command": "", "returncode": returncode, "stdout": stdout, "stderr": ""}


def host_identity_result(hostname: str = "gentoo", machine_id: str = MACHINE_ID, hostkey: str = HOSTKEY) -> dict:
    return result("host_identity",
                  f"hostname={hostname}\nmachine_id={machine_id}\nhostkey=256 {hostkey} root@{hostname} (ED25519)\n")


def cpu_result(threads: int = 24) -> dict:
    return result("cpu", f"threads={threads}\nArchitecture:        x86_64\nModel name:          Example CPU 12-Core Processor\n")


def memory_result(kib: int = 32768000) -> dict:
    return result("memory", f"MemTotal:       {kib} kB\nSwapTotal:      16777212 kB\n")


def gpu_result(text: str = LSPCI) -> dict:
    return result("gpu", text)


def portage_conf_result(text: str = "") -> dict:
    return result("portage_conf", text)


def kernel_result(release: str = "6.18.57-gentoo-dist-bin") -> dict:
    return result("kernel", release + "\n")


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


def save_provision_plan(**inventory_replace: dict) -> dict:
    """Create and save a provision plan. Call inside tests.helpers.temp_home()."""
    from gentoo_agent.core.install_plan import save_install_plan
    from gentoo_agent.core.provision_plan import create_provision_plan

    return save_install_plan(
        create_provision_plan(PROVISION_TARGET, provision_settings(), provision_inventory(**inventory_replace)))
