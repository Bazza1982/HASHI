# Nightly repair: declarative configuration writer closure

Date: 2026-09-12 AEST. Work item: HN-20260911-002. Branch:
`repair/nightly-batch-20260911`. Implementation commit: `4cb103a9`.
Baseline for the protected-Core guard: `19e330f985aaf6d5523fd82a3bcc8a4256b536c0`.

## Scope and ownership

PAO retains Agent, instance, Scheduler, Move and import business rules;
Frontend Connector retains local UI/channel preferences; the corresponding
Functions owners retain their schemas. `orchestrator/config_json.py` owns only
the common byte/publication contract. No protected Core file, external content,
Provider evidence, credential value or database row is changed by this batch.

This closes the tracked declarative-writer inventory after batches 01–05. It
also corrects two already-migrated preference owners that automatically replayed
a stale setting operation. A conflict is now reported after one rejected
publication and requires a new deliberate user action.

## Implemented boundaries

- Added absent-file snapshots, revision-bearing legacy root arrays and
  revision-checked deletion to the shared primitive.
- Migrated root configuration loading/migration, Workbench Agent management,
  light/legacy onboarding, Agent Move CLI/service, and Hermes Move/import paths.
- Migrated Scheduler definitions/state, skill registry/tasks/state,
  runtime-session state, observer declarations, Anatta state, Telegram stream
  policy, WhatsApp routes, terminal preferences and stable port assignments.
- Preserved unknown fields and made corrupt persisted state read-only: no
  mutation path may turn a display fallback into a replacement document.
- Agent Move records each whole-document publication and restores only that
  still-current revision. A concurrent winner is retained and recovery fails
  closed instead of copying old snapshot bytes over it.
- TUI and voice setting conflicts are no longer retried automatically.

The reviewed exclusions are intentional: Core; append-only logs/audit and
Provider originals; immutable packages; generated/rebuildable caches;
domain-specific transaction, receipt and lifecycle journals; media and user
files; Workzones; and the standalone bootstrap instance registry with its own
exclusive lock and atomic UTF-8/LF publication. They are not competing
declarative configuration writers.

## Focused verification

On HASHI1's source tree, CPython 3.12, the combined owner/direct-consumer run was:

```text
298 passed, 1 third-party deprecation warning in 23.46s
```

It covered shared primitive/config loading, Workbench/onboarding, Move/Hermes,
Scheduler, skill/runtime state, Telegram policy, WhatsApp routes, Anatta,
post-turn observers, TUI/voice/terminal preferences and stable ports. Additional
focused runs during development included 25 Agent Move service cases and the
Memory+/dual-brain observer suite. The latter completed with 47 passes before
the final preference-only edits; its product paths were unchanged afterward.

Compilation and `git diff --check` passed. The protected-Core guard against the
stated baseline reported `protected core check: ok`. No global test suite,
Provider call, connector message, scan, database cleanup or instance operation
was used for this source-level batch.

Native Windows qualification, H1/H3 Functions adoption, HASHI2 adoption and a
post-adoption Memory/Wiki preflight are separate evidence gates. Source commit,
running generation and observed behavior must not be collapsed into one status.
