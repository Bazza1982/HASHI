# HASHI Agent FYI history - October 11-12, 2026

Moved verbatim from the bounded current FYI on October 13. Current behavior and
authority remain defined by `AGENT_FYI.md` and the owning decisions.

October 12 Call interruption decision: confirmed caller speech may now interrupt
Agent or filler playback. Browser AEC/noise suppression, a frozen playback-time
noise baseline, conservative onset confirmation, and short pre-roll protect echo
and the first syllable. The result is a normal new Call input with admission
priority; it does not itself cancel or pause older PAO work. Older Runs may
continue, but cannot reclaim current speech presentation. Correlated,
content-free lifecycle records now cross renderer, Workbench proxy, Remote and
Call Function, distinguishing caller abort, socket cause, generated 502, TTS
completion, cache publication, retrieval, and response completion. This enables
future attribution but cannot reconstruct the origin of historical disconnects.

October 11 authoritative debug-call baseline: the current
[Call behavior contract](HASHI_CALL_CURRENT_BEHAVIOR.md) supersedes conflicting
historical notes. Phone enters only `/phone`; the video control enters only
`/call` and requests microphone and camera. The transports never fall back to
each other, and an unknown or unavailable Function has no entry. Call is a
basic Function once its selected STT/TTS models and credentials are ready; no
Workbench per-instance allowlist is required. Camera denial or failure degrades
that Call to audio-only. Camera and microphone toggles retain the same Call and
Session. Connected speech stops dial/ringback. Local video is mirrored in a
stable bounded preview while the Agent avatar and identity remain visible.
Captions use one canonical rolling PAO transcript. Public commentary and
complete answer chunks enter the same speech FIFO, with natural playable
chunking and no stage directions or artificial sentence gaps. If a known next
chunk is still synthesizing after the bounded playback grace period, the
approved prepared localized fillers may bridge the silence in order and stop as
soon as answer audio is ready; they never create another caption or Session row.
Context actions sit outside the clipped flexible body, so Stop speaking remains
visible and clickable with video and rolling captions at 390×640. Phone and
Call show a compact live voice-media cost: Phone uses the qualified GPT Live
per-minute rate; Call counts only STT/TTS media. Backend text stays in `/metre`,
Call vision stays excluded, and non-provider figures are visibly estimates.
Provider-declared Phone content-policy closure is terminal: show the explicit
content-filter reason, release the foreground line, and do not reconnect the
same rejected context as though the service were temporarily unavailable.

October 12 Frontend Connector microphone correction: ordinary browser
scheduling can deliver a fourth 32 ms PCM frame before three already-completed
detector replies are reconciled. This is recoverable backpressure, not
microphone loss. Keep at most three worker requests in flight and retain the
burst in an ordered, session-fenced queue bounded to 128 frames (about 4.096
seconds). Drain it as replies arrive and clear it on stop/retry/close. A worker
that fills the bound still fails closed as `queue_stalled`; never silently drop
audio or fall back to amplitude-only detection. The Aptenra-inspired adaptive
noise baseline, local Silero gate, onset pre-roll, 800 ms quiet tail and
continuous-speech bound are unchanged. The full 103-test Call suite and build
pass. Two physical HASHI3 video Calls passed camera, microphone, transcript,
Agent reply, playback, controls and media release, including a hangup during
opening speech. HASHI4 deployment is client/source load only: no implementer
reboot or live HASHI4 Call is authorized.

The same candidate retains the recovered restart work: immutable qualified
generations are reused only after exact runtime/source/artifact checks;
duplicate warm-path verification is skipped without weakening the final
cutover check; the completion banner auto-dismisses and cannot be resurrected
by polling; receipt retry is limited to transient Windows pre-commit sharing
failures. Branch audit found no additional official Call or restart branch to
merge wholesale. Divergent historical branches contain superseded behavior.

October 11 Call hangup lifecycle correction: a failed 195-second HASHI3
replacement exposed a real Call-originated Provider request still running
after hangup. The old assertion preserved uncertain admission correctly but
wrongly detached the Run after PAO returned its identity. Hangup now waits only
until the exact request and Run IDs are known, then uses PAO's existing
targeted cancellation fence. A known Call Run is cancelled immediately. It
never becomes an Agent-wide stop and unrelated foreground/background work
remains protected.

Models, effort and endpointing remain unchanged. Source, artifact, running generation, renderer
adoption, restart timing and physical media acceptance are separate facts.
HASHI4 must not be rebooted for this recovery. Detailed October 10-11 notes
remain in [history](AGENT_FYI_HISTORY_2026-10-11.md).
