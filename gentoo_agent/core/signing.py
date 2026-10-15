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
import secrets
from pathlib import Path

from gentoo_agent.core import paths
from gentoo_agent.core.errors import APPROVAL_REQUIRED, AgentError
from gentoo_agent.core.install_plan import canonical


KEY_FILE = "approval.key"
KEY_LENGTH = 64
HEX_DIGITS = set("0123456789abcdef")


def key_path() -> Path:
    return paths.secrets_dir() / KEY_FILE


def _read(path: Path) -> bytes:
    try:
        value = path.read_text(encoding="ascii").strip()
    except UnicodeDecodeError:
        value = ""
    if len(value) != KEY_LENGTH or not set(value) <= HEX_DIGITS:
        raise AgentError(
            APPROVAL_REQUIRED,
            "The approval signing key is malformed; remove secrets/approval.key and approve again.",
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
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read(path)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(secrets.token_hex(KEY_LENGTH // 2))
    return _read(path)


def sign(fields: dict, key: bytes) -> str:
    return hmac.new(key, canonical(fields), hashlib.sha256).hexdigest()


def verify(fields: dict, signature: object, key: bytes) -> bool:
    return isinstance(signature, str) and hmac.compare_digest(sign(fields, key), signature)
