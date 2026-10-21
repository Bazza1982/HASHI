# HASHI shared TTS catalogue

Status: adopted PAO Function design for `/voice` and `/call` synthesis.

## Decision

`/voice` and `/call` consume one validated service and voice catalogue. A service entry
selects a protocol adapter; it is not a Python registration for a vendor. New
OpenAI-compatible local, LAN, or cloud TTS services and their voices are added
through instance configuration, without editing Python.

The shipped defaults live in `orchestrator/voice_catalog.default.json`.
Instance additions and overrides live in the ignored root-level
`voice_catalog.json`. Both use version 1 of the same schema. The instance file
is merged by service ID and voice ID, so it can add, replace, or disable a row
without copying the shipped catalogue.

This removes the former `VOICE_PRESETS` and `PROVIDER_REGISTRY` ownership from
Python. `VoiceManager.VOICE_PRESETS` remains only as a derived compatibility
view for callers and tests. The former ElevenLabs- and Hashi Compute-specific
providers are removed. Hashi Compute, LAN Kokoro, or a vendor gateway is an
ordinary `openai_compatible` service declaration.

## Ownership and boundaries

- PAO owns the shared TTS catalogue, routing, adapters, fallback execution, and
  failure reporting. `/voice` and `/call` remain separate consumers and persist
  separate selections.
- TTS protocol adapters are replaceable Functions. Built-in operating-system
  adapters remain implementation adapters; remote service instances are data.
- `/call` remains a separate Workbench capability with its own profiles,
  segmentation, playback, interruption, and call state. It derives TTS
  services and voices from this catalogue and uses the same language routing,
  adapters, cooldowns, privacy filtering, and ordered fallback execution as
  `/voice`; it does not call the `/voice` command or share Agent voice state.
- Workbench `/voice` and `/say` return the generated Ogg asset to Workbench and,
  when an owner enables a registry-declared mirror, commit those same bytes to
  an independent FC media outbox in the Agent's hidden activity Session. This
  does not create a second Conversation row or couple speech to Telegram or a
  named optional frontend. `/call` keeps its separate lifecycle path.
- Optional mirror publication and delivery are fail-open: setup failure cannot
  prevent Workbench from receiving the generated speech asset. Workbench's
  ordinary reply player remains non-autoplay, so an enabled local presentation
  frontend is the sole automatic speaker on the same machine.
- A valid catalogue revision is loaded for new `/voice` requests and new calls
  without a Core restart. An active call retains the immutable catalogue
  snapshot with which it started. Configuration adoption and physical audio
  playback remain separate facts.

## Instance configuration

Start with `examples/voice_catalog.example.json`, copy it to the repository
root as `voice_catalog.json`, and edit only the instance copy. Existing Agent
Workers safely refresh a changed valid revision before a new synthesis; new
calls freeze the revision they start with. Each Agent still keeps its `/voice`
selection in its own revisioned `voice_state.json`, while Call stores its
selection in the separate Call profile.

Service fields:

- `id`: stable lower-case identifier.
- `adapter`: protocol adapter. Remote services use `openai_compatible`.
- `label` and `menu_label`: operator and compact UI names.
- `location`: `local`, `lan`, or `cloud`.
- `base_url`: the API root before `/audio/speech`.
- `model` and `audio_format`: protocol values; formats are `wav`, `mp3`, or
  signed 16-bit mono `pcm`.
- `credential_ref`: optional `env://NAME` or `secrets://name` reference.
- `request_options`: bounded extra OpenAI-compatible request fields.
- `fallbacks`: ordered service IDs. Missing or inactive platform services are
  ignored at runtime.
- `style_support`, `timeout_seconds`, `max_response_bytes`, and
  `pcm_sample_rate_hz`: declared capability and request bounds.

Voice fields:

- `id`: stable menu/persistence alias, limited to 48 ASCII characters so the
  Telegram callback remains within its transport limit.
- `service` and `voice_name`: catalogue service ID and protocol voice value.
- `label`, optional `display_names`, `language`, optional `language_labels`,
  `locale`, and `gender`: rendered metadata.
- `provider_options`: bounded per-voice request options.
- `other_languages_unsupported`: defaults to true and informs safe routing.
- `platforms`, `enabled`: optional visibility controls for services or voices.

An ElevenLabs deployment does not regain a vendor module. It is configured
through an OpenAI-compatible endpoint or gateway, with its key referenced from
instance secrets. The same rule applies to other vendors.

## Safety and failure contract

The complete default and instance documents are validated before use. Unknown
fields, duplicate IDs, unsupported adapters, unsafe endpoint/location pairs,
inline credentials, reserved request-field overrides, path escapes, excessive
sizes, and dangling references are rejected. Invalid instance JSON is never
rewritten. The Agent Worker can remain available, but `/voice` changes and
synthesis fail closed until the catalogue is repaired.

Remote requests disable environment proxies and redirects, use bounded time
and response sizes, and validate the returned audio signature before local OGG
conversion. Saved Agent state contains service and voice identifiers, not
endpoint URLs or credentials. Provider failures remain explicit and use the
existing bounded fallback and audit path.

The `/voice` and `/call` menus derive their voices and localized labels from the
merged catalogue and paginate large catalogues. Selecting a voice persists the
exact service ID, voice alias, and declared per-voice options in the owning
consumer only.

Call evaluates the selected service followed by its configured fallback IDs.
Cloud candidates are skipped when the current privacy level disallows them.
After one segment falls back, the call keeps that effective service for later
segments; it does not oscillate between voices. The Call snapshot exposes both
selected and effective service labels so the initiating Call Window can render
one explicit downgrade notice. Agent Presence receives lifecycle events only
and never becomes a second Call audio output.

## Validation contract

Focused tests cover catalogue merge and schema rejection, configuration-only
service creation, menu selection and pagination, missing credentials,
no-redirect/no-proxy HTTP requests, bounded audio validation, routing and
fallbacks, Call fallback locking and privacy filtering, structured Call Window
degradation, owner-mirror byte identity and failure isolation, and wire
compatibility. Core protection and the applicable offline product suite remain
release gates. A real endpoint test proves only that endpoint; Function adoption
and device playback are separate live checks.
