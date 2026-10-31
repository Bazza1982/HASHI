# Fingerprint independent implementation review

Date: 2026-10-31, Australia/Sydney.
Scope: HASHI6, feature-fingerprint, PAO / Functions.
Implementer: lin_yueru (/root), sole checkout writer.
Independent read-only reviewer: /root/fingerprint_review.

The reviewer inspected actual implementation changes in policy/configuration,
Run admission and persistence, idempotent recovery, gateway JSON projection,
controlled execution, markers, manifest concurrency/CAS, repair, coverage,
permissions integration, standalone verification and minimal export.

Issues found and corrected during review included: the public frontend
admission path; a buffered-audit durability barrier; preserving a committed
business outcome when derived effects fail; corrupt frozen policies silently
defaulting OFF; first-namespace initialization races; stale peer repair
rewinding latest evidence; premature projection-success states; real Codex
tuple paths; incomplete Shell cleanup after cancellation; supervised write-only
readback boundaries; invalid policy handling after a supervised write; strict
receipt/index validation; optional-field data leakage; and ZIP collisions.

A final failure-boundary pass additionally reproduced stale effect receipts
after partial marker writes, cancellation releasing a lease while marker I/O
was still running, unsupported portable schema versions, and file/directory
ancestor collisions in ZIP names. Each new regression failed before its fix.
The corrected implementation waits for owned finalization before releasing the
lease, withholds stale effects, requires an exact supported integer schema and
rejects conflicting archive paths. The updated focused suite passed 79 cases.

Final read-only conclusion: previously reported blocking issues were corrected
in the reviewed files; no further blocking implementation issue was identified.
This is a source review, not a test execution or runtime-adoption receipt.

Scope limits remain explicit: supervised execution retains its permission
result but does not claim qualified artifact hashes/markers; Engine provenance
covers received events only; the portable bundle is unsigned and selective.

Behavioral failure proof is retained in ignored local verification artifacts.
Before implementation the controlled-write suite failed four cases and the
portable-artifact suite failed five cases. Five later independent, in-memory
fault mutations each caused their focused test to fail: removing the audit
barrier, replacing a recovered policy with current configuration, dropping real
tuple Engine paths, suppressing a version mismatch, and removing content
opt-in. These probes never edited on-disk product source or running services.

An unrelated existing Exchange relay-address/instance-ID format conflict was
observed while arranging a service fixture. It was not changed in this feature.
PAO queued recovery uses valid locally sealed Connector facts; it does not
claim end-to-end qualification of that external Exchange service path.

Final command results and source/PR state are maintained in the delivery
record. Promotion still depends on passing the selected shared-runtime gates
and separately authorized runtime adoption.
