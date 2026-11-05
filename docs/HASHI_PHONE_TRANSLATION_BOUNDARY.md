# Phone translation discovery boundary

Status: current capability boundary; no translation protocol is defined here.

## What this source generation implements

At the catalogue migration baseline (`ea37129b`), HASHI has a [Phone Session/context handoff contract](HASHI_PHONE_CONTEXT_HANDOFF.md) and a [Compute service directory](HASHI_COMPUTE_SERVICE_DIRECTORY.md). The latter defines transcription, TTS and vision service selection. This checkout does not advertise a separate Phone-to-Compute translation service kind, request schema or consumer.

Selecting an output language, voice or transcription language is not evidence of a translation capability. Do not invent an endpoint, infer an API from a guide label, or treat a proposed service as deployed.

## When modifying Phone translation

Read the Phone context handoff contract first to preserve conversation, privacy and authorization boundaries. Then read [Compute compatibility and truthfulness](HASHI_COMPUTE_SERVICE_DIRECTORY.md#compatibility-and-truthfulness) and the actual selected service manifest. If a later qualified generation supplies an authoritative translation contract, use that source/version and update the domain guide. Until then, report the missing contract or capability explicitly before implementing a translation integration.

The FYI refactor supplies reliable discovery and this explicit absence result. It does not implement translation, modify Hashi Compute, or claim that every backend or installed generation has adopted the new documentation reader.
