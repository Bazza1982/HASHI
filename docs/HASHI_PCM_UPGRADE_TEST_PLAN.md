# HASHI PCM Upgrade Test Plan

*Accepted assertion migration and minimum acceptance gate for the next HASHI PCM upgrade*

| **Document information** | **Value** |
| ------------------------ | --------- |
| Status | Accepted implementation test contract |
| Accepted | 26 August 2026 |
| Governing design | [HASHI_PCM_SYSTEM_DESIGN.md](HASHI_PCM_SYSTEM_DESIGN.md) |
| Scope | Backend-neutral HASHI Persona-Context-Memory infrastructure; HER-V2 is one consumer, not the owner of this contract |

## 1. Purpose

This plan records which existing assertions become invalid under the accepted
PCM design, which existing tests remain valuable, and the smallest additional
contract suite needed before the upgrade can be declared complete.

Tests should be replaced in the same change that retires an old assertion. No
test should disappear without equivalent or stronger coverage of the accepted
product contract. Every materially new or rewritten behavioural test must also
follow the red/green failure-proof rule in [TESTING_POLICY.md](TESTING_POLICY.md).

## 2. Assertions to retire or replace

| **Old assertion** | **Required action** | **Replacement assertion** |
| ----------------- | ------------------- | ------------------------- |
| A HASHI Agent may select `agent.md`, `AGENT.md` or an arbitrary `system_md` path as its persona source. | Replace the assertion and its parameterisation. | HASHI reads exactly the lower-case `agent.md` in the Agent workspace. |
| Missing, empty or unreadable identity files are reported only as `system_md_*` source failures. | Replace. | The PCM document validator reports structured `agent.md` validation failures. |
| Substantive text outside recognised PCM blocks can be silently ignored while a valid `[persona]` block is extracted. | Replace. | Substantive unmarked content invalidates the complete PCM document. |
| Missing, empty, duplicate or ambiguous persona blocks select a valid minimal runtime fallback. | Replace as a configuration contract. | Invalid PCM fails closed during validation. A renderer fallback may remain only as defence in depth and must not make the Agent configuration valid. |
| Normal `agents.json` configuration persists a `system_md` field. | Remove from normal fixtures and assertions. | A one-time migration may accept legacy `system_md` as input, then removes it after producing and validating canonical `agent.md`. |
| HASHI import/export may create an unstructured Markdown identity such as `# Zelda`. | Replace. | HASHI-side import/export produces or consumes valid `[persona]`, `[sys]` and optional `[memory]` blocks. |
| Recent context is measured as backend-specific counts of individual message rows, such as 4, 8 or 10 messages. | Replace. | Recent history is measured as at most ten completed user-assistant exchanges. |
| A Fixed incremental request contains only the current user text and no PCM background. | Replace. | Fixed mode omits repeated recent history after bootstrap but sends current delta PCM on every external user turn. |
| Flat string position alone establishes PCM authority. | Replace. | A typed authority envelope separates system instructions, the current user request, memory, runtime context and persona; adapter rendering must preserve those semantics. |
| A history cap may retain older chats while clipping or discarding newer chats. | Replace wherever present. | Capacity pruning removes the oldest complete exchanges first, preserves newer complete exchanges, protects the current request and higher-authority PCM, and audits omissions. |

### 2.1 Exact tests that need semantic rewrites

The following current tests contain the clearest obsolete assertions and should
be renamed or rewritten rather than merely patched until green:

- `test_configured_system_md_is_the_only_persona_source`
- `test_missing_empty_and_invalid_utf8_sources_fail_closed_without_creation`
- `test_v2_packaging_source_exposes_only_the_explicit_persona_block`
- `test_invalid_v2_persona_blocks_select_minimal_fallback`
- `test_bridge_context_assembler_splits_turn_and_saved_memory_flags`
- `test_managed_prompt_orders_policy_then_old_memory_then_recent_then_request`
- `test_context_profiles_separate_persistent_cli_and_stateless_api_memory`
- `test_incremental_memory_plus_prompt_keeps_authoritative_request_marker_without_background`
- `test_fixed_session_backend_uses_incremental_prompt`
- HASHI-side transfer assertions that expect a persisted `system_md` field or an unstructured identity document

