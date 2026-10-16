# Operating the MCP server in protected mode

## When do I need this

You need protected mode when the approval must be a real barrier between the
AI agent and the destructive installation, not a convention the agent is
asked to follow.

The MCP server enforces approvals, so the agent must not be able to change the
server's code, its data, or the signing key. In development mode (server run
from this repository, as in [Installing with an AI client](ai-install.md)) the
agent can edit `gentoo_agent/` and the barrier does not hold.

Use development mode to try the flow on a disposable VM. Use protected mode
for every installation where a misbehaving agent approving its own plan would
be a problem.

Requirements: macOS (Seatbelt) or Linux/WSL2 (bubblewrap) for the Claude Code
sandbox. Native Windows is not supported.

## Setup

1. Install a protected copy of the server in its own virtual environment,
   outside any directory the agent can write:

   ```bash
   python3 -m venv ~/.local/share/gentoo-agent/venv
   ~/.local/share/gentoo-agent/venv/bin/pip install 'gentoo-agent[mcp] @ git+https://github.com/Bajtlamer/gentoo-ai-installer-agent@<tag>'
   ```

2. Create a work directory, for example `~/gentoo-agent-work`, containing:
   - `config/plan.json` and `config/install.default.json`, copied from the
     repository;
   - `config/target.json`, written by you (see the
     [configuration reference](../CONFIGURATION.md)).

3. Register the server in the work directory, as described in
   [Step 3 of the AI guide](ai-install.md#step-3-register-the-server): copy
   `.mcp.json.example` to `.mcp.json` (or use `claude mcp add`), set
   `command` to the absolute path of `gentoo-agent-mcp` in the protected venv,
   and set `GENTOO_AGENT_HOME` to the absolute path of the work directory.

4. Apply the operator profile. Run Claude Code from the work directory with
   `docs/operator/claude-settings.json` as user settings
   (`~/.claude/settings.json`) or, so it cannot be overridden, as managed
   settings.

5. Deny writes to the venv as well: add its absolute path to
   `sandbox.filesystem.denyWrite` and add an `Edit(//ABSOLUTE/PATH/venv/**)`
   rule to `permissions.deny`.

6. Prepare the target yourself. In protected mode the agent can read but not
   change `config/` and `inventory/`, because a hostile inventory or
   `plan.json` could otherwise influence what the approval dialog shows.
   Edit `config/target.json` and run `inspect` yourself, or let the agent use
   `detect_hardware`, which runs inside the MCP server, outside the sandbox.

7. Start Claude Code in the work directory and follow
   [Installing with an AI client](ai-install.md) from the
   [example conversation](ai-install.md#example-conversation) onward.

## What the profile does

`docs/operator/claude-settings.json` enables the sandbox with
`failIfUnavailable: true` and `allowUnsandboxedCommands: false`, and:

- denies the agent's shell writes to `./approvals`, `./plans`, `./runs`,
  `./secrets`, `./config`, `./inventory`, `./.mcp.json`, and `./.claude`;
- denies the agent's shell reads of `./secrets` and `./runs`;
- denies the file tools reads of `secrets/` and of run credentials
  (`runs/*/credentials.json`, `runs/*/.credentials.*`);
- denies the file tools edits of `approvals/`, `plans/`, `runs/`, `secrets/`,
  `config/`, `inventory/`, `.mcp.json`, and `.claude/`;
- denies running `gentoo-agent approve`, `python -m gentoo_agent approve`, and
  `python3 -m gentoo_agent approve`.

The agent's shell cannot read `runs/` at all: it reads run status and logs
through the `get_run_status` and `get_run_log` tools instead. The agent also
cannot change `.mcp.json` or `.claude/`, so it cannot point the server entry at
other code or loosen the project settings.

## What the barrier covers and what it does not

The agent's shell and file tools cannot read the signing key or write
approvals, plans, runs, secrets, `config/`, `inventory/`, `.mcp.json`, or
`.claude/`, so it cannot create a valid approval. The shell cannot read
`runs/`, and the file tools cannot read generated credentials.
Approvals require the human to accept the server's dialog or run
`gentoo-agent approve` in their own terminal.

Not covered: a user-configured Elicitation hook that answers dialogs
automatically, commands the human runs with `!`, and settings the human
loosens. Managed settings lock the sandbox keys.

## Approval dialogs

Approval dialogs show values escaped on one line. The "Disk erase" and
"Destructive phases" lines always come first, so read them before accepting.
[The approval dialog](ai-install.md#the-approval-dialog) lists every line.

If the SSH key changes after a plan is created, `verify_install` refuses with
`PLAN_HASH_MISMATCH` and the plan must be recreated.

## Credentials

Generated passwords are never sent to the model. Read them in your own
terminal, not through the agent, with the venv's `gentoo-agent` and
`GENTOO_AGENT_HOME` set to the work directory, for example with `<venv>` as
`~/.local/share/gentoo-agent/venv`. The command and the meaning of
`credentials_waiting` and `credentials_applied` are in
[Reading generated passwords](ai-install.md#reading-generated-passwords).

## See also

- [Installing with an AI client](ai-install.md)
- [Installing with the CLI](cli-install.md)
- [Troubleshooting](../TROUBLESHOOTING.md)
