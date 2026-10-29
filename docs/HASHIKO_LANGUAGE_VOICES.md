# Hashiko language voices — HASHI3, 2026-10-23

Owner: PAO shared speech Functions (`voice_catalog`, `tts_routing`); Call
consumes the frozen catalogue. Instance selection belongs to HASHI3, never AP.

The requested dedicated Kokoro 82M policy is Chinese `zf_027` (素人1),
English `af_heart`, Japanese `jf_alpha`. A separate catalogue service
keeps other Agents' existing voice choices intact. Current character binding
is Kasumi; its /voice state and current owner/Agent Call profile are prepared
together, with an Agent default for future callers. Existing /voice enable
and reply mode are preserved. VideoTTS remains disabled pending the separate
Call Window integration; its prepared voice source is the Call TTS profile.

Live service evidence: Chinese and Japanese returned valid nonempty WAV;
English was absent from the service inventory and returned HTTP 400
`voice_not_found`. The requested English choice is retained. No automatic
replacement voice is configured on this character service.

Focused red/green evidence covers exact voices across /voice and sequential
Call segments (including Japanese followed by Chinese), alias validation,
Agent default/owner selection precedence, and the actual external AV
coordinator's VideoTTS request. The AP reference remains unchanged.

The active Kasumi Run prevents shared `/reboot max` under the supported
activity preflight. Do not bypass it or cold restart Core. Prepared instance
files are kept in Kasumi's ignored evidence directory until compatible shared
Functions are adopted. The in-use catalogue must remain readable by the old
generation. This is staged configuration, not a claim of live voice adoption.

Evidence: `workspaces/kasumi/evidence/sunrise_voice_20261023`. The final
receipt records the local source commit, checks and configuration revisions.
