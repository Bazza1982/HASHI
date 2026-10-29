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

## Approved scope and limitations — 2026-10-29

The user approved limiting this feature to information whose time or source can
be tracked accurately, and leaving untraceable summaries alone. The preview and
completion notice must state that this can leave information from the requested
period accessible to the Agent. This is a technical limitation, not a guarantee
that the Agent has forgotten everything from that period.

- Persona and system instructions remain. Memory+, HCC, unstructured PCM,
  compaction, Wiki pages, relationship caches and other summaries are not
  cleared merely because their creation or modification time overlaps the
  interval. A summary can mix older and newer facts; it can still contain facts
  from the selected interval. No new per-item timestamp scheme is implied.
- Individually timestamped Session messages and raw turns in the interval are
  redacted; messages with no reliable timestamp and messages outside the
  interval are retained. A boundary-crossing pair retains its outside message.
  An attributed memory is removed only when its source can be matched to a
  selected whole exchange or run.
- Phone calls entirely within the interval can be removed as a unit. A call
  crossing a boundary, or lacking reliable bounds, is left intact; it can still
  expose interval content. Other indivisible run projections may also remain
  when a run crosses the boundary. These are additional implementation limits
  and must be made visible rather than described as exact full forgetting.
- Raw transcript and central-memory replay can exclude records matching
  known times and source fingerprints. Missing or unverifiable timestamps and
  summaries remain available. Historical central records without such a
  fingerprint cannot be guaranteed removed.
- The command opens a fresh Session and retains original conversation logs and
  protected audit copies for investigation. Those audit copies are not part
  of automatic context, but untracked summaries can still carry similar facts.
  A fresh Session does not imply all old Session-specific summaries are copied.
- Verified older Memory+ archives can be carried to the fresh Session. This is
  separate from a guarantee about the origin of each fact in a summary.
- This scope does not erase provider-held copies, external backups, arbitrary
  user files, independent services, or another Agent's data. Audit retention is
  not irreversible deletion. Content explicitly copied into a new request can
  influence that request.

## Qualification and rollout

Use isolated disposable data for repeatable tests, including failure and resume,
old and new messages, unknown-time summaries, and boundary-crossing material.
Report source, tests, running Worker, production, and user acceptance separately.
The user's accepted limitation must be present in the interface and PR review;
passing tests never proves complete erasure of untraceable information.

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
  may include older information. User acceptance and the provenance limitation
  remain open; the user's real Agent memory was not modified during these tests.

## HASHI3 development record — 2026-10-29

- Approval: the user reiterated that only the specified interval is to be
  forgotten; prior approval covers implementation and HASHI3 reboot/testing.
- Implementation: independently verified older Memory+ archives are available
  in the fresh Session. Their content digest and conservative time bound follow
  copies through repeated operations, including retries and wider intervals.
- Offline validation: 210 relevant tests passed; a further focused case passed
  after adding interruption, repeated-forget and modified-copy checks. Worker
  adoption and user acceptance require separate evidence.
- Mixed untimestamped content and complete cross-boundary runs/Phone calls
  remain open; do not describe this as exact time-bounded forgetting.

## Scope decision — 2026-10-29

The user explicitly chose to leave summaries without precise source lineage
unprocessed and accepted that as a technical limitation, rather than adding
timestamps throughout the rest of the system. This decision supersedes the
earlier summary quarantine policy. The command warning and review must say
that forgotten-period information can remain in untraceable material.
Implementation, offline checks, HASHI3 Worker adoption and live acceptance
must each be recorded against their exact source version.
