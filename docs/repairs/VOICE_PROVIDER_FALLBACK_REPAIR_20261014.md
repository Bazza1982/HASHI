# Voice provider fallback and useful failure reporting

Owner: Frontend Connector. Engineering layers: Workbench speech proxy/UI and
replaceable voice Functions with an isolated TTS runtime. No Core migration.

## Authorization and cause

On October 14 the user approved solution B on HASHI3, the `debug-voice` branch,
and a HASHI3 hot reboot for validation. HASHI4 is outside this repair scope.

The failure had two independent causes:

- Workbench sent `message_read_aloud`, but its server accepted only `say` and
  `voice_reply`, so a manual Read Aloud request could be rejected before HASHI.
- The isolated Edge TTS runtime still used `edge-tts` 7.2.8. A real request now
  receives HTTP 403 from the Edge speech protocol. Retrying that same rejected
  protocol cannot recover it. Edge browser health is unrelated to this client
  protocol.

The prior implementation also retried only the selected provider. It never
changed to Windows TTS and reduced downstream failures to generic UI text.

## Decision

- Pin the isolated Edge client to 7.3.1; do not install or upgrade dependencies
  in the Core interpreter.
- If Edge fails on Windows, try Windows SAPI with language-based voice selection
  and without carrying an Edge neural voice name into SAPI. A short in-memory
  cooldown avoids repeatedly calling a provider that has just failed.
- Preserve the provider, fallback provider, stable reason code and safe reason
  on successful fallback. If every provider fails, return each bounded cause.
- Workbench accepts manual read-aloud requests and renders the concrete safe
  cause. URLs, tokens and signatures are removed at both Function and frontend
  boundaries. An HTTP success envelope containing `ok: false` is still failure.

Fallback changes only synthesis delivery. It does not change opt-in automatic
narration, the canonical message, Agent voice preferences, or Core.

## Verification

- Pre-fix real Edge 7.2.8: WebSocket handshake rejected with HTTP 403.
- Isolated Edge 7.3.1: real synthesis returned nonempty audio; the full HASHI
  Edge provider produced a nonempty OGG asset.
- Deterministic Edge HTTP 403 followed by real Windows SAPI and conversion:
  Windows returned a nonempty OGG asset with the Edge failure metadata retained.
- Focused HASHI and Workbench tests cover manual read aloud, typed causes,
  secret removal, provider fallback, both-provider failure and UI presentation.

Source validation, commit, hot adoption and user hearing acceptance remain
separate facts. This record will be updated after the authorized HASHI3 reboot.
