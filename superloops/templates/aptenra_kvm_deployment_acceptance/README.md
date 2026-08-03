# Aptenra KVM Deployment Acceptance Superloop

## Purpose

Rapidly find and repair Aptenra product defects by repeatedly building from
the latest committed `main`, upgrading the Windows 11 x64 Lenovo through KVM,
and testing only the zones affected by the current bug batch. Stable zones
retain qualification through a compact boundary smoke unless their code,
shared dependency or observed behaviour changes. Packaging remains a supported
failure route, but it is no longer the centre of every round.

The loop stays small:

```text
latest committed main + open APB regressions
-> fresh immutable candidate
-> KVM in-place upgrade + identity/startup smoke
-> deep-test OPEN/REOPENED or diff-affected zones
-> compact boundary smoke for QUALIFIED zones
-> record all safe-to-continue failures; do not change code mid-pass
-> deduplicate and diagnose the complete bug batch
-> fix the batch in main + add automated regressions + update APB records
-> next immutable candidate + active-zone retest
-> milestone candidate only: complete cross-zone acceptance
-> success: close device-verified APBs, clean scratch media, leave candidate installed
```

## Target boundary

- The target is a representative Windows 11 x64 PC, currently Lenovo
  `APT-HW-0002`.
- Installation, configuration, permission prompts and acceptance are performed
  through KVM like a human user or IT technician.
- No agent on the target may substitute for visible KVM acceptance.
- Local PowerShell opened through KVM is allowed for diagnosis.
- HASHI Remote may assist read-only diagnosis when already authorized, but it
  is not an acceptance requirement.
- Administrator access is used only when Setup, Repair, Uninstall, diagnosis or
  Level 5 legitimately requires it.
- The package must not rely on a source checkout, Python, Node, Git or other
  undeclared developer tooling on the target.

## Runtime configuration

Secrets come from an operator-supplied secure file outside the repositories.
Secret values must never enter Superloop state, screenshots, logs, journals,
task commands, commits or evidence summaries.

For the current acceptance profile:

- DeepSeek Lite: Primary;
- DeepSeek Pro: Action and Escalation;
- GLM credential: save and validate only when requested, with no active route;
- OpenRouter: the product's default audio route;
- Telegram: excluded;
- remote connections: excluded from acceptance.

The test checks that configuration can be entered safely, activates the
expected route and survives an Aptenra restart. It does not benchmark model
quality or force a complex escalation.

## Debug zones and qualification

Keep one small zone table. Do not create a second gate system or duplicate bug
database. Each zone is `ACTIVE`, `QUALIFIED` or `REOPENED` for a round.

| Zone | Boundary | Deep qualification | Normal candidate treatment |
| --- | --- | --- | --- |
| `Z1` | Setup, MSI, Upgrade, cache and installed identity | Upgrade fidelity plus conditional lifecycle | Identity, payload and launch smoke only |
| `Z2` | Host, Core, frozen Runtime and lock identity | Health, single instance, restart and architecture contracts | Health and exact runtime-lock smoke |
| `Z3` | Typed Chat, request delivery and session isolation | Five-turn continuity, correction, New Chat/reset, cancel and hidden continuation | One connected typed/read turn |
| `Z4` | Microphone, Push/Wake, KWS, Whisper and TTS | Real-device acoustic and activation cases | One Push turn; Wake only while qualified |
| `Z5` | Voice Agent/Chat/Mixed routing and memory | `3 x 2` routes, five-turn continuity, handoff and mode boundaries | One representative voice route |
| `Z6` | Action, approval, Risk and final review | Safe file/action job, approval, review correction and restart recovery | One safe read/action boundary |
| `Z7` | Shell UI, Activity, Settings, locale, Companion and Browser | Only the affected UI subzone; do not reopen unrelated surfaces | Launch plus visible shell smoke |
| `Z8` | Workbench, Remote and Offline boundaries | Representative journey and recovery for the affected subzone | Health plus one representative request |

A `QUALIFIED` zone is isolated from repeated deep testing. Reopen it only when:

- a file owned by the zone changed;
- a shared dependency or cross-zone contract used by the zone changed;
- its compact smoke fails or a new field symptom appears; or
- the candidate is selected for milestone/promotion acceptance.

Before each repair batch, compare the last installed product source with the
new source using `git diff --name-only`, map changed files to zones, and record
the affected-zone list in the round note. If ownership is ambiguous, reopen the
smallest plausible pair of zones rather than the whole product.

Every ordinary candidate runs one compact cross-zone smoke:

```text
installed launch
-> one typed reply
-> one Push voice turn
-> one safe read/action boundary
-> Workbench and Remote health
-> clean exit
```

Only a milestone or promotion candidate reruns the complete user-function
matrix: typed and mixed-input continuity, Voice `Agent/Chat/Mixed x Push/Wake`,
model routes, cancellation, recovery, major product surfaces, safe file
roundtrip, Risk boundaries, restart/reboot and Program Files integrity.
Telegram configuration and real remote actions remain excluded unless
explicitly brought into scope.

## Batch discovery and repair round

