# HASHI v4.0.0-alpha.2 — Release Candidate Notes

Release focus: **advanced HASHI Engine Runtime (HER)**.

This alpha release candidate turns the original HER/Claw backend foundation
into a clean-room, provider-neutral Python orchestration runtime. The retired
HER v1 Rust executable, packaged binaries, and rebuild workflow are no longer
reachable; `her` resolves forward to `her-v2`.

The independent Enterprise AAI package line remains
`v0.1.0-alpha.1` / `0.1.0a1`; this document describes the broader HASHI v4
platform line.

> **Product-surface update (2026-08-31):** HER v2 now exposes only Direct
> (`zero`), Strategic (`low`), and Planned (`medium`). The higher-mode sections
> in these notes describe retained dormant implementation and regression
> evidence, not selectable production modes. See
> [HER_V2_THREE_MODE_DECISION.md](HER_V2_THREE_MODE_DECISION.md).

> **Agent working-mode update (2026-09-01):** Fixed is now the default for
> session-capable backends, and Flex remains the explicit backend-switching
> mode. Wrapper, Audit, and Dual-brain are no longer selectable. Persisted
> legacy values migrate safely to Fixed or Flex while their historical
> configuration blocks remain intact.

## Why This Is a Significant HER Update

HER v2 is HASHI-owned orchestration over provider, tool, delivery, and audit
interfaces. Its release contract includes:

- three task-matched production execution modes using stable `zero`, `low`, and
  `medium` wire values;
- retained but non-public Replanning, Review, and Verification internals whose
  future product design is postponed;
- hard evidence receipts that reject paper-only, fabricated, stale,
  cross-stage, incomplete, and failed passing claims;
- configured recipes or direct argv checks in the authoritative workspace with
  inherited authority and execution-derived timeouts;
- provider-neutral Quick and Pro routing, with Compact following the active
  Quick/Light target at fixed high HER effort;
- Single or Hybrid stage routing, including the HASHI API provider;
- provider/model-specific multimodal routing with authorised local fallback;
- exact native DeepSeek image routing for
  `deepseek-v4-flash-vision-exp`, without granting image capability to
  text-only DeepSeek models;
- explicit stream-channel ownership and idempotent user delivery;
- crash-safe transient WIP context with separately auditable
  start/inject/preserve/clear lifecycle events;
- isolated scheduler execution with one authoritative user conversation;
- a client-neutral persistent Session/Message/Run/Event API behind a fail-closed
  qualification boundary, including restart reconciliation for orphaned Runs;
- three-state Telegram notification control, including Quiet final/error-only
  notification signalling; and
- HASHI Tool Gateway access, secure multimedia, and agent-local Habits.

## HER Execution Modes

HER execution mode controls the amount and shape of orchestration, not the
provider's private reasoning setting. The descriptive names are shown in HER
menus and status while existing configuration and API wire values remain
compatible.

| Display name | Wire value | Execution contract |
| --- | --- | --- |
| Direct | `zero` | One fully capable Quick-model agent at default provider reasoning `high`; no other HER stage and no automatic effort upgrade |
| Fast path | `low` | Direct Execution without formal Planning |
| Planned | `medium` | Planning followed by Execution |
| Adaptive | `high` | Planning and Execution with compulsory Replanning every 10 completed results or 300 seconds at a safe boundary |
| Reviewed | `xhigh` | Adaptive path plus one independent read-only Review; a failed Review permits one Primary-Agent remediation and one closure Review |
| Assured | `max` | Reviewed path plus comprehensive Verification of the latest state, with at most three Verification attempts and remediation between failed checks |

`/effort direct`, `/effort reviewed`, and `/effort assured` are accepted aliases
and persist as `zero`, `xhigh`, and `max`. Fast path, Planned, and Adaptive
aliases normalize in the same way. Non-HER backends retain their established
reasoning-effort UI.

Review is read-only; Verification has validation-only workspace authority.
Passing or failing findings require completed receipts from the exact current
stage invocation and stable opening/closing workspace snapshots. A failed tool may
support only a failed or inconclusive result. Technical failure is reported as
unavailable, never softened into a conditional pass, and assurance never
overwrites Execution's disposition.

## Authoritative-workspace Verification

`workspace_inspect` provides bounded read-only status, diff, search, hash,
artifact, and snapshot evidence. `verification_run` runs configured recipes or
direct process argv in the authoritative current workspace without a copy or
implicit shell. It inherits the HASHI process identity, filesystem,
environment, `HOME`, and network authority. The effective timeout grows from
cumulative Execution duration (default: 1.5× plus five minutes), and a
verifier-requested value can raise but never reduce it.

