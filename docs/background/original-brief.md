> Note: this is the original problem statement and research from the start of
> the project, kept for history. The current design is described in
> [ARCHITECTURE.md](../ARCHITECTURE.md) and the [design spec](../superpowers/specs/2026-10-15-mcp-server-design.md).

# How to use agents to install or fix a Gentoo Linux system

## Answer in one sentence

Use a local supervisor agent over OpenSSH, backed by specialized planner,
inspector, executor, verifier, and recovery agents. Keep the target changes in
idempotent scripts, persist state outside the live image, and require human
approval before destructive operations or reboot.

I want to learn the right way to install Gentoo Linux on a remote machine using SSH. The technique must also work for fixing issues on a remote machine.

### Problem description - the target goal

The goal is to boot the remote machine from a Live CD or DVD, start the SSHD service, and continue the installation over an SSH connection from another device, such as a laptop or PC.

There are two situations:

- The machine is empty and ready for a fresh installation.
- The machine already has a Linux distribution installed but can’t boot for some reason.

In the first scenario, the process is much clearer. We connect over SSH to the empty device, then start the installation process from scratch. The situation in the second scenario is a bit more complicated. I need to find the problem, fix it, and then reboot the machine to resume normal work. In both scenarios, we can use the same technique: let the AI agent operate over SSH on the machine, whether that means starting a fresh installation or trying a fix.

### Find the right solution - what we need

We want to find the best way to achieve these procedures: how to securely navigate the AI agent, control it, and deliver instructions for the tasks or issues we need to fix. Which tool to use, and which AI, agent, skill, or model to use.

### The requirements - restrictions

We want to use only the secure SSH protocol and operate from the terminal. All techniques and processes must be repeatable on any PC, laptop, or server. The installation must be repeatable and run automatically from start to finish, detecting the current state and building a plan to continue every restart in or out of a stack situation.

On the other hand, the user is always ready to help or operate behind the scenes, whenever needed.

## Proposed solution: a supervised multi-agent system

The best fit for this problem is not one unrestricted agent. Use a small team
of specialized agents with different permissions and a human approval gate.
The agents share a state directory in the operator's workspace and communicate
through structured files, not through vague conversational assumptions.

```text
                    +----------------------+
                    | Human approval gate  |
                    +----------+-----------+
                               |
             +-----------------+-----------------+
             |                                   |
   +---------v---------+               +---------v---------+
   | Planner agent     |               | Recovery agent    |
   | plan + decisions  |               | diagnose + repair |
   +---------+---------+               +---------+---------+
             |                                   |
   +---------v---------+               +---------v---------+
   | Inspector agent   |               | Verifier agent    |
   | read-only SSH    |               | checks + evidence |
   +---------+---------+               +---------+---------+
             |                                   |
             +-----------------+-----------------+
                               |
                    +----------v-----------+
                    | SSH execution agent  |
                    | approved scripts     |
                    +----------+-----------+
                               |
                         Gentoo target
```

### Agent roles

1. **Planner** — converts the user's requirements into a plan and asks the
   questions that must be answered before any destructive action. It cannot
   execute shell commands.
2. **Inspector** — connects over SSH with read-only commands and writes a
   machine-readable inventory: disks, filesystems, boot mode, mounts, network,
   kernel, services, and errors.
3. **Executor** — runs only named, version-controlled phase scripts. It cannot
   invent a replacement command when a script fails.
4. **Verifier** — checks postconditions after each phase and records evidence.
   It can approve a safe next phase but cannot approve destructive storage
   operations.
5. **Recovery** — is used when the target does not boot or a verification step
   fails. It is diagnostic first and can propose a repair, but a human must
   approve the repair before it writes.

For a small personal project, Planner, Inspector, and Verifier may be one
agent, while Executor remains a narrowly controlled tool. The important
property is the permission boundary, not the number of model calls.

## Agent state and hand-offs

Keep the shared state on the operator machine, for example:

```text
gentoo-agent/
  AGENTS.md                 # safety and role instructions
  target.yaml               # confirmed target choices
  plan.yaml                 # current phase and dependencies
  state.json                # last known target state
  inventory/                # read-only SSH evidence
  phases/                   # deterministic shell scripts
  logs/                     # command output and verification results
  approvals/                # human approvals with timestamps
```

Every agent must read `target.yaml`, `plan.yaml`, and `state.json` before
acting. Each hand-off should produce a structured result such as:

```yaml
phase: storage
status: awaiting_approval
target: workstation-01
observations:
  boot_mode: uefi
  candidate_disk: /dev/nvme0n1
  existing_data: true
risks:
  - formatting the candidate disk destroys existing data
requested_approval: "Confirm /dev/nvme0n1 and the proposed partition layout"
```