1. Lock the latest committed Aptenra `main`, the packaging commit and the open
   Product Bug Register regressions in an isolated clean checkout. Diff against
   Lenovo's installed source, map files to zones, and declare only the
   `ACTIVE/REOPENED` zones for the round. If the operator checkout is changing,
   wait for its intended work to be committed; never package staged or
   uncommitted content.
2. Run tests owned by affected zones plus the full Python suite and the compact
   Electron smoke before spending time on a freeze. Then build one fresh
   immutable candidate.
   Reuse the last known-complete test interpreter and record `sys.executable`;
   before the full suite, import its required test/runtime dependencies and run
   one focused case. Do not discover an incomplete venv by repeatedly starting
   the whole collection.
   Run a mini-media rehearsal only when packaging code changed or the last
   failure points to packaging.
3. Transfer by manifest/SHA and perform a visible KVM in-place Upgrade. Verify
   exact identity, preserved user state, launch and disk baseline before the
   functional pass.
4. Deep-test every `ACTIVE/REOPENED` zone and run only the compact boundary
   smoke for `QUALIFIED` zones. Record each failure with a provisional case
   reference and continue every test that is still safe and meaningful.
   Dependent cases become `BLOCKED-BY-APB-NNN`, not silent skips.
5. Isolate test state by zone: use a dedicated Chat, test directory, marker,
   settings snapshot and log time window. Restore only the settings changed by
   that zone before moving to the next one.
6. End the discovery pass only after all active-zone cases are exhausted, or immediately
   for security/privacy failure, data-loss risk, unsafe permission bypass,
   untrustworthy installed identity or a crash that makes further testing
   impossible.
7. Triage all observations together: discard harness/environment incidents,
   merge duplicate symptoms by root cause, assign permanent APB IDs, and
   diagnose the whole code batch before editing.
8. Fix the batch in current `main`. Add an automated regression and permanent
   KVM case for every APB, update the register to `FIXED-IN-MAIN`, then build the
   next immutable candidate.
9. Rerun the affected zones plus compact cross-zone smoke on the next candidate.
   Close an APB only after its original Lenovo path passes. Promote a zone to
   `QUALIFIED` when its active cases pass and the boundary smoke remains healthy.
10. When all zones are qualified, build or nominate one milestone candidate and
    perform the complete cross-zone acceptance once. Repeat only zones exposed
    by that acceptance, not the entire unaffected product.

## Observation contracts

Use the narrowest invariant that actually belongs to the boundary under test:

- Hash every immutable Program Files file against the candidate release proof.
- Compare exact ProductCode, version, Setup/MSI/proof SHA-256 and cache-slot
  identity for deployment transactions.
- Treat user state as mutable runtime data. Prove preservation with required
  files, credential-provider presence, schema/version and bounded semantic
  checks; do not fail because a healthy launch added logs, history or state.
- Never print or hash secret plaintext. Credential checks use the explicit
  standard-user DPAPI directory and report only provider/file presence.
- Use an explicit standard-user state root even from an elevated diagnostic;
  do not infer it from the administrator process's `LOCALAPPDATA`.
- Every controller result records `candidate`, `operation`, `ok`, observed
  identity and the exact evidence path. A screenshot is a visible witness, not
  a replacement for the JSON transaction/audit result.

Readiness is condition-based, not sleep-based. Poll the smallest observable
contract for up to 60 seconds (or a separately justified timeout), stop early
when it passes and retain the last state when it expires. A fixed 10- or
25-second screenshot must not turn normal startup variance into a candidate
failure.

## Conditional negative-lane discipline

Run destructive and failure-injection journeys after the positive and
preservation baselines. For an Upgrade rollback test:

1. prove working N can launch and capture its immutable/user-state baseline;
2. pre-arm the injector with elevation before starting N+1 Setup;
3. prove the injection happened inside the intended transaction, including the
   exact file/event and before/after observation;
4. require N+1 failure plus complete N identity, payload, cache, user-state and
   real-launch recovery;
5. perform a final clean Upgrade to the accepted N+1 and repeat its compact
   terminal-state audit.

An injector that cannot access protected ProgramData is a harness failure, not
proof of rollback. Do not classify the resulting uninjected successful Upgrade
as a product failure, and do not append it to the Packaging Failure Journal.

## Failure routing

- Product/UI/runtime defect: assign or update an `APB-*` record in
  `/home/lily/projects/Aptenra-main-yuhuan/docs/aptenra/APTENRA_PRODUCT_BUG_REGISTER.md`,
  then fix and commit Aptenra `main`. Never write it into the Packaging Failure
  Journal.
- Freeze/media/Setup/install/startup/Upgrade/Repair/rollback/Uninstall defect:
  fix the authoritative source, append one evidence-backed entry to the unique
  Packaging Failure Journal and synchronize its Current Index and Production
  Specification.
- Target/network/KVM/provider outage: restore or retry the environment with the
  same candidate. Do not disguise it as a product fix.
