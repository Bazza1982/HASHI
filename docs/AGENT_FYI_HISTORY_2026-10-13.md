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

## HASHI4 October 11-12 Call source and live receipts

Moved verbatim from the HASHI4 current FYI during the October 13 bounded-FYI
cleanup. These are historical receipts; current behavior is defined by the
owning Call decision and current FYI.

October 11 authoritative debug-call baseline: the current
[Call behavior contract](HASHI_CALL_CURRENT_BEHAVIOR.md) supersedes conflicting
historical notes. The fixed Phone control owns legacy `/phone`; the fixed video
control owns direct `/call`. They never cross-fallback. A video click immediately
requests microphone and camera; camera denial or failure degrades that direct
Call to audio-only. Camera and microphone toggles retain the same Call and
Session. Connected speech stops dial/ringback. Local video is mirrored in a
stable bounded preview while the Agent avatar and identity remain visible.
Captions use one canonical rolling PAO transcript. Public commentary and
complete answer chunks enter the same speech FIFO, with natural playable
chunking and no stage directions or artificial sentence gaps. If a known next
chunk is still synthesizing after the bounded playback grace period, the
approved prepared localized fillers may bridge the silence in order and stop as
soon as answer audio is ready; they never create another caption or Session row.
An unknown or unavailable Function has no entry. Call is a basic Function once
its selected STT/TTS models and credentials are ready; no Workbench per-instance
allowlist is required. Context actions sit outside the clipped flexible body, so
Stop speaking remains visible and clickable with video and rolling captions at
390×640. Phone and Call show a compact live voice-media cost: Phone uses the
qualified GPT Live per-minute rate; Call counts only STT/TTS media. Backend text
stays in `/metre`, Call vision stays excluded, and non-provider figures are
visibly marked as estimates.
Provider-declared Phone content-policy closure is terminal: show the explicit
content-filter reason, release the foreground line, and do not reconnect the
same rejected context as though the service were temporarily unavailable.
Terminal Phone calls also settle linked pending transport attempts. Startup
reconciles older terminal-call attempts, and Call/Phone admission ignores a
pending attempt whose linked logical call is already terminal, so a cancelled
resume cannot leave both entry icons permanently busy across a reboot.

October 12 HASHI4 Call latency correction: correlated physical-call evidence
shows normal Agent Runs (about 2.8/4.4 s) and STT (about 1.8/2.4 s), while one
browser recording remained open for the 59.8-second safety limit. The approved
Aptenra-inspired local detector plus moving idle-noise baseline had been
replaced by a later HASHI2-only cloud experiment and was never actually
reconnected despite the restoration record. The temporary stable-tail repair
was a different symptom heuristic and is superseded. Frontend Connector now
again warms and reuses the local speech detector, learns its baseline only from
rejected idle frames, and retains onset pre-roll, the normal 800 ms quiet
endpoint, and the continuous-speech bound. Detector failure is explicit and
never falls back to amplitude-only capture. PAO records the once-only audible
opening in the same conversation, delays filler warming until every opening
segment is synthesized, and tells the Agent to compare explicit timestamps
when relative dates or elapsed time matter. The retained acoustic replay rejects
noise and Call tones, preserves normal and quiet speech segments, and processes
95% of detector frames within 1.1 ms. The complete Workbench Call suite passes
95/95, its production build passes, the matching HASHI Function suite passes
158/158, and Protected Core remains clean. This is `debug-call` source and
deterministic-browser validation; running HASHI4 adoption, fresh renderer proof,
and physical acoustic acceptance remain pending the user's lifecycle action.

October 12 remote Call correction: the Shirley trace showed normal HASHI4 Agent
turns but two independent Frontend Connector failures. A remote browser camera
prompt ended with repeated ten-second `AbortError` results; Workbench now clears
the prior Call identity before redial, treats a rejection arriving after hangup
as cancellation, and keeps voice active with an explicit device/permission
notice. The phone's background failure sound came from intermittent read-only
502 responses through `a9_max.local`, which resolved to multiple unrelated
interfaces. The same-host Workbench route is now `127.0.0.1:8771`; its
authenticated probe returned HASHI4/200 and 20/20 Call availability checks
passed. One in-flight automatic-discovery scan initially restored the stale
hostname after the manual correction. Frontend Connector now fences discovery
reconciliation against a connection snapshot changed during its network probe;
the live route was corrected again after that stale scan settled and remained
loopback through later refresh cycles. Speech-asset reads get one bounded retry, and a recoverable playback
failure no longer emits the whole-call failure sound. No mutating operation is
replayed and no HASHI4 lifecycle action was performed. Focused tests, the full
Workbench Call suite, and the production build pass; a refreshed remote page is
still required for physical client acceptance.

