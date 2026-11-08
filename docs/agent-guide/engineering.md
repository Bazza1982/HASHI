# Engineering, configuration and verification

## Purpose and when to read
Read this before changing source, persisted configuration, packaging, runtime adoption or release checks.

## Ownership and layer
Choose the owning PCM, PAO, HERV3 or Frontend Connector boundary. Ordinary product work belongs in replaceable Functions; platform and instance configuration remain separate. Core is not a feature extension point.

## Distinctions
A Git checkout, qualified artifact, running process and live acceptance are different evidence. A successful file save can have a durability warning; a stale revision cannot be retried as a new edit. Fingerprint records evidence and never replays effects.

## Authoritative documents
- [Architecture](../../ARCHITECTURE.md) and [engineering rules](../../AGENTS.md).
- [Layered runtime boundaries](../HASHI_LAYERED_RUNTIME_BOUNDARIES.md).
- [Core protection](../HASHI_CORE_PROTECTION_HARDENING_2026-09-13.md).
- [Core/Functions runtime contract](../HASHI_PYTHON_RUNTIME_COMPATIBILITY.md), including the approved v5 separation candidate, original-dependency hold point and separate adoption gates.
- [v5 upgrade overview](../RELEASE_NOTES_v5.0.0-alpha.1.md), [versioning](../HASHI_VERSIONING_POLICY.md) and [release checklist](../RELEASE_CHECKLIST.md). The overview summarizes changes; owning contracts and exact-candidate evidence retain authority.
- [Configuration persistence](../HASHI_CONFIGURATION_PERSISTENCE.md).
- [Testing policy](../TESTING_POLICY.md) and [Fingerprint](../HASHI_FINGERPRINT.md).

## Source and verification
Start with `orchestrator/runtime_contract.py` for the canonical protected manifest, `orchestrator/config_json.py` for persisted edits, and `orchestrator/function_generation.py` for artifact closure. Do not edit protected paths. Use the Core check script and the owning focused tests; configuration changes use `tests/test_config_json.py`. Runtime/ABI changes follow the broader checks in the testing policy.

## Core dependency generation
The [Core 5 standard dependency candidate](../HASHI_CORE_DEPENDENCY_GENERATION_5.md)
is limited to source preparation, offline validation and independent review.
Python and public APIs are unchanged; its standard dependency fingerprint changes.
Product major-version and declared standard-lock changes require the maintainer
approval label, a major reset and matching independent review seals even when
protected source is unchanged. The candidate cannot be hot adopted or installed
into a running Core interpreter. Deployment requires separate authorization.

## Adoption
Use the actual owner-scoped adoption contract and current user authorization. Documentation changes do not prove running code changed. Preserve other checkout work and review the complete difference. Historical repair notes are evidence of their stated version, not a competing current architecture.
