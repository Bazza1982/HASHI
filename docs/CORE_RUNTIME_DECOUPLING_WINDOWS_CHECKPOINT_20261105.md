# Core v5 atomic Windows launch checkpoint

## Selective real dependency security handoff

Following the successful original-version split/parity checkpoint, only the
five reviewed package pins from PR137 changed. Other 45 standard packages and
the Portable-only closure are unchanged. Derived hashed closures contain
50 standard and 55 Portable packages. Fresh empty-target installation used
only wheels with required SHA256 hashes; the five installed wheel file sets
also matched the separately cached, reviewed official artifacts. PR137 itself
was not merged and its coupled Core governance/version records were not copied.

The old exact cookie/reference-lifetime probes previously failed as expected.
The same probes plus image/certificate coverage and derived-lock checks now
pass: 17 cases, 3.94 seconds. The actual Windows controller qualification took
305.20 seconds, exit 0, source tree stable
3260a376a1d8e1dd02c26ec86113d7179ba9da86905afb469bef9a2703ad605e.
Core PID 42520, CR_ID
cr:sha256:0e7a585adf8b26e508e188124a567951ce322cc3cd97a8ff9c819cdf6246ea18
and protected source digest cbad4ed2987d209665d12af2c1c8c2ce05a3f492f11abfa8592f051fcce705ac
remained identical throughout actual old/new package switching, failed
activation rollback, downgrade and a second upgrade. The synthetic physical
effect count stayed 1. Owned Functions and the temporary controller retired.

Old FR_ID fr:sha256:8a8c26a2508384a3af659ac0895ee21db0550d0694a8799b017db85987b0dd50;
new FR_ID fr:sha256:9f91b6ee0e7c95d2225ec316901cd99393c7dff6bf2292fa2efe63eb6adab6c1.
Both separately prepared the same complete product FG
sha256:50e0d452edfbd1ea606bc1c362bc4f92318531e0676d06f6b2fa641cc160305c,
667 modules and 728 resources, with no service/connector activation or real
provider/account use. Full post-upgrade business CI and packaging remain
pending at this checkpoint, as do independent human review and adoption.
Source evidence stays separate from live-client and L2/WAC qualification.


Status: review-only source. This supplements the role/split checkpoints, without
claiming complete Gate 2/3, security-dependency adoption or release readiness.

Windows source CI found a real race: a venv redirector could create its base
interpreter before the parent assigned the redirector's Job. Admission correctly
rejected that unowned interpreter. A deliberately delayed ownership test
reproduced the same OS failure locally; no unexpected receipt was accepted.

The launch owner now creates and configures its Job first, then calls the
documented CreateProcessW extended-startup interface with JOB_LIST and an
explicit inherited-handle whitelist in the same operation. The interpreter and
its redirector cannot execute before assignment. This also avoids the suspended
orphan window of a separate create-suspend-assign-resume sequence. No debugger,
extra privilege, global spawn-executable override or localhost listener is used.

Only the private IPC channel and explicitly duplicated standard I/O are
inherited, matching Popen's redirected-stdin behavior. The Job handle itself is
not inherited. Temporary standard handles, process/thread handles and attribute
lists have explicit retirement. Existing ancestor Job restrictions remain in
force; native tests observe that membership before crashing the controller.
Admission still checks the real OS image and parent, exact role fingerprint,
one-time nonce and immutable generation before product execution. A child
outside its already assigned Job is rejected.

The process wrapper observes asynchronous Job termination before trying another
termination. Cancellation, lifecycle and recovery continue to use the same
process interface and JSON envelopes. POSIX creation remains unchanged and
still needs its separate native qualification.

Native fault evidence includes:
- the original CI and deterministic local pre-ownership race failure;
- real simultaneous starts, round trips, unsolicited events, crash persistence,
  rejected executable/package/schema/PID/nonce/generation and cancellation;
- abrupt controller death immediately after process creation and after a worker
  starts a real long-lived grandchild, with every observed descendant retired;
- preservation of the creator's existing Job restrictions;
- strict OS HANDLE counts across repeated completed launches, without a tolerance.

Disabling only the test controller's child Job kill limit makes the actual
parent-crash assertion fail; the test's independent outer owner then retires all
recorded children. Disabling process-handle retirement makes the OS count
increase and fails the handle test. These private mutations never change
production or installed environments. Their failures and cleanup receipts are
preserved.

