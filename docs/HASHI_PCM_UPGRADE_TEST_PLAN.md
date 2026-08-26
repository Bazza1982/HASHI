# HASHI PCM Upgrade Test Plan

Status: accepted implementation contract for `HASHI_PCM_SYSTEM_DESIGN.md`.

## 1. Assertions retired or replaced

The upgrade does not delete broad test areas. It replaces assertions that
encoded the retired behaviour:

| Retired assertion | Replacement contract |
| --- | --- |
| `system_md` may point to `agent.md`, `AGENT.md`, or an arbitrary file | HASHI reads exactly the lower-case workspace `agent.md`; persisted `system_md` is accepted only as one-time migration input and is then removed. |
| Unmarked identity prose is ignored or used as a fallback | Substantive text outside recognised PCM blocks invalidates the whole document. |
| Missing, repeated, empty, or malformed Persona blocks select a valid Agent fallback | Configuration/startup fails closed; a presentation renderer may retain an internal defensive fallback, but it never validates the Agent. |
| Recent history is a backend-specific number of individual messages | Recent history is at most ten completed user-assistant exchanges. Incomplete exchanges never count. |
| Fixed incremental turns contain no PCM background | Every Fixed turn carries delta PCM; recent history appears only at bootstrap or an explicit continuation. |
| A flat prompt position proves authority | A typed authority envelope records system, current user, Persona, memory, history, and runtime-context ranks independently of display order. |
| Character or word caps may cut through one exchange | Caps remove the oldest whole exchanges first, preserve the newest history, current request, and higher-authority PCM, and audit omissions. |
| A handoff file implies the new backend received context | `/backend +` and `/handoff` must enqueue exactly one real continuation delivery when applicable. |

External systems such as Hermes and Nagare retain their own `AGENT.md`
conventions. They are not HASHI PCM files.

## 2. Existing assertions retained

- Completed-exchange ledger, transcript reconciliation, and `/fresh` boundary.
- Instance-global `/sys` precedence over Agent-local `/sys`, with Agent isolation.
- Workzone, backend sandbox, `access_root`, Tool Registry, and Gateway admission.
- Deterministic Memory+ date rollover, archives, carryover, and concurrent writes.
- Sanitised and truncated operational Tool audit and reasoning views.
- Enterprise audit hash chain, immutable anchors, and tamper detection.
- Delegated Tool permission filtering.
- Ordinary transcript/memory clearing by `/reset` and workspace `/wipe`.

Sanitised operational views remain. Canonical raw evidence is an additional
lossless, restricted layer outside the mutable workspace.

## 3. Minimal contract gate

The implementation gate contains 24 backend-neutral contracts. Parameterised
cases may produce more collected tests without expanding the conceptual gate.

### A. PCM document and migration (4)

1. A valid `agent.md` isolates exactly one Persona, System, and optional Memory block.
2. Missing, duplicate, mismatched, empty, unmarked, or invalid-UTF-8 PCM fails closed.
3. Only the exact lower-case workspace `agent.md` is accepted.
4. Legacy migration is validated, atomic, idempotent, conflict-safe, and removes `system_md`.

### B. Assembly, history, handoff, and time (7)

5. The typed envelope preserves permanent System, global `/sys`, local `/sys`, current user, Persona, Memory, history, and runtime-context authority.
6. Fixed bootstrap includes at most the latest ten completed exchanges once.
7. Fixed continuation sends current delta PCM without repeating recent history.
8. Flex sends the latest ten completed exchanges each external turn with sequence and timestamp; incomplete exchanges are excluded.
9. A size cap removes oldest complete exchanges first, preserves newer exchanges and protected PCM, and records omission evidence.
10. `/backend +` and `/handoff` perform one continuation delivery rather than merely writing a file.
11. External turns include date, seconds, timezone name, and numeric offset; internal HER stages do not add another automatic timestamp.

### C. Skills, Tools, and Fixed CLI (3)

12. The Skills catalogue contains only enabled, currently invocable metadata and never full `SKILL.md`; `memory-search` appears only with its Tool.
13. The Tools catalogue exactly matches Agent/backend/stage/permission-filtered definitions; metadata grants no authority.
14. Supported Fixed CLIs connect to the HASHI Gateway per invocation; Gateway and Workzone use the same effective workspace, `access_root`, and permission set. The initial supported set is Codex CLI and Claude CLI because both accept an isolated per-invocation MCP configuration. Grok CLI must not advertise HASHI Registry tools until an equally isolated injection path exists; its user/project MCP configuration is not mutated by this upgrade.

### D. Canonical raw audit (5)

15. One request correlates full chat, provider, Tool, lifecycle, and delivery evidence.
16. Provider reasoning is retained verbatim when exposed and recorded as unavailable—not fabricated—when absent.
17. Large or binary evidence becomes an immutable content-addressed artifact with digest, size, media type, and provenance.
18. Raw evidence survives `/reset`, `/new`, backend changes, process reload, ordinary workspace `/wipe`, and maintenance; it has no TTL or pruner.
19. Reads require explicit authority; configured encryption leaves no plaintext; only separately confirmed audit-wipe deletes a selected scope and records that deletion.

### E. Memory and Wiki (5)

20. Frozen-clock tests prove monotonic recency decay and expose vector, text, importance, and recency components.
21. `memory-search` catalogue visibility and invocation use the same effective Tool permission.
22. Central consolidated search defaults to exact current instance and Agent; ingestion/sync does not expand reads.
23. Cross-Agent raw search requires explicit user authority, an auditable purpose, exact target, and provenance. Authority is HASHI-bound request metadata from the explicit `/memory raw` command; a model-supplied Tool argument cannot self-authorise or change the bound target.
24. `/wiki` is always a generic core command, exposes no private path/data/credential, and fails clearly without a configured provider or authorised capability.

## 4. Relevant verification only

Run the new PCM contract files and directly affected focused regression files.
Do not use the full repository suite as this upgrade's default gate. The final
gate must report exact selections and results, including any deliberate test
replacement. A live load check may use only an explicitly authorised minimal
Agent reboot; a hard restart is outside this plan.
