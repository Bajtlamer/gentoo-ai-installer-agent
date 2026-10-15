"""HMAC signatures for approval records.

The key lives in secrets/approval.key (directory 0700, file 0600). Only the
human's CLI and the MCP server write approval records, and both run outside
the agent's sandbox; the operator profile denies the agent's shell and file
tools any access to secrets/. Verification never creates a key.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

from gentoo_agent.core import paths
from gentoo_agent.core.errors import APPROVAL_REQUIRED, AgentError
from gentoo_agent.core.install_plan import canonical


KEY_FILE = "approval.key"
KEY_LENGTH = 64
HEX_DIGITS = set("0123456789abcdef")
SIGNATURE_PATTERN = re.compile(r"[0-9a-f]{64}")


def key_path() -> Path:
    return paths.secrets_dir() / KEY_FILE


def _unsafe() -> AgentError:
    return AgentError(
        APPROVAL_REQUIRED,
        "The approval signing key must be a private regular file owned by you; "
        "remove secrets/approval.key and approve again.",
        {},
    )


def _read(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise
    except OSError:
        raise _unsafe() from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise _unsafe()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise _unsafe()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            try:
                value = handle.read(KEY_LENGTH + 16).decode("ascii").strip()
            except (OSError, UnicodeDecodeError):
                value = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(value) != KEY_LENGTH or not set(value) <= HEX_DIGITS:
        raise AgentError(
            APPROVAL_REQUIRED,
            "The approval signing key is malformed; remove secrets/approval.key and approve again "
            "(existing approvals become invalid).",
            {},
        )
    return value.encode("ascii")


def load_key() -> bytes | None:
    try:
        return _read(key_path())
    except FileNotFoundError:
        return None


def load_or_create_key() -> bytes:
    existing = load_key()
    if existing is not None:
        return existing
    path = key_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".approval-", dir=path.parent)
    except OSError:
        raise _unsafe() from None
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(secrets.token_hex(KEY_LENGTH // 2))
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError:
            raise _unsafe() from None
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return _read(path)


def sign(fields: dict, key: bytes) -> str:
    return hmac.new(key, canonical(fields), hashlib.sha256).hexdigest()


def verify(fields: dict, signature: object, key: bytes) -> bool:
    if not isinstance(signature, str) or not SIGNATURE_PATTERN.fullmatch(signature):
        return False
    try:
        expected = sign(fields, key)
    except (UnicodeEncodeError, ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, signature)
