# Installing Gentoo with an AI client (MCP)

An AI client, such as Claude Code, can drive the installation through the
`gentoo-agent-mcp` server. The model reads the hardware, turns your request
into a configuration, creates a plan, and follows the run. It cannot approve
the plan: the approval is a dialog that only you answer, or a command that only
you run in your own terminal.

Read [Installing Gentoo with the CLI](cli-install.md) first. The target
preparation (live ISO, SSH key, host key verification) is the same, and the
MCP tools run the same code underneath.

## Contents

- [Development mode and protected mode](#development-mode-and-protected-mode)
- [Step 1: Install the MCP extra](#step-1-install-the-mcp-extra)
- [Step 2: Prepare the target configuration](#step-2-prepare-the-target-configuration)
- [Step 3: Register the server](#step-3-register-the-server)
- [Step 4: Provide passwords](#step-4-provide-passwords)
- [Client features you need](#client-features-you-need)
- [Tools, resources, and the prompt](#tools-resources-and-the-prompt)
- [Example conversation](#example-conversation)
- [The approval dialog](#the-approval-dialog)
- [Reading generated passwords](#reading-generated-passwords)
- [What the AI must never do](#what-the-ai-must-never-do)

## Development mode and protected mode

| Mode | Server runs from | Barrier |
|---|---|---|
| Development | This repository (`pip install -e '.[mcp]'`) | A convention only. The agent can edit `gentoo_agent/`, read `secrets/`, and write approvals, so a misbehaving agent could approve its own plan. |
| Protected | A separate virtual environment and work directory, with the operator profile | The agent's shell and file tools cannot read the signing key, write approvals, or change the server. |

Use development mode to try the flow on a disposable VM. For a real barrier
between the model and the destructive step, use
[protected mode](protected-mode.md).

## Step 1: Install the MCP extra

The server needs the optional `mcp` extra (it pins `mcp==2.2.0`). Install it in
a virtual environment in the repository:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[mcp]'
```

This provides two commands in `.venv/bin/`: `gentoo-agent` (the CLI) and
`gentoo-agent-mcp` (the stdio MCP server).

Supported targets: amd64 (BIOS or UEFI) and arm64 (UEFI only). The server
infers the architecture from the live system and refuses an `architecture`
override that differs from it.

## Step 2: Prepare the target configuration

The MCP tools read `config/target.json`, `config/plan.json`, and
`config/install.default.json`, plus the latest inventory. They build the
install configuration from your request, so `config/install.json` is not used.

1. Boot the live ISO, start SSH, install your key, and verify the host key, as
   in Steps 1 and 2 of the [CLI guide](cli-install.md#step-1-boot-the-target-and-start-ssh).
2. Create `config/target.json` with the live address, `root` as user, your
   dedicated key, and both disposable flags set to `true`:

   ```json
   "target_is_disposable": true,
   "disk_erase_authorized": true
   ```

   `python3 tools/init_config.py --host <live-ip> --disk /dev/sda
   --identity-file ~/.ssh/gentoo-live_ed25519 --allow-disk-erase` writes such a
   file (and an `install.json` that the MCP path ignores). Without both flags,
   `create_plan` refuses with `CONFIG_INVALID`.

## Step 3: Register the server

The server speaks MCP over stdio. Register it with your client in one of two
ways.

Copy `.mcp.json.example` to `.mcp.json` in the directory where you start the
client, and replace both placeholders with absolute paths:

```json
{
  "mcpServers": {
    "gentoo-agent": {
      "type": "stdio",
      "command": "/ABSOLUTE/PATH/TO/protected-venv/bin/gentoo-agent-mcp",
      "args": [],
      "env": {
        "GENTOO_AGENT_HOME": "/ABSOLUTE/PATH/TO/gentoo-agent-workdir"
      }
    }
  }
}
```

In development mode, `command` is `<repository>/.venv/bin/gentoo-agent-mcp`
and `GENTOO_AGENT_HOME` is the repository root (you may also omit it: the
server then uses the repository it was installed from).

Or register it with the Claude Code command line. Put the server name before
`-e`, because `-e` (`--env`) accepts several values and would otherwise take
the name as one of them:

```sh
claude mcp add gentoo-agent -e GENTOO_AGENT_HOME=/ABSOLUTE/PATH/TO/workdir -- /ABSOLUTE/PATH/TO/venv/bin/gentoo-agent-mcp
```

Run `claude mcp add --help` for the scope option (`-s local`, `user`, or
`project`) and the other flags of your Claude Code version.

Every file the server reads or writes (`config/`, `inventory/`, `plans/`,
`approvals/`, `runs/`, `secrets/`) lives under `GENTOO_AGENT_HOME`.

## Step 4: Provide passwords

You never type a password into the chat. The model refers to a password only by
the name of an environment variable, as `{"env": "NAME"}`. The name must end in
`_PASSWORD` or `_PW`, for example `ALEX_PW`. Anything else, including a
password written as text, is refused with `CONFIG_INVALID`.

The variable must be set in the environment of the MCP server process when
`start_install` (or `resume_run`) runs. The usual way is to export it in the
shell from which you start the client:

```sh
read -rs ALEX_PW && export ALEX_PW
claude
```

The root password defaults to the variable `GENTOO_ROOT_PASSWORD` from
`config/install.default.json`. A referenced variable that is not set is
replaced by a generated password, which you read later in your terminal (see
[Reading generated passwords](#reading-generated-passwords)).

Be aware that the client's own shell tool usually inherits the same
environment, so an exported variable may be readable by the agent's shell. If
the model must never be able to see a password, leave the variable unset and
let the installer generate it.

`password_source: "prompt"` cannot be used through MCP; `start_install`
refuses it with `CONFIG_INVALID`.

## Client features you need

| Feature | Why |
|---|---|
| stdio MCP servers | The server has no network transport. |
| Tool results with `structuredContent` | Every tool returns structured JSON; errors carry `{code, message, details}` and `isError`. |
| Elicitation | `approve_plan` shows the approval dialog through MCP elicitation. Without it, the tool records nothing and returns `APPROVAL_REQUIRED` with the command `gentoo-agent approve <plan_id>`, which you run in your own terminal. |

The destructive tools (`start_install`, `run_phase`, `resume_run`) are
annotated as destructive and carry `anthropic/requiresUserInteraction` in their
metadata, so a client can ask you before each call. Keep that confirmation
switched on.

## Tools, resources, and the prompt

The tools in workflow order:

| Tool | What it does |
|---|---|
| `detect_hardware(refresh)` | Summarizes the firmware boot mode (`uefi`, `bios`, or `unknown`), the architecture (`amd64`, `arm64`, or `unknown`), and the disks with serial and WWN. `refresh=true` first runs the read-only SSH inventory; otherwise the latest saved inventory is used. |
| `list_phases()` | Lists the runnable phases in order, with description, whether each changes the target, and its Handbook sections. |
| `resolve_config(overrides, fallbacks)` | Previews the merged install configuration (overrides, then inferred values, then fallbacks, then defaults) with the source of every value. Writes nothing. |
| `create_plan(overrides, fallbacks)` | Same inputs as `resolve_config`. Freezes the configuration, the disk identity, and every command into a plan; returns `plan_id`, `plan_hash`, and the summary. It refuses an inventory older than 24 hours, a configuration that fails the `validate` review, and a disk that does not match `expected_disk_model` or `expected_disk_size_gib`. |
| `get_plan(plan_id)` | Shows a stored plan, its summary, and its approval status. |
| `approve_plan(plan_id)` | Asks you, in a dialog built by the server, to approve the plan for 60 minutes. The model cannot answer it. |
| `start_install(plan_id)` | Starts the approved plan in the background. Erases the target disk. Returns `run_id`. |
| `run_phase(plan_id, phases)` | Runs only the listed phases of an approved plan, in plan order, in the background. |
| `get_run_status(run_id)` | Run status, phase index, current phase, and last error. Reports `credentials_waiting` and `credentials_applied`, never passwords. |
| `get_run_log(run_id, offset, limit)` | Reads part of a run's log; offset and limit count log lines. |
| `cancel_run(run_id)` | Stops a run at the next phase boundary. |
| `resume_run(run_id)` | Starts a new run of the same approved plan; completed phases are checked and skipped. |
| `verify_install(plan_id)` | Runs each phase's read-only check from the stored plan over SSH and reports which phases are complete. |

The settable keys for `overrides` and `fallbacks`, and the rules for each, are
listed in the [configuration reference](../CONFIGURATION.md).
In short: `overrides` may also hold `user_password` and `root_password` as
`{"env": "NAME"}`; `fallbacks` may not set `disk`, `boot_mode`, or passwords;
only `grub` and `openrc` are accepted.

Resources:

| URI | Content |
|---|---|
| `gentoo://plan/{plan_id}` | The same view of a plan as `get_plan`. |
| `gentoo://run/{run_id}/state` | The run record. |
| `gentoo://run/{run_id}/log` | The first 500 log lines. |

The prompt `install_gentoo(request)` gives the model the workflow in seven
steps: refresh the hardware and ask which disk if several are listed; translate
the request into overrides and fallbacks with passwords only as
`{"env": "NAME"}`; preview with `resolve_config` and report conflicts instead
of guessing; `create_plan` and then `approve_plan`, never approving any other
way; `start_install` and poll status and log until the run ends; on failure
report the error code and output tail and resume only if you agree; never
reboot the target, and send you to a terminal for waiting credentials.

## Example conversation

The user writes:

> Install Gentoo on the test VM. Create user Alex, take his password from
> ALEX_PW. UEFI with GRUB. If nothing else decides it, call the machine
> "server" and use the Europe/Prague timezone.

The results below are shortened. Identifiers are examples.

### 1. Read the hardware

```json
detect_hardware({"refresh": true})
```

```json
{
  "target": "gentoo-live",
  "collected_at": "2026-10-16T09:12:04+00:00",
  "inventory_hash": "0519c7ea46113d27ce8beff6bf4b160b65d65f63dcf35c3a2141ef4ece044014",
  "boot_mode": "uefi",
  "disks": [
    {"path": "/dev/sda", "size_bytes": 42949672960, "model": "VMware Virtual S",
     "serial": "6000c29a1b2c3d4e", "wwn": "0x6000c29a1b2c3d4e", "strength": "strong"},
    {"path": "/dev/sdb", "size_bytes": 31004295168, "model": "Ultra USB 3.0",
     "serial": "4C530001230517112093", "wwn": "", "strength": "strong"}
  ]
}
```

Two disks are listed, but `/dev/sdb` is the USB stick the live system booted
from. The AI may ask you which disk to use; here it lets `resolve_config`
infer the only eligible disk and shows you the result.

For static networking, the AI passes `network_method: "static"` and a
`network_static` object with `interface`, `address`, `gateway`, and `dns`
(see [Static network](../CONFIGURATION.md#static-network)); it must take the
interface name from the `network` section of the inventory, not guess it.

### 2. Preview the configuration

The AI translates the request. Explicit wishes become overrides; the "if
nothing else decides it" values become fallbacks:

```json
resolve_config({
  "overrides": {
    "username": "Alex",
    "user_password": {"env": "ALEX_PW"},
    "boot_mode": "uefi",
    "bootloader": "grub"
  },
  "fallbacks": {
    "hostname": "server",
    "timezone": "Europe/Prague"
  }
})
```

Important parts of the result:

```json
{
  "install": {
    "boot_mode": "uefi",
    "bootloader": "grub",
    "init_system": "openrc",
    "hostname": "server",
    "timezone": "Europe/Prague",
    "username": "alex",
    "password_source": "environment",
    "root_password_env": "GENTOO_ROOT_PASSWORD",
    "user_password_env": "ALEX_PW",
    "disk": "/dev/sda",
    "partition_table": "gpt",
    "disk_must_be_erased": true
  },
  "sources": {
    "boot_mode": "override",
    "bootloader": "override",
    "username": "override",
    "user_password_env": "override",
    "hostname": "fallback",
    "timezone": "fallback",
    "init_system": "default",
    "root_password_env": "default",
    "disk": "inferred",
    "partition_table": "derived",
    "partition_layout": "derived",
    "disk_must_be_erased": "derived"
  },
  "warnings": [
    "disk /dev/sda was inferred as the only eligible disk; excluded: /dev/sdb (removable; usb transport)",
    "username Alex was lowercased to alex"
  ]
}
```

The AI should tell you both warnings: the disk was chosen by inference, and
the account is `alex` because Linux user names are lowercase. Had you asked for
BIOS on this UEFI-booted VM, the result would be `CONFIG_CONFLICT` ("boot_mode
bios was requested but the live system booted in uefi mode."), not a guess.

### 3. Create the plan

```json
create_plan({ "overrides": { ... }, "fallbacks": { ... } })
```

The same inputs give a stored plan:

```json
{
  "plan_id": "7d497846121f5a7b",
  "plan_hash": "7d497846121f5a7b99f827463cd3fc5fc0b631d5a481fd3d2501cf2240d3f8d5",
  "summary": "Plan: 7d497846121f5a7b\nPlan hash: ...\nDisk erase: YES, all data on the disk will be destroyed\n...",
  "phases": [
    {"id": "preflight", "destructive": false},
    {"id": "storage", "destructive": true},
    ...
  ],
  "approval": {
    "approved": false,
    "code": "APPROVAL_REQUIRED",
    "message": "Plan 7d497846121f5a7b is not approved. A human must run: gentoo-agent approve 7d497846121f5a7b"
  },
  "sources": { ... },
  "warnings": [ ... ]
}
```

### 4. Ask for approval

```json
approve_plan({"plan_id": "7d497846121f5a7b"})
```

Your client shows the dialog described in
[The approval dialog](#the-approval-dialog). You read it, check `approve`, and
accept. The model sees only the outcome:

```json
{
  "approved": true,
  "plan_id": "7d497846121f5a7b",
  "channel": "elicitation",
  "approved_until": "2026-10-16T10:15:40+00:00"
}
```

If you decline, the result is `APPROVAL_DECLINED` ("Approval not recorded.").
If the client cannot show dialogs, the result is `APPROVAL_REQUIRED` with
`details.command` set to `gentoo-agent approve 7d497846121f5a7b`, and the AI
asks you to run it in your own terminal.

### 5. Start and follow the run

```json
start_install({"plan_id": "7d497846121f5a7b"})
```

```json
{"run_id": "20261016T091610Z-5be2a0", "plan_id": "7d497846121f5a7b", "status": "starting"}
```

The AI then polls:

```json
get_run_status({"run_id": "20261016T091610Z-5be2a0"})
```

```json
{
  "run_id": "20261016T091610Z-5be2a0",
  "status": "running",
  "phase_count": 9,
  "phase_index": 5,
  "current_phase": "configuration",
  "completed_phases": ["preflight", "storage", "filesystems", "base-system"],
  "last_error": null,
  "credentials_waiting": true,
  "credentials_applied": null,
  "credentials_hint": "Generated passwords are waiting. Read them yourself in a terminal: gentoo-agent run status 20261016T091610Z-5be2a0"
}
```

`credentials_waiting` is `true` because `GENTOO_ROOT_PASSWORD` was not set, so
a root password was generated. The AI reads progress with
`get_run_log({"run_id": "20261016T091610Z-5be2a0", "offset": 0, "limit": 50})`
and continues from `next_offset`.

### 6. Finish

When `status` becomes `installed_pending_reboot`, the AI may call
`verify_install({"plan_id": "7d497846121f5a7b"})` to run every phase's
read-only check again. It then tells you:

- the installation finished and the target was not rebooted;
- generated passwords are waiting, and you must read them yourself with
  `gentoo-agent run status 20261016T091610Z-5be2a0` in a terminal;
- you reboot the target yourself, as in Step 12 of the
  [CLI guide](cli-install.md#step-12-check-the-result-and-reboot).

## The approval dialog

The server builds the dialog text itself from the stored plan; the model
cannot change it. It contains the plan summary, in this order:

1. `Plan` and `Plan hash`.
2. `Disk erase` and `Destructive phases`. These always come first, before any
   value that was read from the target or the inventory. Read them before
   anything else.
3. `Target` (SSH user, address, port, name) and `Created`.
4. `Install` (boot mode, partition table, hostname, user, init system),
   `Network`, `Passwords` (source and variable names, never values), and
   `Access` (root password SSH, sudo, groups).
5. One `Disk` line with path, size, model, serial, and WWN, plus a `WARNING`
   line when the disk has no serial or WWN.

Then: "Approve this plan? The approval is valid for 60 minutes and covers
exactly the commands and disk above. Check 'approve' and accept only if you,
the human operator, agree." The form has a single boolean field, `approve`.

Values that come from the target or the configuration are shown on one line;
anything with newlines or control characters, and anything longer than 200
characters, is shown escaped and truncated, so it cannot fake extra lines.

Only an accept with `approve` checked writes a signed approval record for
60 minutes. Decline, cancel, or an unchecked box records nothing. If the dialog
fails (unsupported transport, client error, timeout), the server falls back to
`APPROVAL_REQUIRED` and records nothing.

Only you answer the dialog. Do not configure anything that answers it for you
(for example an elicitation hook), and never tell the AI the answer.

## Reading generated passwords

Generated passwords never pass through the model. `get_run_status` only says
whether they are waiting (`credentials_waiting`) and whether they were applied
(`credentials_applied`: `true`, `false` meaning the authentication phase did
not run, or `null` while pending).

Read them yourself, in your own terminal, after the run has ended:

```sh
GENTOO_AGENT_HOME=<work dir> <venv>/bin/gentoo-agent run status <run_id>
```

In development mode that is `.venv/bin/gentoo-agent run status <run_id>` in
the repository. The command shows the passwords once and deletes them. Do not
ask the AI to run this command: whoever runs it first receives the passwords,
and they are gone afterwards.

## What the AI must never do

- Pass a password as text, or ask you to type one into the chat. Passwords are
  only `{"env": "NAME"}` references.
- Approve a plan in any way other than `approve_plan`: not by running
  `gentoo-agent approve`, not by writing to `approvals/`, `plans/`, or
  `secrets/`, and not by answering the dialog.
- Read `secrets/` (the approval signing key) or `runs/<run_id>/credentials.json`,
  or run `gentoo-agent run status` to fetch generated passwords.
- Reboot the target.
- Guess a disk or a boot mode, or resume a failed run without your agreement.

In development mode these are instructions, enforced only by the server's
design and the deny rules in `.claude/settings.json`. In
[protected mode](protected-mode.md), the operator profile makes the approval,
the signing key, and the credentials unreachable for the agent's own tools.

Problems and error codes are covered in [Troubleshooting](../TROUBLESHOOTING.md).
