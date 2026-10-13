# HASHI4 final debug-call source load — 2026-10-11

Status: source loaded and independently verified on `debug-call`; HASHI4
running Function adoption is intentionally unchanged because no HASHI4 reboot
is authorized.

Owners: PAO Call Function and Frontend Connector

Layers: replaceable Function source and external Workbench; Protected Core is
unchanged.

## Exact source boundary

The previous HASHI4 source receipt corresponded to HASHI3 commit `530629b9`.
For the files changed after that point, 27 of 29 HASHI4 blobs matched that
HASHI3 baseline exactly. The other two were HASHI4-specific historical
documents. They were preserved in explicitly superseded archive files before
the current versions were installed.

The final candidate copies the complete contiguous HASHI3 repair outcome through
`e78620d7`, including restart cache/receipt behavior, incremental Call delivery,
commentary speech, duration-aware natural chunks, one-segment warming, exact
Call-Run cancellation on hangup, and the current behavior contract. All 29
candidate files matched HASHI3 byte for byte at the source-load checkpoint. The
only subsequent candidate-file difference is this HASHI4-specific receipt in
`AGENT_FYI.md`; runtime and contract files remain exact.

The shared Workbench service already serves the clean Workbench `debug-call`
worktree at commit `132ad9c`; the unrelated dirty legacy Workbench checkout was
left untouched.

## Validation

- HASHI3 exact committed candidate: 154/154 Frontend Call checks, 93/93 focused
  Workbench Call checks, production build, and Protected Core check pass.
- HASHI3 physical camera/microphone/speaker acceptance: all 18 observable gates
  pass. The run exercised all three ordered bridge fillers and measured a 256 ms
  maximum eligible silence before the next answer audio.
- HASHI4 source tree, using an independent non-production interpreter: 205/205
  focused Call and restart checks pass.
- HASHI4 Protected Core check passes.

## Adoption boundary

No HASHI4 process was stopped or restarted. Its current running Function
generation therefore remains the pre-load generation. A fresh Workbench
renderer and a non-rebooted live H4 Call are separate frontend/runtime
observations and must be recorded separately. User manual acceptance remains
the final gate.

## Non-rebooted live observation

The first browser attempt encountered a transient automatic Session-selection
race before any Call or media operation. A bounded retry entered the Call and
completed the physical acceptance flow.

Fourteen of eighteen observable gates passed against the old running HASHI4
Function. The verified Workbench client provided the physical camera and
microphone, a stable 356 x 230.25 borderless mirrored preview, retained Agent
avatar, one non-duplicated rolling transcript, natural spoken text, ringback
termination, same-call camera off/on and microphone mute/unmute, and complete
hangup media release.

The four failures were `commentaryObserved`,
`commentaryStreamedBeforeTerminal`, `commentarySpoken`, and
`boundedSentenceGap`. The running Function published no commentary and exposed
all five final speech segments only after terminal completion. Workbench played
all three ordered bridge fillers, but the old selected-voice synthesis path then
left 7.367 seconds before segment 1. Later eligible gaps were 54 ms, 32 ms, and
317 ms. This is direct evidence that the new client is loaded while the old
Function generation still lacks the now-qualified backend changes.

HASHI4 was not rebooted. Full 18/18 H4 acceptance remains pending running
Function adoption and user manual testing.
