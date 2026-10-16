"""The only module that talks to the target, over OpenSSH."""

from __future__ import annotations

import os
import queue
import shlex
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
) -> dict:
    """Run one generated shell program and return an auditable result.

    keep_fds are inherited by the ssh process (pass_fds). The worker passes its
    worker-lock descriptor, so the flock stays held until ssh exits even if the
    worker itself is killed while the remote program runs."""
    remote_command = "sh -c " + shlex.quote(command)
    if stream:
        process = subprocess.Popen(
            ssh_base(target) + ["--", remote_command],
            cwd=paths.root(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if input_data is not None else None,
            text=True,
            bufsize=1,
            pass_fds=keep_fds,
        )
        if input_data is not None:
            assert process.stdin is not None
            process.stdin.write(input_data)
            process.stdin.close()
        output: list[str] = []
        lines: queue.Queue[str] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            process.stdout.close()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            while reader.is_alive() or not lines.empty():
                if time.monotonic() - started > timeout:
                    process.kill()
                    raise subprocess.TimeoutExpired(remote_command, timeout)
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                output.append(line)
                if on_output is not None:
                    on_output(line)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
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
    completed = subprocess.run(
        ssh_base(target) + ["--", remote_command],
        cwd=paths.root(),
        capture_output=True,
        text=True,
        input=input_data,
        timeout=timeout,
        check=False,
        pass_fds=keep_fds,
    )
    return {
        "label": label,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "finished_at": now(),
    }
