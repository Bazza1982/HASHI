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
