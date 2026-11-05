# Agent FYI v2 navigation and retrieval contract

Status: current source contract. Functional owner: PCM knowledge/context projection.
Engineering layer: replaceable Functions, documentation and focused tests.
PAO retains tool admission and filesystem protection. Protected Core is unchanged.

## Three levels

Only [Agent FYI](AGENT_FYI.md) and applicable root engineering guidance enter the default or explicit `/fyi` reference. Eight domain guides under `agent-guide/` provide triggers, ownership, distinctions, contracts, source/tests and adoption guidance. Existing specifications retain complete detail. There is no recursive document inclusion, model-based document selector or new documentation server.

The entry uses the `hashi-fyi-catalogue:v2` marker, six named top-level sections and a domain table containing unique identifiers, useful descriptions, reading triggers and guide links. CI validates source-relative Markdown destinations and anchors, actual current contracts, meaningful guide sections and the complete [old knowledge inventory](agent-guide/fyi-v1-migration.md). Historical documents may be referenced as history; they are not current authoritative destinations.

## Maintenance

A behavior change updates its owning contract and tests. Root FYI changes only when global principles, ownership, major domains, navigation or discovery change. Domain guides track significant domain features and moved contracts, source entry points or verification routes. Routine bug repairs do not add root FYI entries. The migration inventory remains review evidence and is never bootstrap content.

## Bootstrap and failure behavior

`orchestrator/agent_fyi.py` reads and validates the catalogue once and reads complete common guidance. The entry and common-guidance input safeguards are 12,000 and 6,000 characters; the assembled reference safety budget is 16,000. These protect actual context injection, not the size of the knowledge base.

Missing, empty, malformed, oversized or unreadable input produces an explicit incomplete-reference message and small known navigation fallback. A partial paragraph is never represented as a complete reference. No loading failure edits PCM, settings, permissions or the current user request. Ordinary conversation can continue. The combined revision hashes validated untruncated entry/common guidance, not every linked body.

Initial primer consumption remains Session-scoped and one-time. Scheduler, bridge and silent requests do not consume it. An explicit `/fyi` already containing the reference consumes an armed primer without injecting a duplicate. User request priority and command syntax are preserved.

## Scoped read-only retrieval

`documentation_read` is advertised through the existing tool catalogue and shared Registry, including the serialized CLI/MCP gateway. Existing explicit tool allowlists and permission admission remain in force.

Approved targets are Markdown files under the active source generation's `docs/` plus root `AGENTS.md`, `ARCHITECTURE.md` and `CONTRIBUTING.md`. No model-selected root, external URL, arbitrary filesystem access or secrets access is accepted. Resolve relative links against `from_path`, not the workzone. Reject traversal, symlink escapes, missing documents and missing anchors explicitly.

A read accepts `path`, optional `from_path` and `section`, and a page size of 256–8,000 characters. Outline mode returns up to 40 heading references. A response identifies the source path, content SHA-256, section and line. Partial responses set `complete=false`, warn that conditions may be incomplete and provide exact continuation arguments. A continuation requires the same source revision; changed content must be reread from offset zero. Source identity does not certify runtime adoption.

The existing qualified-generation binding chooses packaged documents over a mutable developer checkout. The gateway freezes that derived documentation root when serializing its context, so CLI continuation does not depend on inheriting launcher environment variables. This projection is not a new configuration owner. Clean tracked documentation is included in Function artifacts; unpublished local files remain excluded.

A backend with no allowed reader reports that limitation instead of fabricating content. Read results include a small `hashi-doc:` reference. Keep it in task continuity or handoff notes and pass it back as `reference` to reread that source/section. The existing compaction evidence validator recognizes this prefix and rejects a capsule that drops a required reference; no PCM redesign or additional state writer is introduced. A reference grants no filesystem authority, and changed source is reported explicitly. General chat does not fetch domain guides or engineering contracts.

## Verification and context accounting

Run `python scripts/validate_agent_fyi.py` and `tests/test_agent_fyi.py`. The validator checks navigation, migration content/links and actual bootstrap assembly. Runtime tests cover refresh, Session scope, current request priority, failure fallback, versions, explicit continuation, path protection, shared Registry/MCP and reading installed resources from an unrelated workzone.

[Context measurements](agent-guide/fyi-v2-context-metrics.json) reproduce the original wrapper and measure the new assembled reference and representative section reads. Counts are characters and explicitly estimated tokens when no tokenizer is installed. There are no model-dependent CI gates or combined-knowledge-base size assertions.

Phone translation discovery leads through the [Phone context contract](HASHI_PHONE_CONTEXT_HANDOFF.md) to the [explicit capability boundary](HASHI_PHONE_TRANSLATION_BOUNDARY.md) and Compute directory. This baseline has no separate translation service contract; navigation must expose that absence. Move and PCM tasks lead to their existing contracts and actual code/tests.

Source validation, packaged resources, runtime adoption and physical-client acceptance remain separate. This source refactor does not deploy, restart services or claim live adoption.
