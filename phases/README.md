# Phase contract

Every future phase must define:

1. `check`: read the current state without changing it;
2. `apply`: make only the changes required by the approved plan;
3. `verify`: prove the postconditions;
4. `rollback` or a documented recovery path where practical.

Phase scripts must be idempotent, target-specific, logged, and safe to rerun
after an SSH disconnect. Storage, filesystem, bootloader, and reboot phases
must remain disabled until tested on a disposable VM.
