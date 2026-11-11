# Functions lifecycle timing

Owner: PAO / Functions; platform restart controller remains separate. Core source and admission decisions are unchanged.

Each Functions process writes logs/lifecycle/<pid>.jsonl under its instance home.
Start and end records carry UTC epoch nanoseconds, monotonic nanoseconds, PID,
parent PID, thread, span identity, generation and Agent where available.
An interrupted process retains the start record. Missing end is not zero time.
Call arguments, messages, credentials and provider responses are never recorded.

High-level qualification, shared preparation/activation/commit, Agent startup,
worker launch/admission, wait-ready and backend initialization intervals are recorded.
The first observation also records native Windows process creation time: the preceding
bootstrap/admission/import interval is accounted for as one interval. Its protected
internal steps cannot be retroactively split by Functions instrumentation.

For temporary detailed observation, create state/instance/lifecycle-timing.json:
{"observe_calls": true, "expires_at": <Unix expiry seconds>}
Python 3.12 selective code-object monitoring observes selected existing qualification,
runtime verification, artifact and initialization calls. It does not replace functions
or alter checks. Other instrumentation tool IDs are respected; unsupported or occupied
monitoring is reported without disrupting startup. Remove/expire the file afterwards.

Intervals are inclusive and may overlap across tasks, threads and processes. Compute
critical-path wall time or interval unions; do not add all worker wall times together.
thread_cpu_ms is thread activity during a wall interval, not per-task CPU attribution.
Logging failure cannot block a lifecycle action; verify actual log coverage before
claiming complete measurement. Use the production lifecycle command and recorded
operation/restart receipt to join frontend observations, process transitions and spans.

Focused validation: lifecycle timing, runtime release, startup manager, runtime lifecycle,
worker supervisor, runtime contract/generation/protocol, reboot manager, minimal Core,
kernel process and handoff regressions. Real qualified-process tests require a committed
candidate; a dirty-source rejection is a safety check, not a timing regression.