CI also exposed ambient project build metadata during zero-dependency Core
observation. The native fixture now copies the exact protected source artifact
and role policies, asserts its Core digest equals the candidate, and qualifies
that clean artifact. It does not ignore installed packages or change the
zero-package assertion. A separate real-interpreter proof accepts the clean
artifact and rejects the identical Core plus a source build egg-info directory.
Installer observation now includes the actual typed rejection diagnostic.
Native CI retains JUnit plus observed CR/FR/FG identities and handoff receipts.

The earlier original-dependency sealed-role run passed three cases, including a
same-Core test-package update/rollback. After the ownership changes its final
native and complete business qualification still need revalidation. The old
82cb292f candidate's complete offline run was explicitly stopped after CI proved
the ownership defect; its partial cases and prior batch time limit are retained,
not reported as a full-suite pass.

Original application dependency versions are unchanged. PR #137, full business
Gate 3, packaging/installation authority protection, independent same-tree human
Core review, Linux qualification and operator adoption remain HOLD. No active
environment, existing Core/service, real user task or production source has been
changed. Draft publication on the authorized branch is for the requested human
review and does not waive the existing merge/release guard.

The final atomic launch byte tree has since passed 124 focused role, seal, IPC,
cancellation, installation and Core-boundary cases, plus three real Windows
sealed-role cases using the unchanged original application dependencies. The
observed Core PID stayed constant through the fixture dependency update, failed
candidate and explicit rollback; durable effects remained exactly once. This
fixture is not the PR #137 security package upgrade.

The complete product cold qualification and prepare also passed in the separate
zero-dependency Core and original-dependency Functions runtimes: 666 modules and
728 resources, 77.735 seconds cold qualification and 26.062 seconds prepare.
No external product route was activated. The complete offline 9,032-case scope
reached its 1,800-second outer deadline after about 66%; the preserved exit 124
and incomplete JUnit do not constitute a suite pass. A full-scope rerun has a
5,400-second outer budget, retaining the same markers and product timeouts.

Windows CI now runs that complete offline scope after native role qualification
and retains its JUnit even on failure. HCC CI recognizes the explicit Core-major
authorization label and passes both existing guard flags; it still invokes the
same major-version and independent exact-tree review checks. Real execution of
its three authorization states rejects missing/unrecognized authorization and
continues to reject the authorized candidate for its missing independent review.
It does not mark source review, Gate 3 or release accepted.

The completed original-dependency offline run at 424141a7 returned 8,977 passed,
23 failed and 33 skipped (246 deselected by the unchanged markers, 18 subtests
passed), in 2,468.38 seconds. Its source bytes stayed identical throughout;
this is a completed failed scope, not a parity pass. The corresponding PR
Windows CI returned 8,969 passed, 32 failed and 33 skipped.

Read-only control at the untouched pre-migration 6f78ac7f source reproduced all
23 local failures. The original source also reproduced seven API availability
failures when Codex was absent from PATH, explaining seven CI-only cases without
changing assertions. The real GNU patch LF/CRLF mismatch is toolchain-dependent
and remains unadjudicated. These baseline results do not waive Gate 3.

The other CI-only failure was the FYI migration guard's unavailable historical
Git baseline. The workflow now fetches full history. Running the actual unchanged
validator against a private shallow object database reproduced its rejection;
fetching full history made it pass. No extra source checkout or product branch
was created for that proof, and historical-content assertions were retained.

The Functions-side version/topology follow-up projects Core and Functions
runtime IDs independently from their admitted fingerprints, including after
private topology serialization. Legacy/missing Core receipts remain unknown.
Detailed version cards retain HTML escaping and hide interpreter/manifest paths.
Wrong Core-role observations fail before instance/product work. Existing runtime
guidance now distinguishes the v5 candidate from its preserved v4 baseline and
historical restart permissions. The affected version, handoff, supervisor,
language and FYI modules passed 136 cases; the complete FYI primer stays within
the existing budget at 8,354 characters.

Original application pins remain unchanged. Source review, failed business
parity, complete Linux/native qualification, PR #137 security adoption, release
packaging/authority protection and operator adoption remain explicit HOLDs.

## Windows patch and POSIX qualification follow-up

The 7a912b0d CI failures are retained: Linux private launch lost the venv
invocation path when resolving the interpreter symlink, and the reference
builder rejected CPython's source aliases before materializing a sealed copy.
Fingerprint observation now keeps the absolute invocation entry. OS process
image verification remains resolved and exact. Reference capture materializes
only root-contained aliases into fresh regular files, rejects escapes/cycles,
checks copy consistency, and leaves source runtimes unchanged. Newly built
POSIX venvs drop only their redundant internal lib64 alias before sealing;
admission still rejects every link in an installed runtime.