Configuration tests that merely use `system_md` as unrelated fixture data
should be mechanically migrated to canonical `agent.md`. Legacy `system_md`
must remain only in the dedicated one-time migration tests.

External products retain their own conventions. In particular, Hermes and
Nagare tests that intentionally exercise an external `AGENT.md` format must not
be changed by a repository-wide filename replacement.

## 3. Existing assertions to retain

The upgrade reuses, rather than replaces, the following coverage:

- completed-exchange ledger persistence, transcript reconciliation and fresh-boundary filtering;
- global `/sys` state isolation, concurrency and precedence over Agent-local `/sys`;
- Workzone on/off behaviour, effective working directory, Tool Registry `access_root` and Gateway admission checks;
- deterministic Memory+ date rollover, bounded carryover, archive preservation and concurrent update serialisation;
- Tool audit redaction and truncation in sanitised operational views;
- HER-V2 sanitised reasoning/audit views and explicit reasoning-unavailable records;
- Enterprise audit hash chains, tamper detection and WORM anchors;
- Tool Gateway/MCP schema, permission and delegated-tool enforcement;
- context compaction tests that retain raw turns while changing only the active context view; and
- reset/wipe tests that remove ordinary transcript, working memory or session state.

Sanitised operational logs and canonical raw evidence are different layers.
Existing redaction tests must not be inverted to require secrets in normal
logs. The new canonical evidence path must preserve complete values under
separate access controls while the existing operational view remains safe.

Likewise, ordinary transcripts may still be cleared. The new canonical raw
audit store must survive that cleanup.

## 4. Minimum PCM acceptance suite

The minimum new gate is 24 backend-neutral contract-test functions. A function
may be parameterised across invalid forms, lifecycle operations or backend
families. Parameterisation must not be replaced by duplicated test bodies.

### 4.1 PCM document and migration — 4 tests

1. A valid lower-case `agent.md` isolates exactly one `[persona]`, one `[sys]`
   and zero or one `[memory]` block without cross-contamination.
2. A parameterised invalid-document test rejects missing required blocks,
   duplicate or mismatched markers, empty required blocks, substantive unmarked
   content and invalid UTF-8.
3. HASHI resolves only the exact lower-case workspace `agent.md`; uppercase and
   arbitrary custom paths do not become alternative PCM sources.
4. Legacy `system_md` migration is validated, atomic and idempotent. Ambiguous,
   conflicting or invalid input leaves the old configuration recoverable and
   never commits a partial migration.

### 4.2 Assembly, history and time — 7 tests

5. The adapter-neutral authority envelope preserves permanent `[sys]`, global
   `/sys`, local `/sys`, current-user, persona, memory and context semantics.
6. A Fixed backend bootstrap receives the newest ten completed exchanges once.
7. A continuing Fixed session receives complete delta PCM on every external
   user turn without replaying recent history.
8. A Flex backend receives the current request and newest ten completed
   exchanges on every external turn. Incomplete exchanges are excluded and
   each included exchange has sequence and timestamp metadata.
9. When a non-HER or handoff budget is exceeded, HASHI removes the oldest whole
   exchanges first, preserves newer whole exchanges, protects the current
   request and higher-authority PCM, and records an omission audit event.
10. `/backend +` continuation and `/handoff` each deliver the selected context
    to the Fixed backend exactly once rather than only preparing a file or
    pending prompt.
11. Every top-level external user request contains date, seconds, time-zone name
    and UTC offset. Internal HER-V2 stages do not receive an independently
    fabricated top-level time injection.

History pruning is exchange-atomic. If all older exchanges have been removed
and one remaining historical exchange still cannot fit, that historical
exchange is omitted whole and audited; it is not clipped into a partial chat.
The current user request is never treated as historical content and remains
protected.

### 4.3 Skills, Tools and Fixed CLI access — 3 tests

12. The Skills catalogue lists only Skills the Agent can invoke in the current
    request scope, does not inline full `SKILL.md` instructions, and includes
    local memory search only when that Skill is authorised and callable.
