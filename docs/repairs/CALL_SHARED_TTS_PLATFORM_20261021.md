# Call shared TTS platform — 2026-10-21

Status: implemented candidate; offline validation complete; HASHI3 Function
adoption and live audio verification are recorded separately below.

## Authority and ownership

The user approved immediate implementation and explicitly authorized a HASHI3
hot `/reboot` for testing. This does not authorize HASHI4 changes or a Core
migration.

- Functional owner: PAO.
- Engineering layer: replaceable Functions plus HASHI3 instance configuration.
- Frontend presentation owner: external Workbench Call Window.
- Protected Core: unchanged.

## Problem

`/voice` already used a configuration-owned service/voice catalogue and ordered
fallback implementation. `/call` separately copied TTS targets and a much
smaller voice list, then invoked only the selected target. The duplicated model
made Compute voices incomplete in the Call menu and turned one unavailable
premium backend into a silent call even though compliant fallback services were
available.

## Decision and implementation

Call and `/voice` remain different products and keep separate saved choices.
Their TTS platform is now one shared capability:

1. `voice_catalog.json` is the only service and voice source.
2. Both consumers use the same language routing, protocol adapters, provider
   cooldowns, and ordered fallback execution.
3. New requests and new calls safely observe a valid catalogue revision; an
   active call keeps its starting snapshot.
4. Call filters fallback candidates by its current privacy level before sending
   a request. LAN/local remain eligible; forbidden cloud services are skipped.
5. A successful fallback is fixed for later segments of that call. The next
   call may try the saved preference again.
6. Call snapshots expose selected/effective service labels and reason code.
   Workbench renders a localized downgrade notice outside the transcript.
7. Call Window remains the only Call audio output. Agent Presence remains a
   lifecycle-animation consumer.
8. Exhausting all compliant paths is an explicit speech error, never a silent
   success.

Compatible OpenAI speech services and voices are configuration additions. A new
wire protocol or authentication mechanism still requires one replaceable
adapter implementation before instances can configure that class of service.

## Configuration migration

HASHI3's saved Call profiles retain STT, vision, routes, and per-owner identity.
Copied TTS target rows are removed. The default advanced Compute selection and
the existing Kokoro selection are translated to their shared catalogue service
and voice aliases. The optional OpenRouter Gemini service is enabled through the
instance catalogue with a credential reference, never an inline secret. Every
write uses `orchestrator.config_json` with the document's read revision.

## Evidence

Red evidence before implementation:

- a running `VoiceManager` did not observe a valid catalogue revision change;
- Call could not construct a catalogue-backed configuration;
- Call had no cross-provider fallback or structured downgrade state.

Green offline evidence after implementation:

- shared catalogue refresh and revision fencing;
- exact Call service/voice derivation and complete paginated menus;
- preferred-provider failure to same-node Kokoro and effective-provider lock;
- privacy filtering from cloud candidates to Windows local TTS;
- existing `/voice`, Call service, HTTP, menu, preview, and Workbench controller
  and renderer regressions.

## Adoption and live verification

- HASHI3 source commit: the commit containing this decision record.
- HASHI3 instance configuration migration: pending final publication.
- `/reboot max` Function adoption: pending.
- live Compute premium failure → Kokoro audio and Call Window notice: pending.
- physical speaker playback/user acceptance: pending user test.
- HASHI4 source/runtime: not touched.
