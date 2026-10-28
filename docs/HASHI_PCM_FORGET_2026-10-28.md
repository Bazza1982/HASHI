# PCM /forget — development decision

Status: in development on the current HASHI3 branch. This document records the
approved behaviour and remaining qualification work; it is not proof of Worker
adoption, production deployment, or user acceptance.

## Purpose and ownership

A user can request `/forget 2h` or `/forget 3d` for the current Agent on the
current instance. PCM owns the removal and quarantine boundary. PAO owns the
owner-scoped Sessions, execution fence, and fresh Engine/Conversation Session.
Frontend Connectors authenticate the requesting user and display the preview
and result. Core is unchanged.

A preview fixes the time interval and the current Agent's complete Session scope,
including historical owner identities. The authenticated initiating owner and
Session still bind the one-time, expiring confirmation. Changed history, an
expired preview, active work, or an unresolved Phone attempt makes the operation
fail closed. New work for the entire Agent is blocked durably while an accepted
operation remains incomplete; `/forget resume` resumes that owner's operation.

## Retention and isolation

- Persona and system instructions remain. Memory, HCC, Memory+, continuity,
  and compaction artifacts verifiably unchanged before the interval are retained.
  Unverifiable or mixed artifacts are taken out of active Context and preserved
  through the canonical audit owner with its configured access and encryption.
- Selected Session messages, Phone material, local memory rows and relevant
  search indexes are removed from normal projections. Original conversation
  logs and isolated audit copies remain available for human investigation.
- Session generations and Engine state are renewed; forgotten source identities
  and intervals fence later transcript or transfer re-imports.
- Central search admits verifiably older records and exact matches to retained
  local exchanges; uncertain records remain excluded. Wiki and handoff recall
  admit files verifiably unchanged before the interval. Remote export admits
  intact older transcript rounds and new records appended after a verified
  completion checkpoint; forgotten audit rounds remain excluded.
- Remaining limitation: a summary or Wiki page updated during/after the
  interval without complete lineage cannot be safely separated or automatically
  restored. This can also exclude legitimate newer information. Source-level
  provenance and a safe recovery path are still required before full acceptance.
- This scope does not erase provider-held copies, external backups, arbitrary
  user project files, independent services, or another Agent's data. Audit
  retention is not a promise of irreversible deletion. If a person explicitly
  reads or copies retained material back into a new request, it can influence
  that request.

## Qualification and rollout

Unit and API integration tests use isolated disposable data, including failed
and resumed operations. Before enabling the command for real use, verify all
active import/export and continuity paths, Worker adoption and the actual
Frontend experience on a non-production test Agent. Report source, tests,
running Worker, production, and user acceptance separately. Incomplete or
unverifiable isolation is a release blocker, never a successful forget.

## HASHI3 development record — 2026-10-28

- Approval: the user requested development and testing on HASHI3's existing
  branch, with retained audit copies excluded from automatic Agent context.
- Implementation: the source changes belong to HASHI3 branch
  `debug-issues-oct`; the Git commit is the definitive source identity.
  Worker adoption must be verified independently after the commit.
- Offline validation: 364 relevant tests passed, including owner authorization,
  audit retention, failure and resume, isolation, continuity, and Session
  history. The protected Core check passed.
- HASHI3 live validation: after the user authorized commit and reboot, commit
  `49ae6555` was adopted through `/reboot max`, with 13 Agents online.
  The authenticated command usage and non-destructive preview worked in
  Workbench; the preview formatting defect was then corrected in source.
- User correction on 2026-10-28: the scope is the Agent, not a single historical
  owner; preserving other Agents and verifiably older information is required.
  The earlier owner-based rejection and permanent blanket search/export
  quarantine did not meet that intent.
- Corrected implementation: the Agent-wide confirmation, retention rules, and
  filtered memory/export paths are implemented on the same branch.
- Offline validation: 378 related tests passed, followed by 49 focused tests
  after the repeated-forget export correction.
- HASHI3 live verification of implementation commit `6cd533f3`: a full
  reboot loaded the matching Function generation. On a disposable Agent, a
  real conversation was forgotten, its audit copy was retained, and a new
  Session answered that the test marker was unavailable. A second disposable
  Agent with a synthetic historical owner completed confirmation and removed
  both test messages. Both temporary Agents were removed after testing.
- The following user-facing warning clarifies that quarantined memory and HCC
  may include older information. Adoption of that wording requires its own
  Worker verification. User acceptance and the provenance limitation remain
  open; the user's real Agent memory was not modified during these tests.
