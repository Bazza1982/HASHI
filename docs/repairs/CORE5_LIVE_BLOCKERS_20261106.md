# Core v5 non-production live-test blockers

Owner: Functions (Frontend Connector command handoff, PAO Scheduler API client,
Remote availability and qualified Python child launchers). Protected Core,
dependency locks, process protection and admission contracts are unchanged.

The reviewed PR candidate was b0c30e6b3c8af97e7f729b71582085cfee8b3669.
The local follow-up c4ac7df6dcdbc254a051fb253c64a64df87478d2 added Remote's
bytecode-safe launch and its tests/documentation. That commit was not pushed.
The previous HASHI7 live run therefore tested a different, locally adopted
candidate from GitHub PR #140. Its results cannot qualify the older PR HEAD.

The repair candidate retains that complete history on the existing development
branch. Freeze its exact clean commit before building new independent runtimes
and adopting it on HASHI7. Keep NPWB2's exact frontend build and loaded assets
in the same acceptance receipt. Do not claim GitHub contains the candidate
while pushing is prohibited.

## Repairs and regression boundaries

- Background command admission uses the existing trusted command context.
  Connector, owner, Session, generation and idempotency survive the handoff.
- Personal-profile Scheduler deletion uses a Run/Agent/instance/audience scoped
  capability, an authenticated current task revision and the existing
  conditional writer. No administrative credential enters an MCP descriptor;
  no owner, cross-Agent or Governed management rights are delegated.
- Remote health distinguishes local discovery availability from external peer
  trust. Rejected foreign peers remain rejected and visible. Their
  advertisements cannot make an otherwise healthy local service unavailable.
- The MCP tool process and generated native Python hooks use -B. Strict
  installer seals still reject unlisted cache bytes. Preserve the old polluted
  runtime as failure evidence; construct a fresh qualified replacement.

Focused red/green receipts belong to the run evidence directory, rather than
being copied into source as approval. Native coverage executes real MCP/hook
entrypoints with bytecode environment variables absent, then invokes the
official runtime selector. No validator exception or cache exclusion is added.

## Required new live evidence

Start a new acceptance run only after cold adoption and READY. Record the new
Core PID as baseline, exact source/FR/CR identities, dependency inventories and
the frontend candidate. Exercise normal tasks, frontend stop and cancellation,
background jobs, Scheduler writes, execution-time Worker failure, automatic
recovery, Agent lifecycle controls, refresh/reconnect/drafts and mobile input.
Worker recovery and Functions replacement/rollback must preserve that Core PID.
A whole-instance restart used to recover a failed test is a failure, never an
acceptable new baseline within the run.

Re-run official Core and Functions qualification after normal operations and
replacement, and verify all test-owned work is cleaned up. Runtime identity,
loaded code, user-visible result and independent review are separate evidence.
No production mutation, L2 activation, merge, push or manufactured review
record is authorized by this repair.
