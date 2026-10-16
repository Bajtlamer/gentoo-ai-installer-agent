# Contributing to gentoo-agent

gentoo-agent installs Gentoo on a target over SSH, under human supervision.
Its commands erase disks, so a contribution is judged first by whether it
keeps the target safe and only then by what it adds. This guide covers the
setup, the tests, the safety rules for changes, and how work is organized.

## Contents

- [Read first](#read-first)
- [Development setup](#development-setup)
- [Running the tests](#running-the-tests)
- [Golden command files](#golden-command-files)
- [Core purity and errors](#core-purity-and-errors)
- [Safety rules for changes](#safety-rules-for-changes)
- [How work is done](#how-work-is-done)
- [Git and commits](#git-and-commits)
- [Writing conventions](#writing-conventions)
- [Reporting security issues](#reporting-security-issues)
- [License](#license)

## Read first

1. [README.md](README.md): what the project does and how it is used.
2. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the package layout, the plan,
   approval, and run flow, and which module owns what.
3. [AGENTS.md](AGENTS.md): the safety rules. They bind every change,
   including code that alters what the executor sends to a target.
4. [CLAUDE.md](CLAUDE.md): commands and architecture notes for AI-assisted
   work in this repository.

[docs/CONFIGURATION.md](docs/CONFIGURATION.md) is the reference for every
configuration field and rule.

## Development setup

You need Python 3.12 or newer and an OpenSSH client. The core package uses
only the standard library; the MCP server needs the optional `mcp` extra.

```sh
git clone https://github.com/Bajtlamer/gentoo-ai-installer-agent.git
cd gentoo-ai-installer-agent
python3 -m venv .venv
.venv/bin/pip install -e '.[mcp]'
```

The editable install adds the `gentoo-agent` and `gentoo-agent-mcp` commands
to the virtual environment. `python -m gentoo_agent` and
`python tools/gentoo_agent.py` run the same CLI.

Development needs no target machine. Local commands such as
`python tools/gentoo_agent.py validate` and
`python tools/gentoo_agent.py install --dry-run` never connect to one. Do not
run commands that contact a target (`inspect`, `handbook`,
`prepare_install.py`, `run start`, `run resume`) unless you are deliberately
testing against a disposable VM.

## Running the tests

The tests use `unittest` and need no network or SSH. Run them with both
interpreters before you ask for a merge:

```sh
# System Python: the MCP server tests skip themselves without the mcp extra
python3 -m unittest discover -s tests

# Project virtual environment: runs everything, including the MCP tests
.venv/bin/python -m unittest discover -s tests
```

Run one module or one test method:

```sh
.venv/bin/python -m unittest tests.test_validate
.venv/bin/python -m unittest tests.test_authentication.AuthenticationConfigurationTests.test_sudo_can_be_disabled_explicitly
```

Tests that need a repository root use `tests/helpers.temp_home()`, which
points `GENTOO_AGENT_HOME` at a fresh temporary directory. Use it instead of
writing into the checkout.

## Golden command files

`tests/test_golden_commands.py` renders the apply and check program of every
phase for the five configurations in `tests/golden/configs/` and compares them
byte for byte with the matching files in `tests/golden/`. These files are the
exact shell programs a target receives, so any difference is a change in
behavior on real hardware.

- When the golden test fails and you did not mean to change a command, the
  failure is a bug: fix the code, not the golden file.
- Regenerate the files only for an intentional command change:

  ```sh
  GOLDEN_UPDATE=1 .venv/bin/python -m unittest tests.test_golden_commands
  ```

- Every golden diff must be reviewed line by line in the pull request, and
  the pull request must say why each changed line changed.

## Core purity and errors

- `gentoo_agent/core/` never prints, prompts, or exits, and never imports
  `sys`, `getpass`, `argparse`, `builtins`, or `pdb`. It returns data and
  raises errors. `tests/test_core_purity.py` enforces this.
- `gentoo_agent/mcp/` never prints or exits either: stdout carries the MCP
  protocol. Log to stderr through `logging`.
- Errors are `AgentError(code, message, details)` with a code from
  `gentoo_agent/core/errors.py`. Reuse an existing code where it fits; a new
  code is part of the interface that the CLI and MCP clients see.
- Front ends (`cli.py`, `prepare.py`, `init_config.py`, `mcp/server.py`) turn
  errors into text or MCP results. Prompts reach `core/` only as callbacks.

## Safety rules for changes

These rules apply to every change, however small:

- Anything that reaches a remote command must be validated first, in
  `core/validate.py` (`validated_settings()`) or `core/resolve.py` for values
  that come from an AI client.
- Quote every value exactly once with `shlex.quote` (the `q()` helper in
  `core/phases.py`). Do not build quoting by hand and do not quote twice.
- Cover every new value with tests, including hostile inputs: shell
  metacharacters, newlines, control characters, path traversal, wrong types,
  and oversized values.
- Never add free-form remote commands. Commands are generated from validated
  settings, and `inspect` runs only the `READ_ONLY_COMMANDS` allowlist in
  `core/ssh.py`.
- Never weaken the approval gate (`require_approved_plan()`), the single-run
  lock, `claim_run()`, or the disk drift check (`TARGET_DRIFT`). A change
  that touches any of them needs the stronger review described below.
- Never log, print, store in configuration, or return to an MCP client a
  password. Passwords travel only as `chpasswd` records over SSH stdin.
- Test destructive changes (storage, filesystems, bootloader, anything that
  writes to the target disk) on a disposable VM only, never on hardware with
  data you need.

## How work is done

This is the process used for the work so far:

1. A design spec in `docs/superpowers/specs/` describes the goal, the
   decisions, and the build order.
2. An implementation plan in `docs/superpowers/plans/` breaks the spec into
   small tasks with the exact files, tests, and commands for each.
3. Each task is implemented test first (TDD): a failing test, the code that
   makes it pass, then a commit.
4. Each task gets two reviews: a spec review (does it do what the plan says,
   no more and no less) and a code-quality review.
5. Safety-critical code (validation, quoting, approval, signing, the run lock,
   drift detection, credentials) gets a stronger review that also tries to
   break it.
6. Before a branch is merged, the whole branch is reviewed as one change.

Small fixes and documentation changes can skip the spec and plan, but not the
tests or the review.

## Git and commits

- Work on a branch or in a git worktree, never directly on `main`.
- Keep commits small and focused. Write the subject in the imperative mood,
  for example "Refuse systemd in resolve_config".
- Before a merge, the full test suite passes with both `python3` and
  `.venv/bin/python`.
- AI-assisted commits end with a `Co-Authored-By:` trailer that names the
  assistant.
- Never commit private files: `config/target.json`, `config/install.json`,
  `state/state.json`, or anything under `inventory/`, `logs/`, `plans/`,
  `approvals/`, `runs/`, or `secrets/`. They are gitignored; keep it that way.

## Writing conventions

- Write code comments, documentation, and commit messages in English.
- Do not use em dashes. Use a colon, a comma, or a new sentence instead.
- Do not use emoji or icons.
- Permission rules in `.claude/settings.json` and
  `docs/operator/claude-settings.json` must be root-anchored, for example
  `Edit(/plans/**)`. A rule such as `Edit(plans/**)` also matches
  `docs/superpowers/plans/`. Deny file changes with `Edit(...)` rules;
  `tests/test_operator_profile.py` checks the required rules.
- When you add a configuration field, update the default files,
  `init_config.py` and `prepare.py` if they set it, and
  [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Reporting security issues

Do not report security problems in public issues. Report them privately to
the maintainer through GitHub, using the repository's private vulnerability
report form (Security tab, "Report a vulnerability") at
[Bajtlamer/gentoo-ai-installer-agent](https://github.com/Bajtlamer/gentoo-ai-installer-agent).
Include the affected commit, the steps to reproduce, and the impact, for
example a way to start a run without approval or to inject a remote command.

## License

gentoo-agent is released under the MIT License; see [LICENSE](LICENSE). By
contributing, you agree that your contributions are licensed under the same
license.
