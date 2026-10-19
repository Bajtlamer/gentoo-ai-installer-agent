"""The only module that talks to the target, over OpenSSH."""

from __future__ import annotations

import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from typing import Callable

from gentoo_agent.core.config import now
from gentoo_agent.core import paths


def ssh_base(target: dict) -> list[str]:
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    known_hosts = target.get("known_hosts_file", "")
    if known_hosts:
        command += ["-o", f"UserKnownHostsFile={os.path.expanduser(known_hosts)}"]
    if target.get("identity_file"):
        command += ["-i", os.path.expanduser(target["identity_file"])]
    if target.get("port"):
        command += ["-p", str(target["port"])]
    command.append(f"{target['user']}@{target['host']}")
    return command


SECURE_BOOT_VARIABLE = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"

READ_ONLY_COMMANDS = {
    "identity": "printf 'hostname='; hostname; printf '\\narch='; uname -m; printf '\\nkernel='; uname -r",
    "boot_mode": "if [ -d /sys/firmware/efi ]; then printf 'uefi'; else printf 'bios-or-unknown'; fi",
    "storage": "lsblk -e7 -o NAME,PATH,SIZE,TYPE,FSTYPE,FSVER,LABEL,UUID,MOUNTPOINTS,ROTA,MODEL",
    "storage_json": "lsblk -J -e7 -o NAME,PATH,SIZE,TYPE,FSTYPE,MODEL,MOUNTPOINTS",
    "disk_identity": "lsblk -J -b -d -e7 -o PATH,TYPE,SIZE,MODEL,SERIAL,WWN,RM,TRAN,RO",
    "block_ids": "blkid || true",
    "mounts": "findmnt -D || mount",
    "network": "ip -brief address; printf '\\n'; ip route",
    "errors": "dmesg --level=err,warn 2>/dev/null | tail -n 200 || true",
    # Partition type GUIDs and filesystems, read by core/foreign_os.py to find Windows.
    "partitions": "lsblk -J -b -e7 -o PATH,TYPE,PKNAME,PARTTYPE,PARTTYPENAME,FSTYPE,UUID,PARTUUID,SIZE,LABEL",
    # The last byte of the SecureBoot EFI variable (4 attribute bytes, then 1 data byte).
    "secure_boot": (
        f"f={SECURE_BOOT_VARIABLE}; "
        "if [ -r \"$f\" ]; then od -An -t u1 -j 4 -N 1 \"$f\" | tr -d ' \\n'; else printf unknown; fi"
    ),
}

# Read-only facts of a running, installed system, collected for provision plans (core/provision_plan.py).
PROVISION_READ_ONLY_COMMANDS = {
    "host_identity": (
        "printf 'hostname='; hostname; printf 'machine_id='; cat /etc/machine-id; "
        "printf 'hostkey='; ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub"
    ),
    "cpu": "printf 'threads='; nproc --all; lscpu",
    "memory": "grep -E '^(MemTotal|SwapTotal):' /proc/meminfo",
    # One line per PCI device: address, class, vendor, device (hex as sysfs prints it). No lspci: a stage3 has none.
    "gpu": (
        "for d in /sys/bus/pci/devices/*; do [ -r \"$d/class\" ] || continue; "
        "printf '%s %s %s %s\\n' \"${d##*/}\" \"$(cat \"$d/class\")\" \"$(cat \"$d/vendor\")\" \"$(cat \"$d/device\")\"; done"
    ),
    # The user's own MAKEOPTS and EMERGE_DEFAULT_OPTS, outside the block that a plan writes.
    "portage_conf": (
        "sed -n -e '/^# gentoo-agent portage-tuning begin$/,/^# gentoo-agent portage-tuning end$/d' "
        "-e '/^\\(MAKEOPTS\\|EMERGE_DEFAULT_OPTS\\)=/p' /etc/portage/make.conf 2>/dev/null || true"
    ),
    "kernel": "uname -r",
}

HOST_KEY_FAILURE = "Host key verification failed"
HOST_KEY_MESSAGE = ("The target's SSH host key is unknown or has changed. Verify its fingerprint on the target "
                    "console, then add it to known_hosts (see docs/guides/cli-install.md).")


def host_key_failed(result: dict) -> bool:
    """True when ssh refused the connection because of the host key (strict checking)."""
    return result.get("returncode") == 255 and HOST_KEY_FAILURE in str(result.get("stderr") or "")


