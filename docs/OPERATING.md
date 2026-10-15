# Operating the MCP server in protected mode

## Why a protected mode exists

The MCP server enforces approvals, so the agent must not be able to change the
server's code, its data, or the signing key. In development mode (server run
from this repository) the agent can edit `gentoo_agent/` and the barrier does
not hold.

## Install a protected copy

```bash
python3 -m venv ~/.local/share/gentoo-agent/venv
~/.local/share/gentoo-agent/venv/bin/pip install 'gentoo-agent[mcp] @ git+https://github.com/Bajtlamer/gentoo-ai-installer-agent@<tag>'
```

Create a work directory (for example `~/gentoo-agent-work`) containing
`config/plan.json`, `config/install.default.json` (copied from the repository),
and `config/target.json`.

## Register the server

Copy `.mcp.json.example` to `.mcp.json` in the work directory (or use
`claude mcp add`) and set the absolute venv command and `GENTOO_AGENT_HOME`.

## Apply the sandbox profile

Run Claude Code from the work directory with `docs/operator/claude-settings.json`
as user settings (`~/.claude/settings.json`) or, so it cannot be overridden, as
managed settings. Also deny writes to the venv: add its absolute path to
`sandbox.filesystem.denyWrite` and an `Edit(//ABSOLUTE/PATH/venv/**)` rule.
Requires macOS (Seatbelt) or Linux/WSL2 (bubblewrap); native Windows is not
supported.

In operator mode the agent can read but not change `config/` and `inventory/`.
A hostile inventory or `plan.json` could otherwise influence what the approval
dialog shows. The operator therefore edits `config/target.json` and runs
`inspect` themselves, or uses `detect_hardware` through the MCP server, which
runs outside the sandbox.

## What the barrier covers and what it does not

The agent's shell and file tools cannot read the signing key or write
approvals, plans, runs, or secrets, so it cannot create a valid approval.
Approvals require the human to accept the server's dialog or run
`gentoo-agent approve` in their own terminal.

Not covered: a user-configured Elicitation hook that answers dialogs
automatically, commands the human runs with `!`, and settings the human
loosens. Managed settings lock the sandbox keys.

## Approval dialogs

Approval dialogs show values escaped on one line. The "Disk erase" and
"Destructive phases" lines always come first, so read them before accepting.

If the SSH key changes after a plan is created, `verify_install` refuses with
`PLAN_HASH_MISMATCH` and the plan must be recreated.

## Credentials

Generated passwords are never sent to the model. Read them with
`GENTOO_AGENT_HOME=<work dir> gentoo-agent run status <run_id>` in a terminal.