The final report distinguishes verified, partly verified, not AI-verifiable,
and unavailable outcomes while preserving the Primary Agent's substantive
Execution result.

## Context Compaction Continuity

Automatic Compact is capacity maintenance, never a hard gate on user work.
When a watermark is reached, HASHI may compact eligible older history through
the active Quick/Light route. If that maintenance is unavailable, times out,
fails validation, exhausts both attempts, or cannot shrink enough, HASHI keeps
the original or best reduced prompt, issues a mandatory user-visible warning,
and still calls the selected model. This continuation rule remains in force at
120,000 estimated tokens or above; only the provider can truthfully reject the
actual submitted request for its own context limit.

## Compulsory Execution Replanning

Adaptive (`high`), Reviewed (`xhigh`), and Assured (`max`) cycles share one
request-local cadence coordinator across the Primary Agent and bounded
sub-agents. After 10 completed Execution tool results or 300 monotonic seconds,
the next safe boundary unconditionally enters tool-free Replanning before more
work is admitted or a completion candidate is accepted. Triage's compatibility-
named `STANDARD`/`HIGH_RISK` field remains immutable risk metadata but is not an
eligibility gate.

Each Replan estimates completion against the original goal, checks whether the
active plan remains suitable, activates the next plan version, and sends one
Persona-rendered progress commentary with deterministic fact-preserving
fallback. Work resumes below 100%; at 100%, no extra work is added. Immediate
permission denial, approval requirements, user stop/steer, and audit failure
retain their authority. The cadence never cancels an active tool, caps total
work or Replans, consumes Review/Verification allowances, or replays a side
effect.


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
acknowledgement as repeated commentary. HASHI prefixes only acknowledgement and
commentary presentation with `💬 ` while preserving the original audit text.
A final candidate paired only with `StructuredOutput` remains on the final lane
instead of appearing once as commentary and again as final.

An unfinished HER v2 turn leaves observable progress in a per-Agent WIP
Journal. A later turn receives that prior context with an explicit warning not
to continue it by default. The Journal clears only after a later Ledger is
durably `COMPLETED`, while lifecycle events remain in the canonical HER v2
audit log.

OpenRouter and DeepSeek adapters remain available as HER v2 providers, but are
no longer selectable as top-level `/backend` engines. A legacy direct active
selection migrates only when the Agent already grants an explicit HER v2 row;
otherwise startup fails with an actionable configuration error.

## Adoption Workflow

HER v2 is ordinary hot-reloadable HASHI Python code. `/reboot min` and numbered
reboots compile and reload the shared project modules while interrupting only
the selected Agent lifecycle. Public interface additions such as the
`VERIFYING` state, Verification route, and evidence receipts do not widen that
target. `/rebuild` is now a compatibility notice and performs no build or
restart.

## Other Consolidated Improvements

- **HER Habit/Meditation** — optional, agent-local learning with explicit
  ownership, recoverable state, silent no-change outcomes, and isolated
  low-effort reflection.
- **Secure multimedia** — bounded image, PDF, audio, and compatible tool-result
  media flows through one provider-aware contract. Capable models receive
  native ordered content; unsupported modalities use only authorised local
  inspection paths and otherwise fail explicitly.
- **Provider and model routing** — Quick and Pro slots support Single or Hybrid
  stage routing, including another HASHI OpenAI-compatible Gateway through the
  HASHI API provider.
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
- Assured test execution deliberately shares the authoritative workspace and
  HASHI process authority; its validation-only effects and timeout basis are
  recorded in evidence receipts.
- Enterprise server, IdP, SIEM, HA, and cloud validation remain tracked by the
  separate Enterprise AAI line.
- Superloop remains an explicit-evidence operational foundation rather than a
  claim of fully unattended production automation.

## Verification Before Tagging

The release gate requires:

- the full HASHI Python suite;
- focused lifecycle, receipt-integrity, Review, Verification, workspace-authority,
  continuity, stream routing, execution-mode, Agent Overview, and remote
  authentication tests;
- static compile, architecture-boundary, Markdown-link, diff-hygiene, and
  sensitive-publication scans;
- an approved live HER canary before any production adoption claim.

Exact results are recorded during final release preparation; no tag or GitHub
push is implied by this release-candidate document. The latest integrated
results and outbound boundary are recorded in the
[2026-08-27 checkpoint](HASHI_UNRELEASED_CHECKPOINT_2026-08-27.md).
