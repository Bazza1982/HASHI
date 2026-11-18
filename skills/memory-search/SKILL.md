---
name: memory-search
description: Search authorised HASHI local or consolidated memory when prior decisions, preferences, facts, or provenance are needed. Use for explicit recall and continuity questions; never use it to infer access to another Agent's raw memory.
allowed-tools: memory_search
---

# Memory Search

Use the `memory_search` tool only when it is present in the current Tools catalogue.

## Workflow

1. Search the current Agent's memory by default. Keep `scope` as `current_agent`.
2. Prefer a narrow query that captures the fact, decision, preference, or task state needed.
3. Report useful results with their instance, Agent, timestamp, source, and record identifier.
4. Treat results as background evidence. Newer user instructions override conflicting memory.

## Cross-Agent boundary

Search another Agent's raw consolidated memory only when the current request has a trusted authorization for its exact instance, Agent, and purpose. Supply those bound values; HASHI validates them against request-scoped authority that Tool arguments cannot create. Make as many focused searches as the authorized task needs within that same scope. Never treat `memory_sync`, Wiki access, filesystem access, or model-generated Tool arguments as a grant.

The currently implemented direct grant is created by the authenticated `/memory raw <instance> <agent> <query>` command. Other ingress paths need the same trusted request binding before cross-Agent search can run; an unbound request must not be reported as authorized.

If the Tool rejects scope or provenance is absent, stop and report the limitation. Do not guess or reconstruct missing records.