October 12 detector scheduling-jitter correction: a fourth 32 ms PCM frame can
arrive before three already-completed local detector replies are reconciled on
the browser main thread. That burst is recoverable backpressure, not microphone
loss. Frontend Connector keeps three requests in flight and drains additional
frames in order from a session-fenced queue bounded to 128 frames (about 4.096
seconds). Stop, retry and close clear the queue. A worker that fills the bound
still fails closed as `queue_stalled`; audio is never silently dropped and the
client never falls back to amplitude-only detection. The Aptenra-inspired
adaptive baseline, local Silero gate, onset pre-roll, 800 ms quiet tail and
Call window design are unchanged. Red/green coverage verifies recovery,
ordering, cross-Call fencing and the true-stall boundary. The complete 103-test
Call suite and production build pass. Two physical HASHI3 video Calls passed
camera, microphone, transcript, Agent reply, playback, controls, repeated
admission and media release. HASHI4 receives only this qualified Workbench
client and this source receipt; the implementer performs no HASHI4 reboot or
live HASHI4 Call. User acceptance remains separate.

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
The Agent must not reboot HASHI4 for this recovery; the user owns the later
adoption reboot. Detailed October 10-11 notes remain in
[history](AGENT_FYI_HISTORY_2026-10-11.md).

HASHI4 source-load receipt: the final HASHI3 `debug-call` candidate was copied
from its previously matched baseline into this `debug-call` branch. At the
source-load checkpoint all 29 changed source, contract, repair, and regression
files matched the verified HASHI3 candidate byte for byte; this instance receipt
is the only later FYI addition. Two HASHI4-specific predecessor documents were
retained as explicitly superseded history. Independent validation passes 205
Call/restart checks and the Protected Core check. This is source qualification,
not running Function adoption; HASHI4 remained running and was not rebooted.
See [final source load](repairs/DEBUG_CALL_HASHI4_FINAL_SOURCE_LOAD_20261011.md).

Non-rebooted H4 live receipt: the new Workbench client passed physical media,
stable mirrored preview, retained avatar, one canonical transcript, natural
spoken projection, ringback termination, reversible controls, and hangup media
release. The old running Function did not publish commentary and released five
final speech segments together; even after the client played all three bridge
fillers, one synthesis wait left 7.367 seconds of silence. These four failed
gates are the exact behavior corrected in the loaded Function source. They are
not adopted until a separately authorized HASHI4 lifecycle action occurs.

October 11 Workbench entry-routing receipt: the video button had been wired to
a video-first launcher that silently fell back to legacy `/phone`, and the local
Workbench allow-list admitted direct Call only for HASHI2. Workbench commit
`d76fbc0949f21e19fb2c13eede70b0398dea77d6` restores strict ownership and
the scoped local configuration now admits HASHI2, HASHI3 and HASHI4. After a
managed Workbench reload, a physical HASHI4 video click opened only `/call`,
showed stable live camera, retained the Agent avatar, used one transcript, and
received an answer that correctly described the camera frame; no Phone audit
was created. A separate Phone click opened only the audio panel and created the
expected legacy attempt/call audit. Both calls ended normally. This correction
performed no HASHI4 lifecycle action.

October 11 final Call action and voice-cost receipt: Workbench behavior commit
`a396e12123a57a9b1576c45f728292f425518f57`, final Workbench source
`57c99bf97c01121463d820ff828a2e1fff94b2ad`, and HASHI4 Function commit
`912d9daf` are loaded on `debug-call`. Independent checks pass 53 focused
Workbench tests, its production build, 249 Call/Phone Function tests, and the
Protected Core check. The managed Workbench service reports the final source,
and the existing Electron window created a fresh renderer session after reload.
A physical direct Call opened a live 1024x576 camera track with mirror
transform, retained the Agent identity and one transcript, and kept all three
controls visible. During real playback, Stop speaking was fully inside the
viewport and the pointer hit-test selected the button; clicking it produced the
`user_stopped` playback receipt and returned the Call to listening. The Call
then ended and released camera and microphone. The Function source is staged for
the user-owned HASHI4 reboot; running-generation adoption and live cost display
remain pending that reboot. See
[source-load receipt](repairs/CALL_ENTRY_COST_HASHI4_SOURCE_LOAD_20261011.md).
