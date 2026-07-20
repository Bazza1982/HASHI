# HASHI Claw Backend Contract

Status: active for `0.1.3-hashi.1`

## Deployment scope

This contract and packaged Linux binary belong to the standalone HASHI runtime,
currently certified on HASHI1. They do not update Aptenra's embedded HASHI runtime,
Windows `aptenra_hashi.exe`, debug candidate, or installation package.

Aptenra adoption is a separate release task: explicitly integrate the selected HASHI
changes, build the Windows artifact, record its provenance and SHA-256, and run the
Aptenra product certification suite. No change in this document or package propagates
to Aptenra automatically.

## Ownership boundary

HASHI owns conversation continuity, memory injection, handoff context, authorization,
request identity, cancellation, and delivery. Claw is a stateless per-turn execution
backend. Every request receives the complete context selected by HASHI and starts a new
Claw process.

The production `ClawCLIAdapter` therefore:

- reports `supports_sessions = false`;
- never passes `--resume` for a normal HASHI turn;
- treats `/new` as a HASHI continuity reset, not a request to retain or mutate hidden
  Claw session state;
- may expose low-level resume arguments only in diagnostic helpers that are outside the
  production adapter contract.

This is intentional. It prevents duplicate context ownership, stale permission state,
implicit cross-agent state, and failures caused by passing a normal prompt after
`--resume latest`, which Claw `0.1.0` and `0.1.3` both reject.

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

## Future persistent-session mode

Persistent Claw sessions require a separate design and cannot be enabled by changing a
single adapter flag. A future opt-in mode must define agent/workspace binding, context
deduplication, permission invalidation, `/new` and `/handoff` semantics, backend-switch
lifecycle, corruption recovery, and audit visibility. Stateless execution remains the
safe default until that contract is implemented and certified.