The Windows GNU port used in CI strips CR even with binary file input. Uniform
CRLF targets now select the existing strict in-process diff engine before any
external tool. LF native verdicts remain authoritative; invalid context still
rejects without target writes. Original Fingerprint and voice assertions remain.
The new negative route checks failed before repair; all 62 patch/Fingerprint/
voice cases pass on the original split Functions dependencies. Logical-entry,
manifest, reference-copy and actual launcher coverage passes 86 native Windows
cases; three new defect cases failed before repair. Ruff passes.

This changes protected Core bytes after 4a35c21f, so the former protected digest
is historical evidence only. External qualification must bind the new exact C;
it does not modify sealed runtime files. Linux CI, the complete original-version
parity scope and final exact-tree human review are still pending at publication.
No PR137 version, production process, active Python environment or service has
been changed.

## Completed original-version parity and runtime protection

At exact source 030689c4e48de6eeebf718a1ccd50e1a747a760a, the complete
offline original-version suite passed locally in the separate test-tool FR:
9,021 passed, 33 skipped, 246 excluded by unchanged scope markers and 18 subtests
passed (2,494.89 pytest seconds; 2,501.50 command seconds). Source tree
219f4c20664a2e0109bf551c079906609b99c8a0e34187e3b0e1cd2816a8ab75
stayed identical. Windows push CI 42058021617 independently passed the same
9,021 + 18 subtest scope, 33 skips, in 1,386.99 seconds; its 80 role checks,
134 startup contracts and 68 patch/approval checks passed. Linux passed 75
role checks (5 Windows-only skips) and 955 curated Core checks (7 platform skips).
Marker exclusions, skip reasons and original failure reports remain retained.

A new full actual-product cold preparation on native Windows used zero-app
Core and the original 50-app FR: qualification 83.782 seconds, prepare 29.281
seconds, 666 modules and 728 resources. No external routes were activated and
both owned processes retired normally. Their CR/FR byte seals remain valid.

A follow-up Tool Registry red test showed that deriving protection only from
current FR sys.prefix omitted the separate Core. Functions now derive exact
Core/FR/base and qualification-file targets from admitted fingerprints and the
fixed installer selector. Both bound old and selected new FR stay protected.
Invalid selection denies execution; independent workspaces/development Python
and valid read-only inspection stay allowed. Core code bytes do not change.
The added actual-role/handoff, old permission, supervisor and version scope
passes 68 cases, with 3 POSIX-only skips on Windows, in 352.96 seconds. The
earlier red is preserved. First target derivation measured 3.847 seconds;
subsequent independent writes/core denials took about 5 ms.

Post-suite seal checking correctly rejected bytecode caches generated in the
test-tool driver. That driver is retained as test evidence and is not an
adoptable product runtime. Both real product-role environments/base trees
still match their exact seals; no cache exception or hash assertion was added.
The test driver includes six separately declared test-only distributions and
is distinct from the actual 0/50 role qualification.

No PR137 package version is adopted yet. Its exact cookie/reference-lifetime
probes reproduce two expected failures against old packages. Only reviewed
public wheel inputs were cached. Native account/WAC, independent human Core
review, formal release packaging and operator adoption remain separate gates.

## Canonical Functions-only build

The builder now exposes build_reference_functions and the build-functions
CLI operation. It accepts the existing qualified Core reference, materializes
only a fresh FR/base from the frozen reference, checks cross-role compatibility,
and revalidates the same CR before returning. It never publishes the fixed
selector or performs adoption. The existing actual same-Core dependency fixture
test now uses this canonical API instead of manually cloning/qualifying FR.
All original activation-failure, rollback and durable-counter assertions remain.

Native Windows role/build/profile/seal scope passed 43 cases in 501.39 seconds
(command 503.00) on exact tree
389e0c152563dd31d73c89bacf56d853fab5e35e496fe37c240c83bd60ccc19f.
The added no-write test rejects output within the existing Core base. CLI
absence was reproduced before implementation; CLI help and Ruff pass after.

The ee760645 Windows/Linux CI failure was one new fixture's observation through
the editable build workspace, whose project distribution metadata is outside
the sealed FR site. The fixture now observes through the same exact minimal
Core code artifact used by native qualification. A separate actual control
injected synthetic workspace egg-info: unchanged dependency validation rejected
it, then qualified the same untouched FR from the clean code artifact with the
same FR_ID. This is fixture isolation; no seal or package assertion was relaxed.
Ruff and ten builder/profile boundary cases pass after the correction. Final CI
for the combined checkpoint is still pending. No PR137 pin has changed.

