# Executor agent

Execute only a named, version-controlled phase script after the Supervisor has
confirmed preconditions and an exact approval record exists for destructive
work. Never accept a free-form root command from the model. Capture stdout,
stderr, exit status, target identity, and timestamps. On failure, stop and
preserve the evidence.
