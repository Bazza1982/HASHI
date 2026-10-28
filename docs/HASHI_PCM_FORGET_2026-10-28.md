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

A preview fixes the time interval and the Agent/owner/Session scope. Confirmation
is a separate, one-time, expiring command. Changed history, an expired preview,
another owner sharing unscoped Agent memory, active work, or an unresolved Phone
attempt makes the operation fail closed. New Agent work is blocked durably while
an accepted operation remains incomplete; `/forget resume` resumes that operation.

## Retention and isolation

- Persona and system instructions remain. Source-unverifiable Agent memory, HCC,
  Memory+, dual-brain continuity, and context-compaction pointers are taken out
  of active Context. Original artifacts are retained through the canonical audit
  owner, using its configured access and encryption policy.
- Selected Session messages, Phone material, local memory rows and relevant
  search indexes are removed from normal projections. Original conversation
  logs and isolated audit copies remain available for human investigation.
- Session generations and Engine state are renewed; forgotten source identities
  and intervals fence later transcript or transfer re-imports.
- Memory and relationship material without reliable lineage is quarantined.
  Current central memory, Wiki search, and remote memory export remain
  quarantined for this Agent because they do not establish complete provenance.
  There is no automatic release path in this development version.
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
- Live validation: not performed. No production deployment or user acceptance
  is implied; the limitations above remain open release concerns.