def run_ssh_readonly(target: dict, label: str, command: str) -> dict:
    # OpenSSH joins all arguments after the host into one remote command. Quote
    # the shell program explicitly so pipes, semicolons, and conditionals are
    # interpreted on the target instead of being split by the SSH invocation.
    remote_command = "sh -c " + shlex.quote(command)
    completed = subprocess.run(
        ssh_base(target) + ["--", remote_command],
        cwd=paths.root(),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    return {
        "label": label,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


# Runs ssh as its child and exits the way ssh did. Started with the kept
# descriptors (pass_fds), it holds them until ssh exits: OpenSSH closes every
# inherited descriptor above stderr at startup, so it cannot hold them itself.
# subprocess.call closes them for ssh (close_fds) and passes stdin, stdout,
# and stderr through unchanged. If ssh cannot be started, it writes one line
# to stderr and exits 255, the code ssh itself uses for its own errors.
# The first line is a fixed marker so an operator can find a hanging holder
# with `pgrep -f gentoo-agent-ssh-holder` (docs/TROUBLESHOOTING.md).
HOLDER_MARKER = "gentoo-agent-ssh-holder"
HOLDER = (
    f"# {HOLDER_MARKER}\n"
    "import os, signal, subprocess, sys\n"
    "try:\n"
    "    code = subprocess.call(sys.argv[1:])\n"
    "except OSError as error:\n"
    "    sys.stderr.write(f'gentoo-agent holder: cannot run ssh: {error}\\n')\n"
    "    sys.exit(255)\n"
    "if code < 0:\n"
    "    signal.signal(-code, signal.SIG_DFL)\n"
    "    os.kill(os.getpid(), -code)\n"
    "sys.exit(code)\n"
)


def _kill(process: subprocess.Popen, group: bool) -> None:
    """Kill the process, or with group its whole process group (the holder, ssh, and its ProxyCommand)."""
    if not group:
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # The group is already gone. macOS reports EPERM instead of ESRCH when every
        # member has exited but the leader is not reaped yet; the leader is our
        # unreaped child, so its group id cannot belong to anyone else.
        pass


def remote_shell(
    target: dict,
    label: str,
    command: str,
    timeout: int = 3600,
    stream: bool = False,
    input_data: str | None = None,
    on_output: Callable[[str], None] | None = None,
    *,
    keep_fds: tuple[int, ...] = (),
    holder_python: str | None = None,
) -> dict:
    """Run one generated shell program and return an auditable result.

    With keep_fds, ssh runs under a holder process (HOLDER, run by
    holder_python with -I; the caller passes the interpreter because core/
    does not import sys) that
    inherits those descriptors and keeps them open until ssh exits. The worker
    passes its worker-lock descriptor, so the flock stays held while the remote
    program runs even if the worker itself is killed. The holder starts a new
    session, and a timeout or any other exception while it runs kills the whole
    process group, so no ssh is left behind. Without keep_fds ssh runs directly
    and such an exception kills ssh itself."""
    remote_command = "sh -c " + shlex.quote(command)
    argv = ssh_base(target) + ["--", remote_command]
    group = bool(keep_fds)
    extra: dict = {}
    if group:
        if not holder_python:
            raise ValueError("keep_fds requires holder_python, the interpreter that runs the holder")
        argv = [holder_python, "-I", "-c", HOLDER, *argv]
        extra = {"pass_fds": tuple(keep_fds), "start_new_session": True}
    if stream:
        process = subprocess.Popen(
            argv,
            cwd=paths.root(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if input_data is not None else None,
            text=True,
            bufsize=1,
            **extra,
        )
        output: list[str] = []
        lines: queue.Queue[str] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            process.stdout.close()

        started = time.monotonic()
        try:
            if input_data is not None:
                assert process.stdin is not None
                process.stdin.write(input_data)
                process.stdin.close()
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            while reader.is_alive() or not lines.empty():
                if time.monotonic() - started > timeout:
                    _kill(process, group)
                    raise subprocess.TimeoutExpired(remote_command, timeout)
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                output.append(line)
                if on_output is not None:
                    on_output(line)
        except BaseException:
            # A timeout, a failing on_output callback, or an interrupt: never leave ssh running.
            if process.poll() is None:
                _kill(process, group)
            process.wait()
            raise
        reader.join(timeout=1)
        returncode = process.wait(timeout=5)
        return {
            "label": label,
            "command": command,
            "returncode": returncode,
            "stdout": "".join(output),
            "stderr": "",
            "finished_at": now(),
        }
    if group:
        # subprocess.run would kill only the holder on a timeout; kill the group instead.
        with subprocess.Popen(
            argv,
            cwd=paths.root(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_data is not None else None,
            text=True,
            **extra,
        ) as process:
            try:
                stdout, stderr = process.communicate(input_data, timeout=timeout)
            except BaseException:
                _kill(process, group)
                process.communicate()
                raise
            returncode = process.returncode
        return {
            "label": label,
            "command": command,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "finished_at": now(),
        }
    completed = subprocess.run(
        argv,
        cwd=paths.root(),
        capture_output=True,
        text=True,
        input=input_data,
        timeout=timeout,
        check=False,
    )
    return {
        "label": label,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "finished_at": now(),
    }
