# HASHI Fingerprint

Owner: PAO. Engineering layer: Functions.
Decision date: 2026-10-31.
Source scope: existing `feature-fingerprint` branch; implementation and offline
qualification are separate from runtime adoption and frontend acceptance.

This implements the artifact-provenance first version described in the
[implementation plan](HASHI_FINGERPRINT_IMPLEMENTATION_PLAN.md). It does not
change Runtime/Core fingerprints, permissions or protected Core.

## Policy and admission

The setting is `global.fingerprint` in the existing instance configuration:

```json
{"global":{"fingerprint":{"enabled":false,"marking":"auto","export_content":false}}}
```

Missing settings stay OFF. Reading defaults never writes them back. Explicit
null, string booleans, unknown keys and unsupported marking modes are rejected.
Only the existing authorized management boundary calls ConfigAdmin's policy
writer. No model-tool parameter can enable or override the feature.

The writer uses the existing config_json revision/CAS writer and records a
durable prepared/changed management pair in Canonical Audit. Failure before
publication preserves configuration. A post-publication audit failure carries
the existing committed durability error; it must not be blindly retried.

PAO reads the configured policy at admission and persists its immutable JSON
snapshot with the Run in the SessionStore transaction. Schema 31 adds one
Run-owned field; legacy Runs without it remain OFF. Corrupt nonlegacy policy
is rejected rather than silently becoming OFF. Idempotent replay and queued
recovery restore the transaction winner's original snapshot, even after a
management change or process restart. Policy does not enter the message
idempotency digest.

Pipeline/backend/gateway project that same pure JSON policy and trusted
Instance, Agent, Session, Run and request IDs. Tool-call identities come from
the execution boundary, not file hashes, directory names or tool arguments.
Managed native execution also projects the persisted Run policy.

OFF performs no Fingerprint target observation, marker generation, audit
events or background watching; existing tool logs and behavior continue.

## Controlled file operations

For HASHI file_write/apply_patch, provenance runs after ordinary admission and
inside the existing resource lease. It observes actual preexisting bytes,
including uncommitted human edits, persists a preparation, executes the tool,
observes the result and persists an exact-version receipt. Buffered Canonical
Audit must pass its flush/barrier; queue admission alone is insufficient.

Before/after hashes stream large files. An unstable observation becomes a
conflict, not an affirmative attribution. Small final byte observations are
reused by existing effect receipts rather than reopening after the lease.
The lease coordinates participating HASHI tools; it does not lock human editors.

Business, evidence and marker/projection outcomes remain separate. A failure
to establish required durable preparation blocks that controlled mutation.
Failures after a committed write never cause automatic business replay.
Cancellation preserves partial-effect evidence and cleanup uncertainty. Started
post-execution evidence I/O finishes before releasing the existing resource
lease, even when the caller cancels. A partial marker failure leaves the final
version unknown and suppresses stale derived effect receipts; it does not replay
the completed business operation.

## Portable markers

The first qualified inline adapter is UTF-8 Python. It rejects generated,
signed, snapshot and unsupported-encoding cases; checks parsed semantics before
and after adding one short comment; and preserves existing header, BOM, mode
and newline bytes. The final hash includes the marker. Repeated edits keep one
artifact marker rather than accumulating author names or timestamps.

Other formats use `.hashi-fingerprint/manifest.json`. A single-file export also
includes `filename.hashi.json`. JSON, binary and signed artifacts never receive
a forced text comment. The manifest contains selected receipt projections,
exact hashes and uncertainty; it is rebuildable evidence, not a competing
authoritative audit history.

Namespace ownership is checked before updates. Existing unrelated directories,
files, links and sidecars are preserved. Participating initializers and manifest
writers serialize; revision conflicts are surfaced without blind overwrite.
An older Agent's repair cannot rewind a newer known shared entry.

The public projection omits raw tool arguments, conversations, content and
absolute local paths. Copy the manifest with a project, or the companion with
a standalone file. Copying only a code fragment or removing the companion
does not preserve provenance.

## Coverage

