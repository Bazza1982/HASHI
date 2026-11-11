# Functions startup performance

Core v5 admission, runtime qualification, artifact verification and per-Worker identity checks remain unchanged.

`global.startup_agent_concurrency` controls the Functions initial-Agent scheduler. It accepts an integer from 1 to 16 and defaults to 8. The actual limit is capped by the selected Agent count. Every Agent still uses the existing isolated Worker lifecycle; failure of one Agent does not skip the remaining roster. Change it only after measuring resource contention and total startup time on the target machine.

Normal lifecycle logs now separate Worker contract checks, runtime module imports, configuration loading, runtime construction, handler binding, backend initialization and ready-event publication. Backend initialization also records adapter loading, construction, tool attachment and adapter initialization separately. Logs contain timing and lifecycle identity, never configuration bodies or credentials. Nested and parallel durations must not be added to claim an end-to-end saving.

No startup validation is bypassed. Core v5 is not part of this change.

global.worker_launch_concurrency independently bounds calls to the unchanged Core process launch/admission API. It accepts an integer from 1 to 16 and defaults to 8. A slot is released after admission, before backend preparation and ready-event waiting. This permits a wider initial-Agent pool to pipeline admission and initialization without increasing simultaneous admission work. The bound is frozen on first use in each Functions supervisor; adopt a new Functions generation to apply a setting change. Failed or cancelled admission releases its slot. Worker readiness failures still use the original forced retirement path.
