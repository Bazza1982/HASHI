# HASHI Documentation and Beta Preparation Review — 2026-09-11

Status: documentation cleanup implemented; package/documentation checks pass.
This is not a Beta release acceptance or a publication of the development branch.

Scope: HASHI2 checkout, local main based on ea20f57c.
Approval: the current user requested README/release-page corrections and minor
documentation cleanup before a future npm repack and installation pack.
The preceding product-positioning edits are included in this review.

Owners: Frontend Connectors for public documentation and user guidance;
PCM for the FYI reference projection; PAO for release/adoption guidance;
Platform Configuration for npm documentation packaging.
No protected Core implementation changed.

## Changes and reasons

| Change | Reason |
|---|---|
| README reduced from 1,885 to 168 lines | Make purpose, capabilities, installation, maturity, and navigation visible without an embedded operating manual |
| Separate usage, configuration, integration, troubleshooting, and release guides | Keep operational detail discoverable and avoid repeating the full changelog |
| Root INSTALL and launcher README point to the canonical installation guide | Remove divergent setup paths, wrong clone-directory case, old direct-script onboarding, and missing prerequisites |
| Registry queries and explicit npm version selection | npm latest was 1.0.1 while source metadata was 4.0.0-alpha.2; bare installation could silently select a legacy package |
| Source/npm/Python/Portable scope and pre-release channel guidance | An npm tarball is not a standalone offline installer; the Python artifact contains Nagare/Flow rather than the full application |
| npm allowlist includes six previously unbundled user documents | The installation guide and new navigation must work inside an actual npm package |
| Architecture, API, working-mode, roadmap, and candidate-note corrections | Shared ingress belongs to Functions; backend selection works in both modes; endpoint/model choices derive from current configuration |
| WhatsApp and privacy wording | Current WhatsApp uses neonize; authentication, encryption, local state, and external data transfer are distinct claims |
| Legacy kill/restart troubleshooting removed | Duplicate instance and port errors require scoped inspection, not computer-wide process termination |
| OpenClaw attribution corrected | The upstream author is Peter Steinberger; provenance remains acknowledged without defining HASHI's current product category |
| FYI condensed to 11,935 characters | Existing 25,029-character baseline exceeded its 12,000-character loader limit and silently lost its second half |
| Release checklist and versioning clarification | GitHub Latest excludes pre-releases; npm channels and installation-pack provenance need separate verification |

The FYI summary retains authority, delivery, ownership, migration, mode, media,
memory, scheduling, presentation, and release boundaries with links to detailed
owners. Its reader limit and runtime code were not changed.

## GitHub changes verified online

Updated titles and added a dated historical notice to:

- v1.0.1 — Legacy release (March 2026)
- v2.0.0 — Legacy release (March 2026)
- v4.0.0-alpha.1 — Historical pre-release (May 2026)

The notices point to current source/installation guidance and explain the
independent npm channel. Original notes remain intact below the notice.
Release IDs, tags, pre-release flags, and release count were verified unchanged.

GitHub still identifies v2.0.0 as the newest stable Latest. Its API excludes
drafts and pre-releases, so making an Alpha/Beta stable merely to change that
badge would misstate maturity. The current release-page guidance now makes the
legacy status explicit.

No new GitHub tag or release was created. Source candidate metadata remains
v4.0.0-alpha.2 / Python 4.0.0a2. npm dist-tags were inspected, not changed.

## Verification evidence

- Package JSON and Python TOML parse; descriptions match and ecosystem version
  identities agree.
- Internal Markdown targets and anchors were checked with a CommonMark parser,
  including same-repository GitHub links against local source:
  266 documents, 511 checked links, no missing targets/anchors.
- git diff --check and both protected-Core guard/manifest validation pass.
- npm package red proof: before the allowlist correction,
  tests/contract/test_npm_package_contract.py reported 1 failed, 1 passed and
  named the six missing documents.
- Final focused command — 15 passed:

  ~~~bash
  python -m pytest -q tests/test_agent_fyi.py tests/contract/test_npm_package_contract.py tests/test_npm_command_surface.py
  ~~~
- Core gate: python -m pytest -q — 637 passed in 69.31 seconds.
- Offline product suite:
  python -m pytest -q tests -m "not contract and not live and not platform and not real_wall_clock"
  — 4,230 passed, 4 failed, 165 deselected, one dependency deprecation warning,
  in 523.79 seconds.
- The four failed nodes were reproduced on an isolated, unmodified ea20f57c
  checkout: 4 failed. This establishes that they preceded these documentation
  edits. The FYI failure is now fixed and its complete module passes.
- A real npm tarball was unpacked outside the checkout. Its own Node entry
  served help, nested command help, and version. The final inspection package
  contained 764 files. Eight public documents had 56 valid local navigation
  links; the packaged FYI loaded completely. Nine reviewed documents matched
  their final source bytes. These checks used the unpacked files.
- Inspection tarballs are local validation artifacts built from a dirty review
  checkout, not release/install acceptance artifacts. Build again from the
  reviewed clean source when publishing.

The offline product suite is a recorded run, not a final all-green result.
After the documentation-only FYI fix, focused checks were rerun rather than
claiming a second full-suite pass.

## Three existing failures still requiring release reconciliation

| Test | Observed conflict | Follow-up |
|---|---|---|
| tests/test_context_compaction.py::test_her_stage_error_preserves_hashi_capacity_code_without_stage_deadline_fields | Test expects PROVIDER_UNKNOWN; the implementation preserves explicit VENDOR_UNDOCUMENTED | Reconcile the legacy expectation with the current typed Adapter error contract |
| tests/test_superloop_receipts.py::test_unrelated_or_blocked_reply_does_not_wake_controller[recipient] | Unknown-recipient routing consults Agent Move state; the fixture has no agents.json and raises AgentMoveError | Exercise the boundary with a valid instance fixture and decide separately whether missing configuration needs handling |
| tests/test_workbench_agent_management.py::test_agent_activation_persists_before_start_and_can_be_disabled | Fixture has one active Agent; test expects successful deactivation but implementation returns 409 under the last-active-Agent invariant | Separate ordinary activation/deactivation from the protected last-Agent case |

These tests and their product implementations were left unchanged. Their failures
are not evidence of regressions from this documentation change, but a Beta release
should reconcile them before claiming the offline product gate is green.

## Publication and live-adoption boundary

GitHub release-page metadata is online. The edited README, guides, package
manifest, and review record are local, uncommitted work for the next coordinated
source/package publication. Local main was 119 commits ahead of origin/main;
this review did not publish that unrelated development range.

No npm publish, distribution-channel promotion, installer release, runtime
restart, shared replacement, Agent reboot, or live FYI refresh was performed.
The next publisher must choose the reviewed version under the
[versioning policy](HASHI_VERSIONING_POLICY.md), resolve the remaining gate
conflicts, and follow the [release checklist](RELEASE_CHECKLIST.md) for exact
source, npm integrity, platform installation, and migration evidence.
