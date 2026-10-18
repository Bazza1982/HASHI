# HERV3 Visual Fallback Decision — 2026-10-18

Status: approved and implemented in HASHI3 source on `debug-issues-oct`;
focused offline validation passed. No Worker reboot, production adoption or
end-to-end frontend acceptance is claimed.

## Ownership and boundary

- PAO owns the Compute Service Directory and configured LAN trust boundary.
- HERV3 owns the per-stage model and media route.
- Frontend Connector renders the typed required warning.
- The implementation is Function/configuration-layer code. Protected Core is
  unchanged.

## Decision

For a canonical request containing an image, resolve exactly one route before
the first Model Provider call:

1. selected exact model, when its verified capability supports image input;
2. first configured and allowed image-capable model under the same Model
   Provider engine;
3. LAN HASHI Compute Vision, resolved from the PAO catalogue; or
4. the existing typed modality failure.

A same-Provider candidate must preserve the stage's tool contract and leave no
required attachment unsupported. Candidate order is the Agent's derived model
order, not a copied global list. The selection is invocation-local and never
writes model configuration.

The LAN route requires input `text+image`, output `text`, protocol
`openai.chat.completions`, and location `lan`. The configured node origin is
authoritative; manifest URLs cannot redirect it. Each image is normalized and
bounded, sent transiently to that exact LAN service, then removed from the
request sent to the selected cloud model. Only the bounded visual description
and original request continue downstream.

## Warning and privacy contract

The fallback emits one required `fallback_warning` per request. It names the
temporary same-Provider model, or states that LAN Vision received the raw image
and the selected cloud model receives derived text only. If warning delivery is
rejected or unavailable, HERV3 makes no downstream cloud call.

“LAN/local” describes the configured client-to-compute boundary. HASHI does not
claim more than the service attests. Raw image bytes are transient and are not
persisted to PCM, the catalogue, model settings or logs.

## Failure and replay rules

- Attachment identity, authorized root, size and digest are checked first.
- Candidate inspection creates no provider request.
- A LAN projection is cached by request and image digest for later HERV3 stages.
- No whole-turn replay is introduced and no uncertain tool effect is repeated.
- Directory, health, capability, response or warning failure retains a typed
  terminal error with a stable diagnostic code.

## Focused proof

Tests cover native routing, ordered same-Provider selection, unchanged default
model, one required warning, one physical Model Provider call, LAN raw-image
removal, LAN projection caching, typed failure preservation, manifest privacy
boundary validation and configured-origin URL construction.
