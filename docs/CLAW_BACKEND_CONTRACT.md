# HASHI Claw Backend Contract

Status: active for `0.1.3-hashi.2`

## Deployment scope

This contract and packaged Linux binary belong to the standalone HASHI runtime,
currently certified on HASHI1. They do not update Aptenra's embedded HASHI runtime,
Windows `aptenra_hashi.exe`, debug candidate, or installation package.

Aptenra adoption is a separate release task: explicitly integrate the selected HASHI
changes, build the Windows artifact, record its provenance and SHA-256, and run the
Aptenra product certification suite. No change in this document or package propagates
to Aptenra automatically.

## Ownership boundary

HASHI owns agent identity, memory injection, handoff context, authorization, request
identity, cancellation, and delivery. Claw owns the model/tool-loop session inside one
active backend lifecycle. HASHI records the Claw `session_id` and resumes it for the next
turn instead of rebuilding the browser plan from scratch.

The production `ClawCLIAdapter` therefore:

- reports `supports_sessions = true`;
- captures `session_id` from `run_started` before tool execution and checkpoints it again
  from `run_finished`;
- passes `--resume <session_id>` on the next turn in the same backend lifecycle;
- clears the Claw identity on `/new` and when a new adapter instance is created;
- keeps Claw configuration and Tool Gateway state isolated per agent workspace.

The runtime lifecycle must send incremental turns when `supports_sessions` is true; it
must not also replay the full HASHI conversation. Capturing the ID at `run_started`
allows a cancelled turn to retain its intended session identity. Resume after a hard
process kill remains best-effort if Claw did not persist the session file before exit;
that case is logged and must not silently switch to a different agent's session.

## Tool Gateway contract

`ToolRegistry` remains the single capability catalog and execution core. API backends
consume it directly. Claw consumes the same registry through the `hashi-tools` MCP stdio
adapter generated under the agent's `backend_state` directory.

- Browser behavior remains in `tools.browser`; the Claw adapter does not duplicate it.
- The generated Gateway context is mode `0600`, contains only secrets required by the
  allowed tools, and excludes live runtime/config objects.
- Claw-native `--allowedTools` and HASHI capability permissions remain separate layers.
- A required `hashi-tools` MCP entry is validated during backend initialization.
- MCP calls use existing JSON schemas, ToolRegistry permission checks, and tool audit
  records.
- The gateway stops excessive total calls, repeated identical calls, and consecutive
  error loops with explicit errors instructing the model to report partial progress.

## Streaming contract

The authenticated packaged Claw may emit `stream-json`. HASHI consumes assistant,
thinking, tool, and usage events, but HASHI remains responsible for deciding which
events are visible and how final delivery is promoted. Encrypted or provider-redacted
reasoning must never be reconstructed or exposed.

## Binary contract

Production resolution uses `runtime_policy = require-packaged`. The adapter verifies the
platform, executable permission, manifest identity, and SHA-256 before execution. The
certified binary and its provenance are recorded in:

- `hashi_assets/claw/manifest.json`
- `hashi_assets/claw/certification_baseline.json`

A source checkout, PATH binary, or legacy external binary must not silently replace the
packaged runtime.

## Certification exceptions

The baseline is deliberately non-expanding:

- exactly one upstream Rust workspace test is allowed to fail because its expected
  degraded sandbox status conflicts with this host's fully active sandbox;
- exactly six upstream Clippy diagnostics are recognized in Trident/RAG;
- every other Rust workspace test must pass;
- any new Clippy diagnostic fails certification;
- if an allowed item starts passing, certification also fails until the stale exception
  is removed.

Run the full certification check with:

```bash
python scripts/verify_claw_certification.py \
  --source-root /path/to/claw-code-hashi-4ea31c1
```

## Remaining session limitation

Session identity and normal multi-turn resume are active. Persisting an in-flight model
plan across an operating-system kill still depends on whether Claw has flushed its
session file. HASHI therefore treats interrupted-turn resume as best-effort and relies
on structured tool audit records to diagnose completed side effects.
