# HASHI Core v6 quiesce-recovery review addendum

Review time: 2026-11-16T11:30:23+11:00
Branch: `core5-fix`
Base: `main`
Previous reviewed PR head: `fc39be50`
Protected-Core digest: `sha256:b627c549cb5fa7e1bbaeab17e4955e585b7ce2c09f13069645ada7595fb848df`

## Approval and reviewer boundary

This addendum is an AI technical review by the same OpenAI Codex/Hashiko root
agent that implemented the candidate. It is not an independent-human review.
The current human owner explicitly directed this narrow continuation of pull
request 166 in request `req-hashiko-2026-11-15_221221-0007`, including the
specific `kernel_process.py` partial-quiesce timeout repair. That direction
approves review of the branch candidate only; it does not approve merge,
deployment, production restart, or adoption.

## Protected scope reviewed

The only new protected-Core behavior is in
`orchestrator/kernel_process.py`. A replacement now records that quiesce was
attempted before awaiting its receipt. If the receipt times out or is otherwise
lost while the previous process remains alive, failure recovery sends the
existing bounded `resume` request. This covers the uncertain state where the
old process closed intake before its response was lost.

The committed-retirement rollback path is unchanged. Candidate cleanup,
previous-release restoration, failure receipts, sanitization, stage timings,
and rollback error propagation are unchanged. No runtime scan, integrity
check, lifecycle owner, background loop, or security mechanism was added.

## Focused evidence reviewed

- The new partial-quiesce regression makes the old peer pause intake and then
  raises `asyncio.TimeoutError` instead of returning a receipt. The candidate
  is closed, `resume` is sent, the old peer accepts a subsequent request, and
  the rollback receipt reports success.
- The partial-quiesce regression and the existing replacement root-cause and
  rollback regression passed together: 2 passed in 0.27 seconds.
- The affected Remote, AgentMove, and Kernel modules under Python 3.12 reached
  145 passed and 2 platform skips. The only pre-commit failure was the expected
  immutable-generation guard rejecting modified, uncommitted Functions source.
  After commit, the same real-product generation qualification passed in 67.04
  seconds against the clean candidate.
- The focused runtime contract, generation, Worker protocol/supervisor, reboot,
  minimal-Core and runtime-handoff gate passed 218 tests in 68.75 seconds.
- The previous non-production acceptance remains the baseline: one-Agent
  reboot 6.6 seconds, 24-Agent max reboot 39.1 seconds, cold restart 106.962
  seconds, 24 ACTIVE Workers, and normal Agent work. This addendum did not
  restart or modify a production instance.

## Verdict and residual risk

The narrow change is approved for the exact digest above and independent PR
review. Resuming an old process whose quiesce request failed before delivery is
idempotent under the existing peer protocol and is safer than leaving a
possibly paused live process published as active. A peer that is no longer
alive is still not resumed. Existing rollback failure reporting remains the
authoritative indication when the resume RPC itself fails.