The executor must refuse to run a destructive phase unless a matching approval
file exists for the exact target, phase, and disk identifier.

## Tool boundary

The agent should not receive a general-purpose “run any shell command as root”
tool. Give it narrow tools such as:

- `ssh_readonly(command)` for an allowlisted inventory command;
- `ssh_run_phase(phase, arguments)` for a checked-in script;
- `read_state()` and `write_state()` for validated state transitions;
- `request_approval(summary)` for destructive or reboot actions;
- `record_evidence(name, output)` for logs and verification results.

The implementation can use OpenSSH underneath, but the model sees a small
interface with validation, logging, timeouts, and an explicit approval check.
This is the main safety improvement over putting an SSH private key directly in
an agent's unrestricted shell environment.

## Fresh-install agent flow

1. The Planner collects the hardware and installation requirements.
2. The Inspector connects to the live image and creates an inventory.
3. The Planner proposes the disk layout, filesystem, init system, network
   method, and bootloader.
4. The human confirms the target disk and all irreversible choices.
5. The Executor runs one phase script at a time.
6. The Verifier checks the phase and updates `state.json`.
7. The agents repeat until the bootloader and post-boot SSH test pass.

## Repair-agent flow

1. Boot the live image and connect over SSH using the same bootstrap.
2. The Inspector gathers `lsblk`, `blkid`, mounts, boot mode, kernel logs,
   filesystem status, and network information without writing.
3. The Recovery agent classifies the failure and proposes the smallest repair.
4. The human approves the exact files, packages, or boot entries to change.
5. The Executor applies the repair and saves a complete transcript.
6. The Verifier tests the repaired system from the live environment.
7. Reboot only after the operator confirms that a recovery console remains
   available.

## Recommendation

Use a local agent runner with OpenSSH as its transport and an Agents SDK or
Codex-style terminal harness as the orchestration layer. Keep the dangerous
operations in checked-in scripts and keep the agent's tools narrow. Use a
capable current model for planning and diagnosis, but do not treat model choice
as the safety mechanism: state validation, least privilege, idempotent phases,
logs, and human approval are more important.

The result is an agent system that can resume after SSH disconnects, live-image
reboots, or partial failures while ensuring that no agent silently chooses the
wrong disk or converts an uncertain diagnosis into destructive action.

## Use the online Gentoo Handbook as the agent's procedure source

The agent can read the online Gentoo Handbook and use it as the installation
authority. That is preferable to embedding a large, potentially outdated set
of Gentoo commands in the prompt. The official AMD64 Handbook describes the
Internet-based installation flow and Gentoo's native tools, including OpenRC
and Portage: [Gentoo AMD64 Handbook](https://wiki.gentoo.org/wiki/Handbook:AMD64).
AMD64 is only an example; the Planner must select the Handbook for the actual
target architecture, such as ARM64, when applicable.

The agent should use the Handbook in a controlled way:

1. Detect the target architecture, boot mode, init system, storage layout, and
   network situation.
2. Select the matching Handbook and relevant section before proposing a phase.
3. Record the URL, section title, and retrieval time in the phase log.
4. Extract the procedure and variables, then adapt placeholders to the
   confirmed target state.
5. Show the resulting commands and expected effects before any destructive
   operation.
6. Execute only through the approved phase tool, not by blindly pasting every
   command block from the web page.
7. Verify the Handbook's expected postconditions before advancing.

The Handbook is a source of procedure, not permission. The agent still must
not guess `/dev/nvme0n1`, select a filesystem, overwrite an existing system,
or reboot merely because a web page contains a command that could do so.

### Handbook-to-agent mapping

The Planner can map the Handbook to the runbook approximately as follows:

```text
Handbook installation media and networking  -> bootstrap and SSH
Handbook disk preparation                   -> storage approval gate
Handbook filesystems and mounts             -> filesystem phase
Handbook stage and chroot                   -> base-system phase
Handbook configuration                      -> configuration phase
Handbook kernel and initramfs               -> kernel phase
Handbook bootloader                         -> bootloader approval gate
Handbook finalizing and reboot              -> validation and post-boot test
```

For a repair, the Recovery agent should consult the relevant Handbook section
only after it has classified the failure. For example, a bootloader failure
should lead to the bootloader and UEFI sections; it should not trigger a full
reinstallation. If the live Handbook changes during a long task, preserve the
source URLs and phase logs and ask for review before applying materially
different instructions.

This gives the agents current Gentoo knowledge while the runbook supplies the
repeatability, safety checks, approvals, and recovery behavior that a web page
alone cannot provide.
