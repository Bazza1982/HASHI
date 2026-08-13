# HASHI Engine Runtime (HER) Backend Contract

Status: active for HER `0.1.0-hashi.10`

HER is derived from the MIT-licensed Claw runtime. The upstream copyright and
license notice ships with every packaged HER release as `CLAW_LICENSE`.

## Deployment scope

This contract and packaged Linux binary belong to the standalone HASHI runtime.
Certification and deployment are performed independently for each HASHI instance. They
do not update Aptenra's embedded HASHI runtime, Windows `aptenra_hashi.exe`, debug
candidate, or installation package.

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

- Browser behavior remains in `tools.browser`; the HER adapter does not duplicate it.
- The generated Gateway context is mode `0600`, contains only secrets required by the
  allowed tools, and excludes live runtime/config objects.
- Claw-native `--allowedTools` and HASHI capability permissions remain separate layers.
- A required `hashi-tools` MCP entry is validated during backend initialization.
- HER pipes and continuously drains stdio MCP child stderr instead of inheriting it;
  child diagnostics cannot contaminate structured CLI output, and their raw content is
  not retained because it may contain server-owned secrets.
- MCP calls use existing JSON schemas, ToolRegistry permission checks, and tool audit
  records.
- The gateway stops excessive total calls, repeated identical calls, and consecutive
  error loops with explicit errors instructing the model to report partial progress.

## Streaming contract

The authenticated packaged HER may emit `stream-json`. HASHI consumes assistant,
thinking, tool, and usage events, but HASHI remains responsible for deciding which
events are visible and how final delivery is promoted. Encrypted or provider-redacted
reasoning must never be reconstructed or exposed.

Provider reasoning deltas are an exact byte-fragment contract. HER must preserve
leading, trailing, and whitespace-only fragments from the provider stream; HASHI
concatenates those raw fragments without trimming or guessing token boundaries. This
prevents both joined words (`Theusersays`) and invented spaces inside words
(`prov id er`).

## Binary contract

Production resolution uses `runtime_policy = require-packaged`. The adapter verifies the
platform, executable permission, manifest identity, and SHA-256 before execution. The
certified binary and its provenance are recorded in:

- `hashi_assets/her/manifest.json`
- `hashi_assets/her/certification_baseline.json`

A source checkout, PATH binary, or legacy external binary must not silently replace the
packaged runtime.

## Certification exceptions

The baseline is deliberately fail-closed:

- every Rust workspace test must pass;
- the full Rust workspace, including all targets, must pass Clippy with warnings denied;
- no diagnostic allowlist is active;
- any new test failure or Clippy diagnostic fails certification.

Run the full certification check with:

```bash
python scripts/verify_her_certification.py \
  --source-root /path/to/claw-code-hashi-4ea31c1
```

## Interrupted-turn continuation

Session identity and normal multi-turn resume are active. An operating-system kill may
still interrupt HER before its internal plan is fully flushed, so HASHI separately
persists the authoritative original user prompt when `/stop` is issued. A later bare
continuation request is rebound to that prompt while retaining the HER session,
workspace artefacts, and completed tool side effects. This makes task identity durable;
the exact internal model-plan position remains dependent on HER's last session flush.
