# Agent Companion (AC) P0-P3 — HASHI2 HERv2J experiment

Date: 2026-09-24

## Scope

- Instance: `HASHI2`
- Branch: `exp-herv2j`
- Canary target: Arale only
- Functional owner: PAO, with a bounded TypeSafe/Jev advisory
- Engineering layer: replaceable Functions plus local instance opt-in
- Core status: unchanged

AC is disabled by default. Only an Agent with the explicit local
`agent_companion_enabled: true` setting receives a companion. This keeps the
experiment on HASHI2 and prevents it from silently affecting other Agents or
other HASHI instances.

## Decision

Each enabled Agent Turn starts one independent companion. It periodically reads
a compact, redacted state projection. Deterministic code detects known unsafe
shapes such as a resident application launched through a foreground shell. When
semantic judgment is useful, AC may ask TypeSafe/Jev one closed `Choice` question.
Jev supplies probabilities only; PAO code retains policy, authority, and
execution control.

The HASHI2 experiment uses the same fixed TypeSafe System One endpoint and
`typesafe_api_key`/`TYPESAFE_API_KEY` credential sources as the existing HERv2J
route and style experiments. Agent configuration cannot redirect the credential
or state snapshot to another endpoint. No prompt, transcript, memory, credential,
or local path enters the Jev state.

AC does not add a hard wall-clock timeout and does not change foreground shell
semantics. It uses the existing control lane for a typed intervention and sends
the Agent a bounded internal `type/action/next` message. The same issue at the
same progress sequence is handled once.

Resident applications use the typed `managed_process_start/status/stop` tools.
They bind ownership to the calling Agent and reuse `BackgroundJobManager` for
process-tree lifecycle. AC and the Tool Gateway never stop an arbitrary PID or a
process owned by another Agent; ownership and an active lease are checked first.

## P0-P3

1. P0: per-Turn lifecycle, background detach, completion/cancellation, and Worker shutdown cleanup.
2. P1: adjustable observation interval, minimal state, optional Jev Choice, and offline fallback.
3. P2: typed intervention, control-lane interruption, deduplication, and low-confidence safe behavior.
4. P3: managed-process start/status/stop with Agent ownership and lease validation.

## Verification and adoption

- Focused AC and Tool Gateway tests cover opt-in isolation, redaction, JEV endpoint
  pinning, low-confidence fallback, deduplication, managed ownership, and leases.
- Runtime/lifecycle/HER regression tests cover the consumers touched by the change.
- Protected Core and whitespace checks remain required before commit.
- Source verification and live adoption are separate. No HASHI instance is
  restarted by this change; Arale's live canary requires an explicitly authorized
  HASHI2 Function rollout after the offline checks pass.
