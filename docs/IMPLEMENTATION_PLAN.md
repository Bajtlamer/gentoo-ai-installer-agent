# Implementation plan

## Milestone 1: read-only prototype

- Boot a disposable Gentoo AMD64 VM from live media.
- Establish SSH with a temporary key and a verified host key.
- Run Handbook fetch and read-only inventory.
- Review generated JSON and logs manually.

## Milestone 2: planning

- Add a confirmed target profile.
- Have the Planner map Handbook sections to phases.
- Validate required fields before any phase can be approved.

## Milestone 3: non-destructive phases

- Implement mount discovery and validation.
- Implement configuration backup and diff preview.
- Add Verifier postconditions and resumable state transitions.

## Milestone 4: destructive phases in a VM

- Add storage and filesystem scripts with dry-run output first.
- Require exact disk identity and an approval record.
- Test interruption, reconnect, failure, and rollback behavior.

The guarded executor now covers preflight, storage, filesystems, stage3/base
system, configuration, authentication, kernel, GRUB, and validation. The
authentication phase installs and verifies OpenSSH, provisions the configured
regular user, and sets passwords through a non-persistent stdin channel. It
remains disabled until
the reviewed configuration is explicitly approved. Automatic reboot and
post-install repair are intentionally outside this first installer milestone.

## Milestone 5: hardware

- Use a verified backup and an available console.
- Re-run the complete workflow with destructive operations still manually
  approved.
