"""chpasswd records for the authentication phase.

Passwords never appear in generated commands, logs, or configuration. The
runner sends these records to the target over SSH stdin.
"""

from __future__ import annotations

import os
import secrets
from typing import Callable

from gentoo_agent.core.errors import CONFIG_INVALID, AgentError


PasswordPrompt = Callable[[str], str]


def _checked(value: str, label: str, source: str) -> str:
    if not value or "\r" in value or "\n" in value:
        raise AgentError(CONFIG_INVALID, f"Password from {source} is empty or contains a newline", {"label": label})
    return value


def password_input(install: dict, prompt: PasswordPrompt | None = None) -> tuple[str | None, dict[str, str]]:
    password_source = install.get("password_source", "environment")
    generated: dict[str, str] = {}

    def obtain(label: str, env_field: str) -> str:
        if password_source == "environment":
            env_name = str(install.get(env_field, ""))
            value = os.environ.get(env_name)
            if value is None:
                if not install.get("generate_missing_passwords", True):
                    raise AgentError(
                        CONFIG_INVALID,
                        f"Missing required password environment variable: {env_name}",
                        {"variable": env_name},
                    )
                value = secrets.token_urlsafe(24)
                generated[label] = value
            return _checked(value, label, env_name)
        if password_source == "generate":
            value = secrets.token_urlsafe(24)
            generated[label] = value
            return value
        if prompt is None:
            raise AgentError(CONFIG_INVALID, "password_source 'prompt' needs an interactive front end", {"label": label})
        return _checked(prompt(label), label, "prompt")

    records: list[str] = []
    if install.get("set_root_password", True):
        records.append(f"root:{obtain('root', 'root_password_env')}")
    if install.get("create_user", True):
        username = install.get("username", "gentoo")
        records.append(f"{username}:{obtain(str(username), 'user_password_env')}")
    return ("\n".join(records) + "\n" if records else None), generated