## Verified security handoff and release-source checkpoint

The original-pin 030689c4 offline Windows scope completed with 9021 passed,
33 skipped, 246 deselected and 18 subtests; its branch/PR runtime CI passed.
2940a5f4's subsequent actual-role fixture isolation passed Windows and Linux CI.
Only after those results, e2c265f6 changes the five reviewed PR137 package pins
in Functions and derives the standard/Portable hash locks. PR137 is not merged.

The real 0/50 original and upgraded product installations qualified and prepared
the same 667-module / 728-resource FG. One owned Core PID 42520 retained its CR
while upgrading the actual five packages, rejecting an injected activation
failure and rolling back, explicitly downgrading, then performing a second
upgrade. The durable effect counter stayed 1 in all five states; all owned
workers and the temporary Core retired. Both product environment seals remained
valid. The new-package security scope passed 17 checks; the two original-package
failures are preserved. This is runtime evidence, distinct from source CI.

e2c265f6 branch Windows/Linux runtime CI passed. Its synthetic PR-merge Windows
scope exposed a real pre-existing live-audit export ordering defect (9029 passed,
1 failed, 33 skipped). Identical timestamps/random IDs could advance the cursor
past unexported events, and the first 1000-row time-ordered window could stall
later pages. Live export now queries the existing ledger by chain cursor/order
before LIMIT. General chronological queries, ledger hashes and durable schema
are unchanged. Three deterministic red cases (timestamp ties, clock reversal,
1002 rows) turned green; all 36 live-export/ledger/export/anchor/WORM checks pass.

Release-source changes cover installer-owned CR/FR selection, immutable role
capsules and final-path qualification, fresh copied build Python, hashed npm
Functions installation, Windows/Linux launchers, and the enterprise image.
Git-less product admission requires a code-file snapshot anchored beside the
actually bound qualified FR; a package-local snapshot alone grants no authority.
Portable relocation preserves complete role byte seals and IDs; records are
regenerated outside the runtime trees at the final installed path. Existing
installation/data lineage and whole-program failure rollback remain.

Native Windows relocation/catalogue scope passes 2 tests (293.55 seconds);
package/launcher scope 90, sidecar/permission scope 67 with 3 platform skips,
distribution/publication negatives 14, and final launcher/runtime selection
scope 65 pass. These focused scopes overlap and must not be summed. Ruff,
JavaScript and Bash syntax checks pass. Actual npm/Portable artifacts, image CI
and the final complete offline scope remain pending at this source checkpoint.

No existing Core, active environment, instance state or production service was
modified. Independent human review of the exact protected Core tree, approval
of main integration, account/WAC/client qualification and cold operator adoption
remain separate release gates; no reviewer record is fabricated.

## Real artifact failures retained and addressed

The first b37127a7 public Portable build rejected a normal setuptools .pth in
the separately delegated media helper. Core/FR startup hooks remain forbidden.
Sidecar resource verification now checks the complete declared file set, hashes,
regular-file/reparse rules and open-file identity before/after hashing; it does
not assign runtime roles or execute helper files. The same .pth is still rejected
by the unchanged Core runtime verifier. Added/missing files, modified hooks and
escaping resource paths are rejected by the new negative scope.

The first real npm tarball installed and sealed both roles in a fresh private
directory, then full Core-driven product qualification rejected a native CFFI
DLL in the deep Windows path. The generic Function contract was
too small to catch this. Installer preflight now uses the canonical complete
Function manifest/probe and source publication before publishing active.json.
The actual deep installation is rejected by that probe; previous selection
bytes remain unchanged. Final positive qualification uses a short fresh local
test data root, reflecting Windows native-loader path limits.

The first actual enterprise image exposed PYTHONPATH contamination: the sidecar
pip subprocess saw the disposable seed site and skipped packages absent from
the sidecar itself. The provisioning CLIs are proven stdlib-only; their build
commands now run without that ambient seed path. Standard and Kubernetes image
profiles are qualified separately, including complete imports and the optional
backend. The packaging doctor follows the exact hashed-extra pipeline.

Full documentation remains packaged. Deployment-specific address literals are
generalized rather than excluded from leak detection; the canonical Functions
hash lock is explicitly tracked. No assertion, Core startup restriction or
runtime byte-seal rule was disabled. Actual rebuilt artifact and CI receipts
are recorded separately from these source fixes in PR140.
