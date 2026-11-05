# Trusted permission-ledger maintenance

Owner: PAO. Layer: Functions and the independent Windows installation.
This is an operator procedure, never an Agent tool or public approval RPC.
It grants no permission and does not change Protected Core or L0.

## Required operating boundary

Use the explicitly named nonproduction installation and its complete qualified
source/runtime generation. Record instance ID, Windows build, source commit,
capsule identity, service/Runner identities, initial service state and owned
resources. Preserve production, unrelated services, credentials and user files.

Stop admission and drain the exact test installation. Record uncertain effects;
stop its native resources through the existing Runner/supervisor and retain
their actual cleanup receipts. Stop Safety before offline maintenance. The
operator takes the same kernel-exclusive service.lock as startup before opening
the ledger. A competing service or maintainer must fail without displacing the
owner. Native elevation and private directory/file ACLs are mandatory.
No server, normal question answer or application credential substitutes for this
local operator boundary.

## Inspect and reconcile an old backup

Use the qualified interpreter with the module
orchestrator.permissions.ledger_maintenance, action inspect, an absolute private
installation descriptor and its explicit instance ID. Inspection is read-only for
the two databases: it performs no startup, migration, generation switch or absent
file creation. Record the returned revision.

For action reconcile supply that revision, a fresh 32-character recovery ID and
an incident reason. The operation archives both observed SQLite databases in the
private installation maintenance directory before any permission change. Archive
files and manifest are never sent to a Runner or normal backup restore target.
Partial or changed archives fail closed and are preserved for investigation;
a new incident ID may be selected after an explicit fresh inspection.

The manifest is atomically published for the installed Safety SID, System and
Administrators. An elevated operator can differ from the service account; its
extra user ACE is deliberately excluded while the candidate is still empty.
The completed manifest must pass the same private-path check immediately and
on exact replay. Never expand the trusted SID set to bypass this check.
Unqualified archives made by an earlier Draft remain preserved for explicit
operator reconciliation. Do not issue a new recovery ID merely to repair an
archive when its original ledger recovery may already have committed.

The ledger owner applies one attached-database transaction:

- revoke active and reserved grants, withdraw pending requests and expire old
  action buttons;
- fence prior executions and pause active tasks while preserving task identity,
  decisions, history, checkpoints and completed results;
- retain started operations as unknown, cancel queued/prepared operations, and
  retain unconfirmed resources as stopping; never claim physical cleanup;
- rotate the service fence and continuity witness together with the recovery
  receipt and append per-task recovery events.

A changed inspection revision, backward trusted time, unknown schema, missing
witness, foreign instance or witness-write failure aborts without changing the
owner. Do not delete a witness, reset schema versions or blindly retry a conflict.
After success, exact replay returns the original receipt without touching new
grants. A restored stale copy of that receipt cannot stand in for continuity.

Normal startup can now proceed with the qualified current binary. Bind a new
execution before resuming a task. Unknown effects still require the existing
outcome reconciliation and trusted resource cleanup. Missing permissions require
new genuine decisions. Recovery never silently replays a business effect.
A restored backup cannot recreate later history absent from that backup; preserve
the archived files and original PAO/canonical audit evidence and report the gap.

## Upgrade and version rollback

Keep the entire previous qualified source/runtime generation and private
installation descriptors. Never install dependencies into Core or copy selected
source patches into an installed service. Stage and qualify the complete
candidate first. Both startup and maintenance use the same exclusive
installation lock. Incompatible candidate admission must not fence an active
owner.

A legacy-to-schema-9 migration uses the authority's existing atomic migration and
attached witness transaction. Validate preserved task/decision data, valid L1
grants and fresh execution binding. A failed precommit migration must leave the
old schema usable. Interruptions after commit must recover the complete new pair.

Preserved lease history does not authorize a changed installed policy revision.
Changing the qualified runtime descriptor changes that revision: retain the
old records and require current-policy decisions instead of rewriting old grants
to match. A precommit rollback to the exact original program and descriptor can
retain its still-valid L1 authorization. Never restore an old ledger to achieve
that behavior after schema 9 has committed.

Rollback selects a complete compatible qualified program generation. It never
restores older grants, copies a witness backward or changes user_version to make
an old binary accept new data. A schema-8 program must reject schema 9. If the
chosen old program is incompatible, keep L1/L2 explicitly unavailable and L0
usable, then restore the qualified schema-9 program using the unchanged current
ledger. Do not advertise this temporary unavailability as successful service
restoration or enable an unrestricted fallback.

## Qualification and release

The backend failure proof covers actual SQLite backup restoration, unknown
effects, fresh approvals, replay after new grants, stale-receipt restoration,
CAS conflicts, witness-write rollback, trusted-time rollback and absent witnesses.
Native administrator admission, private ACLs, kernel ownership, SCM
upgrade/failure/rollback, clock and resource cleanup need separate Windows
receipts. WAC30–32, WAC16/21 and frontend/platform combinations remain open until
their actual current-version observations satisfy the full normative WAC table.
