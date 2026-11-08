# Independent simultaneous interpretation

Status: accepted design; HASHI6 Functions implementation and qualification in progress.

## Product and ownership

`/translate` owns the simultaneous interpretation entry. `/phone` owns an Agent
conversation. PAO owns interpretation preferences, logical calls, admission,
frozen selection, recovery and durable subtitles. The Frontend Connector owns
authenticated command/API transport. External clients own their microphone,
speaker, buttons and views. Compute owns translation inference and speech.
All HASHI changes belong to Functions; no protected Core migration is required.

Interpretation has independent `translate_state.json` preferences in the Agent
workspace, saved through the existing revisioned JSON owner. It does not mutate
`phone_state.json`. Directions are derived from the qualified translation model
catalogue. Existing Phone interpretation preferences remain readable; new Phone
selection directs the user to `/translate` and does not silently rewrite files.

## Entry and interaction

The slash menu shows the saved direction and the audio/privacy boundaries.
`/translate direction en-zh|zh-en` persists the direction for the next session.
`/translate status` reads it; `/translate reset` resets only translation settings.
Capture begins through the external client's simultaneous interpretation action.

The external client exposes a dedicated top-bar interpretation button and a
“Start simultaneous interpretation” item in its add menu. Both open the same
interpretation view, with direction selection, translated subtitles, microphone
mute, connection status and end action. Conversation and interpretation use the
same media implementation and controls but keep separate entry identities and
configuration. The Workbench direction dialog uses this same authenticated
command menu and its opaque revision-bound buttons; it never stores a second
direction preference or copies the model catalogue. A failed or uncertain
settings write disables capture until an explicit canonical refresh.
Changing direction does not silently change an active call.

One owner may have only one foreground media call across Phone and Translate.
Switching product requires ending the current call and receiving close
confirmation before starting the other. Unknown close outcomes remain visible
and cannot be treated as released resources.

## Data boundary

Interpretation sends no Agent Persona, memory, conversation history, task results
or tool definitions. Spoken instructions are content to interpret. Proactive
greetings and delegation are unavailable. Background chat remains in its normal
destination and is not queued to the translation provider. Translation fragments
remain durable live-session subtitles for recovery; ending interpretation does
not automatically publish a Phone transcript message to ordinary chat.
Source subtitles are not invented when the model only emits target subtitles.

## Shared transport contract

The authenticated application root is `/api/v1/translate`. Its context, calls,
attempts, resume, events, observations and controls use the same versioned wire
contract as `/api/v1/live-voice`. One LiveVoiceManager owns both route families,
including foreground exclusion, idempotency and provider close confirmation.
The route derives `entrypoint=translate`; the client cannot override it. Frozen
public/selection snapshots retain this entry identity, and cross-entry call or
attempt access is rejected. The compatibility `phone` snapshot field and
`phone_revision` request field retain their existing wire names. Recovery validates
its saved provider and direction without consulting next-call preferences, even
when those preferences now select another product.

WebRTC audio, `oai-events`, media heartbeat, mute and close semantics are shared.
The Compute transport and discovery remain governed by
[the interpretation adapter contract](HASHI_PHONE_INTERPRETATION.md).

## Implementation and qualification

The preference owner is `orchestrator/translate_manager.py`; it reuses the
revisioned persistence operations of PhoneManager. Model facts live once in
`orchestrator/phone_catalog.py`. Slash rendering belongs to
`orchestrator/translate_settings.py`; registration derives from command metadata.
API admission and durable lifecycle remain in `frontend_live_voice`.

Focused verification covers independent preferences, frozen direction, zero PCM
input, route-derived entry identity, cross-entry controls, chat inbox isolation,
no automatic chat projection, and conversational Phone regression. Live
qualification must use the assigned non-production instance and external client,
both directions, subtitles/audio, mute, close confirmation, reconnect, product
switching and final released capacity. Synthetic audio, actual frontend delivery
and physical microphone/speaker observations are recorded separately.

Source implementation, focused tests, committed artifact, running adoption and
external-client qualification are separate evidence. No production adoption is
implied by this document.