- Controller typo, display-only assertion or transient transfer retry with
  proven zero candidate/target impact: retain it in round evidence and correct
  the current experiment. Do not create PFJ noise unless it obscured the
  result, changed target state, exposed a reusable workflow defect or repeated
  after an existing prevention rule.
- Never patch an installed immutable candidate and then accept its media.

The unique packaging records are:

```text
/home/lily/projects/Aptenra-packaging-side-by-side/docs/aptenra/APTENRA_PACKAGING_FAILURE_JOURNAL.md
/home/lily/projects/Aptenra-packaging-side-by-side/docs/aptenra/APTENRA_PACKAGING_FAILURE_CURRENT_INDEX.md
/home/lily/projects/Aptenra-packaging-side-by-side/docs/aptenra/APTENRA_WINDOWS_INSTALLER_PRODUCTION_SPECIFICATION.md
```

Only the first file is the Journal. The other two are synchronized subordinate
views. No second Journal is created.

## Storage and media retention

- Before a full freeze/CAB build, check build-host and Lenovo free space. If it
  is insufficient, remove stale reproducible scratch media before continuing;
  treat this as housekeeping, not a candidate failure.
- Keep complete immutable media only for the accepted current candidate and
  previous LKG. Preserve small manifests, release proof, transaction logs and
  acceptance evidence for older candidates, then remove their reproducible
  payload, CAB, Workbench, native-entry and publication copies.
- Target `install-staging` is transfer scratch space. After acceptance, retain
  its small manifests in support evidence and remove the downloaded Setup/MSI/
  CAB tree. The protected `ProgramData` current/previous-LKG Installer Cache is
  the authoritative Repair and rollback source and must never be removed by
  this cleanup.
- Calculate transfer reuse from final signed SHA-256 values before choosing a
  delta path. Candidate-specific Authenticode signatures can change every CAB,
  including CABs whose large model payload is otherwise unchanged; in that
  case transfer the verified full media instead of spending time attempting
  ineffective reuse.
- Disk retention cleanup and controller-only measurement corrections remain
  round evidence. They do not create Packaging Failure Journal entries unless
  an actual packaging lifecycle defect is demonstrated.

## Exit condition

The debug loop exits when every zone is `QUALIFIED`, every observed APB is
closed or explicitly ruled out as non-product, compact cross-zone smoke passes,
and there is no packaging blocker. Internal promotion additionally requires one
milestone candidate to pass the complete cross-zone acceptance. The accepted
candidate remains installed.

Repair, downgrade, Uninstall/Reinstall and rollback are conditional in this
function-focused loop. Run them only when packaging code changed, a result
points to packaging, or the candidate is being promoted beyond internal Lenovo
acceptance.

## Keep it fast

- Read the current open/reopened APB summary and only the latest relevant PFJ
  lesson when packaging is actually implicated.
- Use an isolated clean checkout; never package from the operator's dirty
  working tree.
- Route by diff: product change means full freeze, Setup-only change means
  verified payload reuse plus new candidate identity.
- Run focused checks before a full freeze/CAB; run mini-media only for a
  packaging diff or packaging failure.
- Check disk space and apply the current/previous-LKG retention policy before
  expensive media work.
- Reuse CAB bytes by SHA-256 on both build host and target; verify every logical
  candidate file after assembly.
- Replace candidate-number-specific scripts with one Windows PowerShell 5.1
  compatible parameterized controller that emits JSON and a separate screenshot
  per action.
- Use `scripts/prepare_candidate_from_bundle.ps1` and
  `scripts/build_registered_candidate.ps1` for subsequent immutable builds;
  pass candidate identity as parameters instead of cloning another dated build
  wrapper.
- Use `scripts/serve_registered_candidate.ps1` and
  `scripts/stop_registered_candidate_server.ps1` for temporary transfer;
  candidate identity and display version are parameters, and the paired stop
  script removes both listener and firewall rule.
- Before sending a diagnostic command, prove the intended target window has
  focus. For encoded or long PiKVM keyboard input, open the shell explicitly
  and send a second `Enter` after printing completes; an empty second command at
  a prompt is harmless and prevents false non-execution.
- Split controllers by reusable responsibility (`identity audit`, `Repair
  corruption`, `rollback injection`, `post-transaction audit`, `launch
  readiness`) rather than cloning an entire candidate script.
- Capture one compact prebaseline, the authoritative deployment JSON and one
  post-audit summary per boundary. Add screenshots only for user-visible state
  transitions or a failure signature; do not sample unchanged progress screens.
- Do not stop the discovery pass for an ordinary product bug. Record it and
  continue unaffected cases so one package yields one complete repair batch.
- Reuse evidence for unchanged `QUALIFIED` zones. Every newly built candidate
  reruns only affected-zone qualification plus the compact cross-zone smoke.
- Do not reopen a qualified zone merely because another zone is being tested;
  map the source diff and shared contracts first.
- Run the complete user-function matrix once for a milestone/promotion
  candidate, not for every repair candidate.
- Keep evidence compact: one case row, one useful visible capture and the
  narrow request/log proof. Do not generate large duplicate evidence bundles.
- Do not add an independent target agent, historical gate matrix or duplicate
  safety framework.
