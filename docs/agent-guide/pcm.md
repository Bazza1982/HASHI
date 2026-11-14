# Persona, Context and Memory

## Purpose and when to read
Read for PCM authority, context assembly, FYI, HCC, Memory+, forgetting, compaction and context handoff.

## Ownership and layer
PCM owns reference sources, authority, retrieval, assembly and projection in Functions. PAO owns the underlying Session, Run and Workzone control state. HERV3 owns its Engine Session and compaction lifecycle.

## Distinctions
Source, trust assurance, authorization and output destination are separate typed facts. Memory and open items are background, not queued instructions. A documentation revision covers its stated source; it does not prove adoption or grant permissions.

## Authoritative documents
- [FYI catalogue and document retrieval contract](../HASHI_AGENT_FYI_V2.md).
- [PCM design and authority](../HASHI_PCM_SYSTEM_DESIGN.md).
- [PAO-to-PCM boundary](../HASHI_PAO_SYSTEM_DESIGN.md#61-pao-to-pcm).
- [Precise forgetting](../HASHI_PCM_FORGET_2026-10-28.md).
- [Parked conversation recovery](../PARKED_TOPICS.md).
- [Phone context handoff](../HASHI_PHONE_CONTEXT_HANDOFF.md).
- [HERV3 context lifecycle](../HERV3_UPGRADE.md).

## Source and verification
Start with `orchestrator/agent_fyi.py`, `orchestrator/agent_documentation.py`, `orchestrator/pcm.py`, `orchestrator/hcc.py` and the relevant `orchestrator/runtime_pipeline.py` projection. Use `tests/test_agent_fyi.py`, `tests/test_pcm_upgrade_contract.py`, `tests/test_hcc.py` and the owning forgetting tests. Verify current request priority, immutable provenance, unavailable-reference handling and session-scoped primer consumption.

Parked-topic saving uses `orchestrator/flexible_agent_runtime.py` and `orchestrator/parked_topics.py`; verify bounded optional summary enrichment, cancellation and one durable save with `tests/test_park_summary_timeout.py` and the owning guarded-deletion tests.

## Adoption
For FYI details, use `documentation_read` with the source path, section and returned revision. Keep a small reference through compaction/handoff and reread it when needed. Do not reload all guides every turn. Consolidation timestamps alone do not prove new memory candidates or usable retrieval.
