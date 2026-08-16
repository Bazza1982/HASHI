# HASHI v4.0.0-alpha.2 — Release Candidate Notes

Release focus: **advanced HASHI Engine Runtime (HER)**.

This alpha release candidate turns the original HER/Claw backend foundation
into a substantially more capable, testable, and maintainable HASHI runtime.
It keeps the proven Rust execution engine, integrates its reviewed source into
the HASHI repository, and adds a safe development rebuild/adoption path.

The independent Enterprise AAI package line remains
`v0.1.0-alpha.1` / `0.1.0a1`; this document describes the broader HASHI v4
platform line.

## Why This Is a Significant HER Update

HER is derived from the MIT-licensed Claw runtime and retains the upstream
copyright and license. HASHI now adds substantial runtime contracts and
orchestration that were not present in the original backend:

- seven task-matched execution effort levels;
- adaptive planning, evidence, review, and exact-test ceilings;
- bounded HER-private multi-agent coordination at `ultra`;
- persistent fixed-mode sessions and correct full-context mode boundaries;
- explicit stream-channel ownership and idempotent user delivery;
- isolated scheduler execution with one authoritative user conversation;
- HASHI Tool Gateway/MCP access, secure multimedia, and agent-local Habits;
- certified Linux and Windows packages from one reviewed `.22` source line;
- an integrated Rust source tree and transactional `/rebuild` workflow.

## HER Effort Levels

HER effort controls the amount and shape of agentic execution, not the
provider's private reasoning setting. Effort is a capability ceiling: a simple
request can still finish quickly at a high configured level.

| Effort | Execution contract |
| --- | --- |
| `low` | Direct single-Agent execution without TaskFrame planning |
| `medium` | Adaptive planning without a review loop |
| `high` | Adaptive planning with optional self-review |
| `xhigh` | Adds optional independent read-only review |
| `max` | Expands the single-Agent execution and assurance ceiling |
| `max+` | May also rerun exact plan-declared tests in an isolated snapshot |
| `ultra` | HER-private primary/worker orchestration with a bounded DAG, isolated sessions, evidence assembly, retries, and at most ten concurrent sub-agents |

`low` through `max+` are single-Agent HER efforts. `ultra` is intercepted by
the HASHI HER adapter and coordinates multiple ordinary HER sessions; the
native executable never receives `ultra` as a single-process effort value.

## Certified HER 0.1.0-hashi.22

The selected packaged runtime is `0.1.0-hashi.22` for Linux x86-64 and Windows
x86-64. Both packages declare the same reviewed source identity, platform
target, embedded provenance, and SHA-256 metadata. The rejected `.21` artifact
remains forensic evidence only and is not selected by the manifest.

The `.22` contract includes:

- persistent session resume with failed/cancelled-turn checkpoint hygiene;
- provider-native structured TaskFrame planning and bounded replanning;
- fail-open review and planning maintenance paths;
- stable commentary/final event semantics;
- finalization reserve and useful incomplete-run outcomes;
- Tool Gateway/MCP integration and secure media inputs;
- Linux and native Windows offline command certification.

## Conversation and Message Delivery

Direct user turns remain on one persistent HER conversation and execute in
order. Cron and scheduler work remains isolated, while an actually delivered
isolated result can become the next visible reply target. A user message binds
to the context visible when that message entered the queue; a later cron result
cannot retroactively capture it.

User-visible HER events now have explicit ownership and stable event IDs.
Technical telemetry, reasoning, commentary, control, and final responses use
separate lanes. Replayed copies of the same event are retained for audit but
shown to the user only once. Replanning no longer reuses the initial
acknowledgement as repeated commentary.

## Integrated `/rebuild` Development Workflow

`/rebuild` makes Rust development adoption systematic without weakening release
certification:

```text
integrated HER Rust source
→ source fingerprint and single-flight job
→ incremental Cargo build
→ offline candidate verification
→ immutable development candidate
→ atomic development selection
→ targeted idle-Agent reboot
→ post-adoption health check and result
```

The command reports a durable job ID and an explicit success, unchanged,
pending-activation, rollback, or failure reason. `/rebuild status [job-id]`
inspects the result. A normal `/reboot` continues to reload HASHI Python code
and reuse the already selected executable.

`/rebuild` does **not** modify the packaged manifest, overwrite a certified
binary, mint a release version, cross-compile another platform, or claim
production certification. Clean-source full Rust tests, Clippy, reproducible
packaging, checksums, provenance review, and live canaries remain separate
release gates.

## Other Consolidated Improvements

- **HER Habit/Meditation** — optional, agent-local learning with explicit
  ownership, recoverable state, silent no-change outcomes, and isolated
  low-effort reflection.
- **Secure multimedia** — bounded image, PDF, audio, and compatible tool-result
  media flows through the same HER authority boundary.
- **Workbench Agent Overview** — one canonical, read-only, no-store API view of
  Agent status, workzone, usage, system-prompt slots, and safe parked-topic
  summaries.
- **Remote terminal authentication** — signed shared-token requests can use the
  existing terminal execution endpoint alongside pairing bearer auth; missing
  or tampered signatures remain rejected.
- **Operational commentary** — model-authored commentary is independently
  controllable from technical verbosity and reasoning display.

## Alpha Boundaries

- This remains an alpha release candidate, not a production certification.
- `/rebuild` is for current-host development adoption and requires the Rust
  toolchain plus integrated source; ordinary installs use packaged binaries.
- `ultra` must be enabled and configured for a HER Agent and remains bounded by
  its authority, worker, retry, and workspace-isolation contracts.
- Enterprise server, IdP, SIEM, HA, and cloud validation remain tracked by the
  separate Enterprise AAI line.
- Superloop remains an explicit-evidence operational foundation rather than a
  claim of fully unattended production automation.

## Verification Before Tagging

The release gate requires:

- the full HASHI Python suite;
- the complete integrated HER Rust workspace suite;
- HER certification and package-manifest checks;
- focused continuity, stream routing, effort, Ultra, `/rebuild`, Agent
  Overview, and remote authentication tests;
- static compile, architecture-boundary, Markdown-link, diff-hygiene, and
  sensitive-publication scans;
- an approved live HER canary before any production adoption claim.

Exact results are recorded during final release preparation; no tag or GitHub
push is implied by this release-candidate document.
