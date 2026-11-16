# Media service setup

Owner: Frontend Connector Functions. PAO retains Session/admission authority;
the existing private secret document, Call configuration and Compute directory
retain configuration ownership. Protected Core is unchanged.

## Reachability and starts

The three external frontend entrances are /phone, /call and /translate.
An entrance opens its status/configuration surface even when not configured,
disabled, busy, unresolved or last observed to fail. An active entrance returns
to its own call. There is no separate "unverified" action.
Only explicit Start performs existing foreground/session/media admission.
A prior connectivity test is optional and never a start prerequisite.

## Authenticated setup operation

POST /api/v1/media-services/operation reuses existing Backend API/Remote
authentication and the canonical owner, Agent, active Conversation Session and
context generation. No new role, pairing mechanism or client-provided owner is
introduced. Instance configuration saves additionally require the existing
admin authority. JSON bodies are limited to 16 KiB.

The fixed operations are status, save_key, save_binding and test. Unknown
fields, invalid generations and entrypoints are rejected. Reads never create
configuration. Responses never contain a credential, credential reference,
private configuration path or credential digest.

Status returns an opaque, process-local revision bound to owner/Agent/Session,
entrypoint, selected configuration and the retained source revisions. Saving
or testing a stale view fails; it is not silently refreshed and replayed.

## Configuration and persistence

- Provider/model/voice/direction choices remain existing revision-bound command
  UI operations. Clients do not copy catalogues or write preference files.
- save_key can update only the private secret reference of an exact selected
  service. OpenAI Phone uses its existing instance key; Call uses selected
  targets and the authoritative TTS catalogue; Translate uses the selected
  Compute node reference. Environment/file/vault references stay externally
  managed. Keys are shared by existing consumers of that instance reference.
- Keys use the existing config_json private atomic writer and retained read
  revision. Unrelated secrets are preserved. A conflict is a rejection; a
  committed durability failure is an unknown outcome requiring readback.
- Voice consumers read the current canonical secret document; they do not keep
  another writable credential store. A corrupt document never becomes a
  writable fallback.
- save_binding delegates to ComputeServicesConfig. The optional
  bindings.translate object in compute_services.json contains only node_id and
  service_id. Its node declaration and reference publish in one revision-checked
  write; unrelated nodes and bindings remain. A configured reference takes
  precedence over legacy environment node/service selection.
- Translation setup admits only existing validated local/LAN node origins and
  discovery paths. A new interpretation node defaults to the qualified
  /v1/translation/manifest path. Live call start/resume still accepts no provider
  URL. Its resolved provider Session endpoint remains frozen.
- Credential/connection saves reject during the owner's foreground media call.
  Configuration remains viewable. Browser values are transient and cleared on
  save, service switch or scope departure; they never enter chat/storage/logs.

## Connectivity is limited evidence

test sends no speech, Persona, conversation history, inference or PAO Run.
It does not capture a microphone, create a provider voice Session, or record a
successful voice verification. Explicit configuration repair or a successful API
probe discards only the matching cached provider failure; the next attempt is
permitted and remains unverified until an actual voice session succeeds.

OpenAI checks the selected model API. OpenRouter Call checks its key API;
other compatible HTTP targets check their model-list API. Requests use selected
configuration, existing credentials, a ten-second timeout and no redirects.
Missing keys never pass merely because a public endpoint responds.
Native services without a qualified probe report test-not-supported.

Translate checks the exact Compute node/service/model manifest and required
audio/text modalities and Phone WebRTC/sideband protocols, plus configured
health. It permits no cloud fallback or substitution.

Results identify API access or Compute discovery, never full call/device
qualification. Provider credential rejection is distinct from frontend/Remote
connection authentication. A failed test does not disable a usable configured
entrance or require a test before a direct attempt.

## Verification and adoption

Focused tests: test_frontend_media_setup.py, test_frontend_media_setup_http.py,
the existing Compute/Translate suites, frontend_call HTTP/shared-TTS and live
voice integration suites. They cover authority, source revisions, private
publication, uncertain outcomes, real bounded HTTP probes and existing media
behavior. External UI/browser and real service/device adoption are separate
evidence; a source commit is not a running Function-generation adoption.
