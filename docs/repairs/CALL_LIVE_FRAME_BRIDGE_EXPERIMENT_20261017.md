# Call live-frame bridge experiment — 2026-10-17

## Approval and scope

The user directed an immediate experiment on HASHI3 `debug-deepseek`: retain
continuous LAN MiniCPM-V interpretation, raise camera sampling to one or two
frames per second, and attach up to ten recent raw frames to every caller turn
when the selected main model can read images. HASHI4 is out of scope.

Owners are Frontend Connector / PAO Call Function for collection, sampling and
volatile transport, and HERV3 provider routing for exact-model capability
gating. This is a replaceable Functions change. Protected Core is unchanged.

## Implemented candidate

- Camera-on policy targets 500 ms between frames and 120 local observations per
  minute. Continuous observation remains latest-frame-wins with no inference
  backlog.
- The existing Workbench capture loop already consumes the server-provided
  interval without imposing a one-second floor, so no Frontend source change is
  required for this experiment.
- A ten-second, bounded in-memory ring stores accepted JPEG frames. Each caller
  turn freezes and evenly samples at most ten frames, oldest to newest and
  including the newest usable frame.
- The exact primary answer backend receives labelled `image_url` parts only if
  it declares image/data-URL input. Text-only backends receive no raw frames.
- The MiniCPM-V observation remains auxiliary context. A question-conditioned
  turn image is not overwritten by the generic continuous observation.
- Frame payloads are stripped before PAO Session admission and canonical audit.
  Camera-off or hangup clears the live ring for future input; an already frozen
  turn snapshot remains scoped to that accepted Run and is cleared on terminal
  cleanup or Worker shutdown.
- DeepSeek request validation continues to use its documented provider limits;
  the bridge adds no smaller provider-facing image budget.

## Evidence separation

Focused red evidence first failed because the transient-frame owner did not yet
exist. After implementation, the focused Call, PAO persistence, HERV3 routing,
and DeepSeek structured-image tests passed. Closeout then passed 385 related
Call/provider regressions, Python compilation, changed-file lint, whitespace
validation, and the protected-Core check.

This record proves source behavior only. HASHI3 Worker adoption and a physical
camera conversation remain separate and are pending until the authorized
runtime is reloaded and the user performs the test. HASHI4 was not changed or
restarted.

## Subsequent default-gate compatibility correction

The later full default gate exercised a provider-wire fixture that deliberately
borrows OpenRouter's message builder without constructing a full adapter. The
live-frame change had looked up the optional frame helper even when the turn
contained no transient frames, so that fixture failed before rendering its
ordinary text messages. The builder now returns the unchanged messages first
when there are no transient frames and requires the helper only for an actual
frame-bearing turn. This changes no provider request or capability decision;
the previously failing test and the live-frame multimodal regressions pass.

## Main promotion decision

On 2026-10-17, after continued HASHI3 Call testing and the paired Workbench
echo-stability correction, the user explicitly approved moving the latest Call
upgrade and all associated HASHI3 candidate changes into GitHub `main`. The
candidate first incorporated the two newer remote-main commits without conflict.
Publication approval is recorded separately from runtime adoption: it does not
claim that the raw live-frame path received a physical camera acceptance that
was not observed.