13. The Tools catalogue exactly matches definitions remaining after Agent,
    backend, stage, permission and Tool Registry/Gateway filtering. Catalogue
    metadata alone never authorises execution.
14. Each supported Fixed CLI can invoke an advertised HASHI Tool through MCP or
    its equivalent native bridge. The catalogue, Gateway inventory, Workzone
    and effective admission scope agree; a capability is absent until the
    bridge is both available and authorised.

### 4.4 Canonical raw audit evidence — 5 tests

15. One end-to-end request produces a correlated raw evidence chain covering
    user/model chat, provider activity, complete Tool identifiers, names,
    arguments and results, operation/lifecycle events and delivery events.
16. Provider-exposed reasoning is preserved exactly. When reasoning is not
    exposed, HASHI records explicit unavailability and never reconstructs it.
17. Large or binary evidence is retained as an immutable content-addressed
    artifact with a verifiable digest and provenance. The corresponding
    operational view remains redacted and bounded while the canonical evidence
    remains lossless.
18. A lifecycle-parameterised test proves that canonical evidence survives
    `/reset`, `/new`, backend switching, process reload, ordinary `/wipe` and
    workspace maintenance. No automatic TTL or retention pruner may remove it.
19. Unauthorised raw-evidence reads are denied; configured encryption-at-rest
    prevents plaintext storage; and only a separately scoped, explicitly
    confirmed audit-wipe operation deletes its exact target and records that
    destructive action.

Indefinite retention cannot be demonstrated by waiting forever. The executable
contract is the absence of automatic expiry, survival across every ordinary
lifecycle path, and exclusive deletion through the separately authorised
audit-wipe boundary. Archive or tier changes may move evidence but must preserve
content and provenance.

### 4.5 Memory retrieval and Wiki — 5 tests

20. With a frozen clock, otherwise comparable local memories receive a
    monotonically smaller recency component as they age. Diagnostics expose
    vector similarity, text relevance, importance and recency as distinct
    components.
21. The local memory-search Skill has identical catalogue visibility and
    runtime invocation scope; an unadvertised or unauthorised Agent cannot call
    it through metadata alone.
22. Central BGE-M3 search defaults to the current HASHI instance and Agent.
    Enabling `memory_sync` permits ingestion but does not widen read scope.
23. Cross-Agent raw consolidated-memory search requires explicit user
    authorisation, an auditable purpose and provenance-preserving results. Wiki
    retrieval cannot bypass this boundary or expose the raw cross-Agent store.
24. `/wiki` is registered by HASHI core even when no private command package is
    installed. Its standard prompt and provider interface contain no
    deployment-specific data, paths or credentials; an absent provider or
    insufficient retrieval capability produces a clear unavailable result.

## 5. Implementation and test sequence

1. Add or rewrite the contract tests first and record their credible pre-fix
   red state.
2. Implement the PCM parser, strict migration and adapter-neutral authority
   envelope.
3. Implement completed-exchange budgeting, Fixed delta PCM, handoff delivery,
   time context and capability catalogues.
4. Implement the canonical raw evidence store and its separate authorisation,
   encryption, retention and destruction boundaries.
5. Implement local recency decay, the local/central search interfaces and the
   generic core `/wiki` provider contract.
6. Run owning tests after each coherent change, then the core gate for shared
   runtime/configuration changes, and finally the explicit PCM contract scope.

The `/new` automatic-continuity gap remains governed by the separate session
lifecycle design identified in the PCM system design. It must not be silently
claimed as completed merely because Fixed bootstrap and handoff tests pass.

## 6. Completion gate

The PCM upgrade is complete only when:

- all 24 contract-test functions pass across their required parameter sets;
- all retained directly affected tests pass without weakening their security,
  authority or recovery assertions;
- each rewritten or new behavioural test has a recorded red/green reason;
- no supported backend advertises an unavailable Skill or Tool;
- canonical raw evidence is complete, access-controlled and protected from
  ordinary cleanup; and
- the implementation no longer depends on HER-V2 to provide HASHI-owned PCM
  behaviour.
