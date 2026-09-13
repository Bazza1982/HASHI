# HASHI Core protection hardening — 2026-09-13

Status: approved and implemented in the HASHI2 pilot working tree.
Functional owner: PAO release and engineering governance.
Engineering placement: development tooling, CI, and documentation; no Core
runtime source is changed.

## Decision

The protected Core remains in its current locations. Moving launch entrypoints
or protocol modules into a new folder would itself be a Core migration and does
not create a security boundary. The authoritative scope remains solely
`orchestrator.runtime_contract.CORE_SOURCE_PATHS`; documentation must not copy
its members.

Agents receive the rule from the standard root `AGENTS.md`. Every protected path
is immutable unless the current user explicitly authorizes a Core major-version
migration. A permitted migration is one reviewed pull-request scope and needs
all of the following:

- a higher product major version than the base, with minor and patch reset;
- the `core-change-approved` label;
- a newly added review record matching the candidate Core digest; and
- an independent reviewer distinct from the implementer.

Local authorization flags acknowledge the user's decision but cannot waive
these release gates. Ordinary changes continue to pass without Core ceremony.

## Pilot boundary and status

- Authorization: the user approved these hardening measures for HASHI2 first on
  2026-09-13.
- Core relocation: not performed.
- Protected Core source edits: none intended or authorized by this task.
- HASHI2 local hook: already enabled; it is tightened by this change.
- Focused verification: the Core guard and engineering-safeguard modules pass
  13 tests. They prove that authorization alone is rejected, an unchanged major
  version plus missing review is rejected, and a matching independently reviewed
  major release is admitted. Manifest, shell syntax, workflow parsing, and diff
  checks are recorded at closeout.
- GitHub branch protection: repository-global, not instance-scoped. It remains
  outside the HASHI2-only pilot until the local policy is verified and the user
  chooses shared activation.
- HASHI1, HASHI3, HASHI4 and running Worker adoption: unchanged by this pilot.

Implementation, focused verification, publication, shared repository policy,
and live adoption remain separate claims and must be recorded separately.
