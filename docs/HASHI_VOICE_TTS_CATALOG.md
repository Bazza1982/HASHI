# HASHI `/voice` TTS catalogue

Status: adopted PAO Function design for ordinary `/voice` synthesis.

## Decision

`/voice` consumes a validated service and voice catalogue. A service entry
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

- PAO owns `/voice` selection, persistence, routing, and failure reporting.
- TTS protocol adapters are replaceable Functions. Built-in operating-system
  adapters remain implementation adapters; remote service instances are data.
- `/call` remains a separate Workbench capability with its own profiles and
  state. It shares only the OpenAI-compatible speech wire validator.
- AgentPresence, Telegram, and Workbench delivery paths are unchanged.
- A catalogue edit does not reboot Core or prove that a running Worker adopted
  it. Adoption and live audio playback are separate facts.

## Instance configuration

Start with `examples/voice_catalog.example.json`, copy it to the repository
root as `voice_catalog.json`, and edit only the instance copy. Existing and
future Agent Workers load that same instance catalogue when they start. Each
Agent still keeps its selected service and voice in its own revisioned
`voice_state.json`.

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

The menu derives its voices and localized labels from the merged catalogue and
paginates large catalogues. Selecting a voice persists the exact service ID,
voice value, and declared per-voice options.

## Validation contract

Focused tests cover catalogue merge and schema rejection, configuration-only
service creation, menu selection and pagination, missing credentials,
no-redirect/no-proxy HTTP requests, bounded audio validation, routing and
fallbacks, and `/call` wire compatibility. Core protection and the applicable
offline product suite remain release gates. A real endpoint test proves only
that endpoint; a Worker reboot and device playback are separate live checks.
