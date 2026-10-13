# HASHI Phone optional local cascade: Gate 1

Status: historical experimental Gate 1 checkpoint, originally developed on HASHI2 `feat/phone-local-cascade`; no live adoption or production qualification.

Owner: Frontend Connector Functions and the isolated local voice Worker. Protected Core is unchanged. The Workbench browser codec lives on its own experimental branch. The existing OpenAI Phone provider remains the default qualified provider.

## Gate 1 decision

The `cascade-v1` media path uses a real WebRTC SDP answer, Opus audio tracks, and a browser DataChannel `session.started` event. The local Worker needs an explicitly configured nonempty `CASCADE_WORKER_TOKEN`; the backend needs the same token and an explicit cascade enablement or worker URL. No built-in token is accepted, and the sideband connection uses a bearer header rather than a URL query secret.

Incoming audio energy triggers barge-in after two active frames and ends the speech interval after fifteen quiet frames. Barge-in increments the output generation, drops queued old audio, and sends `output.gate.closed` to the browser, which immediately stops local playback. A later output may reopen playback. These thresholds are experimental and must be tuned against real microphones.

Sideband events produced while the WebSocket is disconnected are replayed in order on reattach, up to 512 buffered events. Overflow rejects reattach instead of silently claiming complete recovery. This is process-memory gap buffering, not durable acknowledged delivery across a Worker crash. HASHI's existing provider-event staging remains the durable boundary for events it receives.

Internal `instructions`, `commentary`, and `thinking` updates are retained as context only. The Worker never speaks their raw contents. The HTTP `/speak` and `/simulate_speech` routes are test scaffolding for output and transcript plumbing.

## What remains for later gates

This Worker does not yet contain real speech recognition, reasoning, or speech synthesis. It must not be presented as a usable local voice assistant. The next gate must connect those engines, prove an actual spoken turn and a safe proactive opening, consume background results as context, and verify cancellation against generated audio. Production qualification also needs durable recovery semantics and a real Workbench microphone and speaker acceptance test. Source tests and an in-process aiortc call do not prove live device adoption.

The subsequent experimental source checkpoint is recorded in [Gate 2](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).
