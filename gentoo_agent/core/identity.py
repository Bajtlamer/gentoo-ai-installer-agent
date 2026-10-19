"""The identity of a running system: what a provision plan is bound to.

A plan stores the SSH host key fingerprint, the UUID of the root filesystem, and the hostname. The runner reads them
again before the run and before every phase that changes the system, so a plan cannot run on another
server and a reinstalled server (new host key and a new root filesystem UUID) invalidates it. Every value comes from
the target and is untrusted: it must match a strict format.
"""

from __future__ import annotations

import re
import subprocess

from gentoo_agent.core.errors import HOST_KEY_UNKNOWN, INVENTORY_MISSING, SSH_UNREACHABLE, TARGET_DRIFT, AgentError
from gentoo_agent.core.ssh import HOST_KEY_MESSAGE, PROVISION_READ_ONLY_COMMANDS, host_key_failed, run_ssh_readonly


IDENTITY_LABEL = "host_identity"
HOSTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
ROOT_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HOSTKEY = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


def parse_identity(stdout: object) -> dict:
    """hostname, root_uuid, and hostkey (the SHA256 fingerprint) from the host_identity result."""
    unusable = AgentError(INVENTORY_MISSING, "The host identity result is missing or malformed; collect it again.",
                          {"label": IDENTITY_LABEL})
    if not isinstance(stdout, str):
        raise unusable
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("hostname", "root_uuid", "hostkey"):
            values[key] = value.strip()
    fields = values.get("hostkey", "").split()
    found = {"hostname": values.get("hostname", ""), "root_uuid": values.get("root_uuid", ""),
             "hostkey": fields[1] if len(fields) >= 2 else ""}
    if not (HOSTNAME.fullmatch(found["hostname"]) and ROOT_UUID.fullmatch(found["root_uuid"])
            and HOSTKEY.fullmatch(found["hostkey"])):
        raise unusable
    return found


def read_live_identity(target: dict) -> dict:
    try:
        result = run_ssh_readonly(target, IDENTITY_LABEL, PROVISION_READ_ONLY_COMMANDS[IDENTITY_LABEL])
    except subprocess.TimeoutExpired as exc:
        raise AgentError(SSH_UNREACHABLE, f"Timed out reading the host identity from {target['host']}",
                         {"host": target["host"]}) from exc
    except OSError as exc:
        raise AgentError(SSH_UNREACHABLE, f"Could not run ssh to read the host identity from {target['host']}: {exc}",
                         {"host": target["host"]}) from exc
    if host_key_failed(result):
        raise AgentError(HOST_KEY_UNKNOWN, HOST_KEY_MESSAGE, {"host": target["host"]})
    if result["returncode"] != 0:
        raise AgentError(SSH_UNREACHABLE,
                         f"Could not read the host identity from {target['host']}: {result['stderr'].strip()}",
                         {"host": target["host"], "returncode": result["returncode"]})
    return parse_identity(result["stdout"])


def check_identity(target: dict, expected: dict) -> None:
    actual = read_live_identity(target)
    if actual != expected:
        raise AgentError(TARGET_DRIFT, "The target does not match the approved plan: it is another host or was reinstalled.",
                         {"expected": expected, "actual": actual})
