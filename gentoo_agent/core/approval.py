"""Operator approval of the reviewed configuration (approve-install).

Plan 2 replaces this with plan_hash-bound approval records.
"""

from __future__ import annotations

from gentoo_agent.core.config import save_json
from gentoo_agent.core.paths import CONFIG


def approval_phrase(target: dict, install: dict) -> str:
    return f"APPROVE INSTALL {target['name']} ON {install['disk']}"


def enable_execution(target: dict, install: dict, plan: dict) -> None:
    install["review_status"] = "approved_for_execution"
    install["execution_enabled"] = True
    target["execution_enabled"] = True
    plan["execution_enabled"] = True
    plan["review_status"] = "approved_for_execution"
    save_json(CONFIG / "install.json", install)
    save_json(CONFIG / "target.json", target)
    save_json(CONFIG / "plan.json", plan)
