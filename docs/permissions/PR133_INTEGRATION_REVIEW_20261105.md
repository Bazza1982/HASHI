# PR #133 integration review — 2026-11-05

Functional owner: PAO. Engineering layer: Functions. HERV3 and Windows platform
contracts are direct validation consumers. No Core migration, deployment, reboot,
service installation or PR merge is part of this review.

## Integration decision

Integrate the complete published permission branch
`71d9e3b7a7a0b018056136ea0cb153532f84121c` with `origin/main`
`ea37129b8edad3c0420541133b41fd96dcacc6c9` by an ordinary merge on the existing
`feature-permission-sys` branch. Preserve both histories and the full upstream
file tree; no cherry-picks, new branch/worktree or force update.

The content conflict in `orchestrator/agent_move/service.py` is between two
Fingerprint comment IDs. Retain the upstream marker and both branches' executable
changes:

- Source-bound lifecycle departure history and completed return receipts remain
  derived from positive, matching transaction evidence. An unfinished move,
  failed cleanup or reused Agent name cannot erase or manufacture retirement.
- Recovery remains revision guarded, resumable and idempotent. It never restores
  over an intervening writer or blindly retries an uncertain publication.
- Both normal `agents.json` publication and object-snapshot recovery use
  `write_agent_config_json`; other files keep the shared JSON primitive.
  The installed Safety identity receives read-only access, never copied ACLs or
  access to `secrets.json`. Legacy arrays retain their read revision.
- L0/L1 behavior, L2 exact Workzone roots, ordinary-approval ceilings, epoch
  fencing, restricted HERV3 and canonical audit ownership are unchanged.

The FYI is now a compact linked index with substantial space below its 12,000
character loader limit. [Operational details](../AGENT_FYI_OPERATIONS.md) retain
the complete integrated pre-compaction guidance and its original decision links.

## Failure proof and checks

The existing native configuration-publication test now also exercises the real
Agent Move publication and snapshot-recovery entry points. The existing conflict
and private-secret scenario covers those paths too.

A process-local mutation replaced only Move's permission-aware publisher with the
generic JSON publisher: the two added DACL cases failed because the Safety reader
was lost (**2 failed**). The unmutated integrated working tree passed the complete
publication module (**13 passed**). No source mutation was persisted. These
pre-commit checks are failure proof, not the final committed-HEAD result.

## Initial failure reconciliation

The first integrated commit, `af57f664e3d255b7bc75f0a73270cbbde9e9689f`,
exposed one Agent Move and two Telegram configuration tests still injecting
faults into the generic publisher. Their real write path now uses the
permission-aware publisher. Retarget the injections, retaining conflict,
unknown-commit, exactly-one-write and preserved-data assertions. Move also
asserts that the competing writer actually ran. A process-local mutation that
drops read revisions makes the two conflict regressions fail; the repaired
Move/Telegram/Fingerprint focused group passes all five cases.

The initial Windows Fingerprint CI failed its two supervised-write cases while
waiting for the approval polling tick. Signal the same waiter used by the real
approval path; retain the existing two-second watchdog and all receipt-only
evidence assertions. No product timeout, lease expiry or approval check changes.

The first local permission run placed its fixture data below the real checkout.
The immutable-code floor correctly refused those writes, producing 16 failures.
Two identical scenarios pass with fixture data outside that protected root.
Use a separate ignored temporary-data root for final regressions, preserving
the product protection. The first 300-second tool transport wait timed out;
the completed JUnit/child receipt independently records its actual failed run.
Original failed logs and results remain evidence, not passes.

## Final committed-HEAD validation

After the integration commit, rerun these separately recorded scopes on its exact
HEAD, recording the full argv, committed tree, working-tree state, JUnit outcomes
and logs. The PR review report carries the final SHA and results, avoiding a
self-referential SHA inside its own commit.

| Scope | Selection |
| --- | --- |
| Agent Move and publication | All four Agent Move modules, remote move, config/config JSON/admin invariants, AgentDirectory and FYI |
| L0/L1, L2 and restricted HERV3 | Every top-level `test_permission*.py` plus direct consumers selected by the permission CI workflow, including native configuration DACLs, hard Workzones, approvals, recovery and audit |
| Audit | PCM canonical audit, runtime/tool/slash audit, restricted host/Worker callbacks, provider context/forensics, L1 compatibility and HERV3 adapter |
| Windows platform | Exact pytest selection from `windows-runtime-contract.yml`, on native Windows |
| Main integration consumers | All top-level test modules added/changed on main since the common ancestor |
| Curated core gate | Bare `python -m pytest -q`, without inventory expansion |
| Static/CI | Protected Core default/staged/full-branch guards, whitespace, focused lint, FYI links/size and GitHub workflows for the exact pushed head |

Scopes overlap and must not be added into one coverage count. Skips are reported
with their platform reason. Any failure retains its original result and is
investigated rather than relabelled as a pass. No live service or real-provider
acceptance is inferred from offline/native regression tests.

## Remaining Windows acceptance

This review does not close any formal WAC01–WAC34 case. The normative
[Windows WAC table](L2_WINDOWS.md) still requires the applicable
version/platform/frontend evidence. The earlier
[Intel qualification](L2_INTEL_LOCAL_QUALIFICATION_20261105.md) records real
observations for a different complete commit and only partial scenario coverage;
it is not fresh native-service acceptance of the integrated head.

Material remaining combinations include:

- WAC02/WAC11/WAC27–WAC29: full named external frontend and approval-environment
  acceptance; application credentials or loopback cannot stand in for a human.
- WAC16/WAC21: concurrent cross-task isolation and once-grant contention.
- WAC17–WAC20: the remaining path/race, import/export, network and disclosure matrix.
- WAC22–WAC26: the complete revocation, failure, lifecycle and uncertain-effect matrix.
- WAC30/WAC31: clock changes, backup/restore, upgrade failure and rollback.
- WAC03–WAC08/WAC12–WAC15/WAC32/WAC34: the remaining installation, tamper,
  execution and truthful-control-report combinations on the integrated version.
- WAC01/WAC09/WAC10/WAC33: formal platform acceptance of compatibility, decision
  provenance/replay and independent-backend fallback.

Immediate profile reclamation and delayed recovery after Windows maintenance
remain different facts. Other operating systems, independent backends, remote
Compute execution, real model providers, production adoption and user-manual
frontend acceptance remain outside the previously qualified local HERV3 result.

Keep PR #133 Draft for another review. Do not merge or advertise complete L2.
