# HASHI Compute Service Directory

Status: accepted PAO/Function decision; v1 consumer interface implemented on
HASHI3 `debug-deepseek` on 2026-10-17. Runtime adoption and each downstream
consumer migration are separate facts.

## Decision

HASHI treats model execution as a configured compute service, independently of
whether it is on the same machine, on a trusted LAN host, or on the internet.
PAO owns one replaceable Function-layer directory. HERV3, Call, speech, vision,
embedding, and future consumers resolve the same immutable snapshots instead
of copying URLs, model lists, limits, or availability rules.

The directory does not add a new Engine and does not force every service
through chat completions. A service advertises the interfaces it actually
implements, such as OpenAI chat, transcription, speech, native Ollama,
embeddings, realtime audio, expressive speech, voice listing, or direct voice
sample playback.

Protected Core owns none of this behavior. The current human-facing Hashi
Compute page remains independent; HASHI reads the machine manifest.

## Instance configuration

The instance-local `compute_services.json` document is revisioned with
`orchestrator.config_json` and is ignored by Git. Version 1 contains a `nodes`
array. Each node has:

- a stable `id` and optional label;
- `location`: exactly `local`, `lan`, or `internet`;
- a plain origin-only `base_url`;
- a configurable absolute `manifest_path` and optional `health_path`;
- an optional secret reference, never an inline credential; and
- enablement and a bounded discovery timeout.

`local` accepts loopback only. `lan` accepts a literal RFC 1918 IPv4 address
only. `internet` requires HTTPS and rejects local/private addresses. Embedded
credentials, redirects, queries, fragments, and base URL paths are rejected.
The configured location is authoritative: a manifest cannot relabel or widen
the privacy boundary.

Example (illustrative address only):

```json
{
  "version": 1,
  "nodes": [
    {
      "id": "studio-gpu",
      "label": "Studio GPU",
      "location": "lan",
      "base_url": "http://192.168.1.50:19446",
      "manifest_path": "/.well-known/hashi-compute",
      "health_path": "/health",
      "enabled": true
    }
  ]
}
```

## Machine manifest v1

The normative machine-readable shape is
`specs/compute/v1/manifest.schema.json`, identified by:

```json
{
  "schema": "hashi.compute.capabilities",
  "schema_version": 1
}
```

The default discovery path is `/.well-known/hashi-compute`. It is deliberately
separate from `/v1/capabilities`, which may remain a model-specific business
endpoint (for example, voice-model capability lookup). An instance can override
the manifest path for a transitional or differently hosted service; the current
Hashi Compute compatibility entry uses `/api/overview` explicitly.

The manifest declares service version, configuration revision, privacy
location, a lightweight JSON health endpoint, global and per-service
concurrency, current in-flight/busy state, and services/models. Every model
declares:

- input and output modalities;
- typed interfaces, HTTP method, relative path, and streaming modes;
- image count, per-image bytes, total bytes, and accepted formats;
- audio input/output formats;
- tool calling, thinking, and context length; and
- service status and capacity.

An image-capable model must provide positive official image limits and at least
one format. An audio input/output must list its formats. Unknown values for a
modality the model does not support are represented by `null` or an empty list;
HASHI never invents a lower local limit.

Interface paths are always relative to the configured node origin. Absolute
URLs from a manifest are rejected. This intentionally prevents a compromised
or mistaken manifest from redirecting a LAN selection elsewhere. Template
paths may contain named parameters, for example:

```json
{
  "id": "sample",
  "protocol": "hashi.voice.sample",
  "method": "GET",
  "path": "/v1/voices/{voice_id}/sample",
  "streaming": ["none"]
}
```

Consumers render that URL and play the returned service sample directly; they
must not synthesize a local substitute and call it the provider's sample.

## Compatibility and truthfulness

Until Hashi Compute publishes v1, the directory can read its current
`/api/overview` response. The compatibility adapter derives only semantics
explicit in the service kind or advertised endpoint: STT is audio-to-text, TTS
is text-to-audio, vision is text/image-to-text, and Ollama chat is text-to-text.
It accepts declared tool/thinking flags and context length. Missing image
limits, formats, streaming details, and maximum concurrency remain unknown.

The compatibility adapter ignores each service's internal `base_url` (for
example, a server-side `127.0.0.1` worker) and routes only through the
configured public node origin. Compatibility is visible as
`source_format=legacy_overview`; it is not represented as full v1 compliance.

## Resolution and availability

A durable consumer selection is the structured tuple `node_id`, `service_id`,
and optional `model_id`. Model IDs are not embedded in a delimiter string, so
IDs containing slashes or colons remain exact.

Resolution checks the exact reference, requested modalities, protocols,
features, minimum context, privacy location, health, service state, concurrency,
and busy flag. A mismatch fails closed. Capability matching can return an
ordered candidate list, but it never chooses one or crosses a privacy boundary
for the caller. Cloud fallback therefore remains an explicit consumer policy
and user choice.

Discovery is bounded, JSON-only, credential-reference-aware, proxy-independent,
and never follows redirects. Refresh publishes an immutable catalogue
atomically. A transient refresh failure retains the previous snapshot only for
diagnosis and marks it unreachable, so stale health cannot authorize a request.
Deleting or disabling a node removes it on the next refresh.

## Adoption boundary

`orchestrator.compute_services` is the shared consumer interface. Existing
inline Call and Engine endpoint declarations remain supported while their
selections are migrated deliberately. This change does not rewrite saved
profiles, create a Workbench page, start a polling daemon, or adopt a running
Worker by itself. Each consumer migration must preserve its own persistence,
failure, and live-acceptance tests.

Focused validation covers the formal schema, current overview compatibility,
privacy-boundary validation, no-redirect/bounded discovery, exact capability
resolution, busy rejection, refresh failure fencing, direct sample URL
rendering, and revisioned configuration. A live read from Hashi Compute proves
only discovery interoperability, not downstream model execution.
