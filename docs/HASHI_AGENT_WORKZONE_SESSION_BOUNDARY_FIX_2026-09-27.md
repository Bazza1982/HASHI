# HASHI Agent Workzone and Session Boundary Fix — 2026-09-27

## Status and scope

- Target: HASHI1 `main` source.
- Approval: the user approved implementation on 2026-09-27 after reviewing the
  proposed `/new` and Session-boundary semantics, and approved busy-time
  Workzone configuration with next-admission effect on 2026-09-29.
- Functional owner: PAO, with PCM consuming the resulting projections.
- Engineering layer: Functions. Protected Core is unchanged.
- Live adoption: not performed; no reboot or restart was authorized.

## Accepted behavior

1. Workzones have one owner/Agent-scoped, revisioned source. Conversation
   Session creation, selection, archive, and context-generation changes do not
   replace it. Each Run freezes its Workzone snapshot at admission.
2. Workzone configuration mutations may commit while Runs are active or queued.
   Already-admitted Runs retain their frozen snapshots; Runs admitted after the
   commit receive the new revision. Runtime, Tool-root, and Engine-session
   activation is deferred to a Run boundary. Explicit `reload`/`reset` remains
   idle-only because it immediately resets the Engine Session.
3. Existing Session Workzone rows remain untouched and inert. No recovery,
   merge, or automatic migration into the Agent profile is performed.
4. Recent-turn injection and saved-memory injection persist independently.
   Session boundaries and runtime reinitialization reapply both choices.
5. Memory+ and Dual Brain are outside this change.
6. A successful `/new`, `/use`, `/fresh`, `/archive`, or `/retry` discards
   unfinished `/long`, Safe Voice, Transfer, and pending Workzone path input.
   A failed provider reset leaves the previous binding/generation and unfinished
   input intact.
7. `/new [title]` stores the complete optional title.

## Failure and transaction boundary

Provider reset is completed before the durable Session binding or generation
is changed. A reset refusal therefore leaves the old channel binding and local
Session state active. Workzone state is no longer part of Session activation,
so it cannot be partially switched. Destructive transient cleanup occurs only
after successful activation. Reply-delivery failure does not roll back a
committed Session change.

A busy-time Workzone mutation commits only the revisioned Agent profile. It
does not overwrite runtime state, Tool roots, or the Workzone snapshot of any
already-admitted Run. The command response states that the saved revision is
for later admissions rather than claiming an immediate switch.

## Red/green evidence

Before implementation, focused tests failed because Workzones disappeared
across Session bindings and the Agent Workzone source did not exist. The new
tests also failed to import the independent recent-turn preference.

After implementation, the focused/component selection covering Session,
Workzone, memory preferences, `/long`, Safe Voice, Transfer, retry, request
admission, backend selection, and locale behavior passed. The deterministic
Core gate reached 722 passing tests; its three generation-qualification tests
correctly rejected the then-uncommitted Function files. That gate must be run
again after the coherent source commit.

Protected-Core and whitespace checks passed before commit. Live Worker adoption
and frontend acceptance remain explicitly unverified.

For the 2026-09-29 busy-time extension, the new focused cases first failed at
the old busy guards. After implementation, 171 Workzone, request-pipeline,
runtime-lifecycle, and locale tests passed, including deferred Engine-session
activation and the retained busy block for explicit reload. The full suite run
from Windows Python 3.14 reached 714 passing and 1 skipped test; its 16 failures
were confined to runtime/process contract gates that require the repository's
mandated Python 3.12.13 environment, so that run is not canonical Core-gate
evidence. No live adoption was attempted.
