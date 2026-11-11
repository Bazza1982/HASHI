# Functions startup performance

Core v5 admission, runtime qualification, artifact verification and per-Worker identity checks remain unchanged.

`global.startup_agent_concurrency` controls the Functions initial-Agent scheduler. It accepts an integer from 1 to 16 and defaults to 8. The actual limit is capped by the selected Agent count. Every Agent still uses the existing isolated Worker lifecycle; failure of one Agent does not skip the remaining roster. Change it only after measuring resource contention and total startup time on the target machine.

Normal lifecycle logs now separate Worker contract checks, runtime module imports, configuration loading, runtime construction, handler binding, backend initialization and ready-event publication. Backend initialization also records adapter loading, construction, tool attachment and adapter initialization separately. Logs contain timing and lifecycle identity, never configuration bodies or credentials. Nested and parallel durations must not be added to claim an end-to-end saving.

No startup validation is bypassed. Core v5 is not part of this change.

The per-Worker runtime fingerprint freshness scan now runs in a background thread, retaining every policy and installed-metadata check. It is timed as worker.runtime_fingerprint. This keeps synchronous metadata I/O off the Functions control and IPC event loop. Cancellation before that read completes cannot launch a late Worker; read failures still fail closed.