| Path | First-version evidence |
|---|---|
| HASHI file_write/apply_patch | Exact before/after versions in the participating controlled interval; expected-content check; safe marker or manifest |
| HASHI shell/bash | Execution-window facts, including failures and unfinished cleanup; only trusted, explicitly configured bounded file targets are observed |
| Native Engine edits | Actually received file-edit events, labelled engine_reported; no claim that a start event means a completed write |
| Supervised files | Existing verified permission result retained, with explicit artifact-adapter gap; no reopening a write-only handle or extra evidence-directory permission |
| Human/external editors | No global lock or blanket authorship attribution |
| Background writes | Coverage remains incomplete while writes may outlive foreground execution |

There is no recursive repository scan per tool and no git-diff authorship
inference. Native events may be fire-and-forget; the receipt scope is only
events actually received, not a guarantee that the entire Engine stream was
drained. Unsupported paths do not acquire an affirmative author marker.

The supervised adapter deliberately reports permission_result_only and
not_qualified. Its existing service audit callback is not assumed to be a
durable Canonical Audit writer. Full held-handle artifact capture remains
unqualified; no permission protocol was widened to pretend otherwise.

## Local commands

```text
python hashi.py fingerprint status --home <instance-home>
python hashi.py fingerprint inspect <file>
python hashi.py fingerprint verify <project>
python hashi.py fingerprint export <project> --output <new-evidence.zip>
python hashi.py fingerprint export <file> --output <new-evidence.zip> --include-content
python hashi.py fingerprint set on --home <instance-home> --actor <authorized-actor> --expected-revision <revision>
python hashi.py fingerprint set off --home <instance-home> --actor <authorized-actor> --expected-revision <revision>
python hashi.py fingerprint repair <project> --home <instance-home> --agent <agent> --actor <authorized-actor>
```

The standalone equivalent is `python -m orchestrator.fingerprint <action> ...`.
Inspect/Verify require only the delivered artifacts and portable evidence.
They distinguish exact-byte match, structural association, evidence grade,
declared coverage and unsigned authentication. Broken links, missing identity,
unsupported schema and malformed indexes never become a positive match.

Export defaults to metadata only. File bytes require the explicit
`--include-content` choice. Output is exclusively created, and archive paths,
case collisions and evidence namespace collisions (including file/directory
ancestor conflicts) are checked before writing. Portable indexes and companions
accept only their supported integer schema; booleans are not schema versions.
The export is selective and unsigned: it is neither cryptographic author
authentication nor a proof of the complete internal audit chain.

Repair reads authorized Canonical Audit and checks current versions before
rebuilding projections. It never reruns the original tool or past business
mutation. A later external edit produces version_mismatch; absence/deletion is
represented explicitly.

## Qualification and adoption

Focused behavior covers default OFF, real management CAS into new Run
admission, persistence/replay/queued recovery, Gateway JSON round trips, actual
Codex file-event conversion, controlled write/patch/cancel outcomes, source and
companion markers, corrupt exports, same-file Agents, external conflicts and
four process-crash boundaries.

Windows offline execution additionally exercises CRLF, long paths, path case,
exclusive file occupancy and real invalid-UTF-8 binary observations. These are
local deterministic tests, not a live frontend/model or running-instance
acceptance claim. Required shared-runtime gates are recorded separately.

On A9_MAX's isolated Python 3.12 test environment, 20 warm small JSON writes
measured median tool latency about 25 ms OFF and 75 ms ON; median measured
Fingerprint time was 62 ms. Process I/O deltas were about 0.34/5.50 MB read and
0.05/0.51 MB written for OFF/ON. These OS counters include audit/locks and are
local observations, not a timing guarantee. Both 100 ms idle samples added no
async task and measured zero CPU ticks. ON pays durability/projection cost;
it introduces no persistent polling process.

Implementation authorization, source commit/PR, offline tests, runtime
adoption and user acceptance are separate evidence. The current delivery does
not change or restart production. Operational adoption must follow the
instance-specific authorization and Functions replacement scope.
