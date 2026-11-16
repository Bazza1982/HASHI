# HASHI Core v6 startup-recovery technical review

Review time: 2026-11-16T02:46:06+11:00
Branch: `core5-fix`
Base: `main` at `1691832a`
Protected-Core digest: `sha256:758fa158eb150eb3ac8eb0b96df70f0334652b05f26a3297226b52faf3254289`

## Reviewer identity and approval boundary

This is an AI technical review performed by the same OpenAI Codex/Hashiko root
agent that implemented the candidate. It is not an independent-human review.
The repository's truthful `owner_approved` mode is used because the current
human owner explicitly requested the Core major-version migration in request
`req-hashiko-2026-11-15_221221-0001`. Two attempts to start a separate AI
reviewer failed before review began because its required HASHI tool server
timed out during handshake. This record does not claim those attempts produced
review evidence and does not authorize merge or production adoption.

## Scope reviewed

The exact protected scope was reviewed, with changed behavior concentrated in
`main.py`, `orchestrator/runtime_contract.py`,
`orchestrator/kernel_artifact.py`, `orchestrator/kernel_process.py`,
`orchestrator/runtime_process.py`, and
`orchestrator/function_worker_bootstrap.py`. The unchanged protected files were
included in the digest and checked for boundary compatibility.

The review also covered the owning non-Core lifecycle paths:
`orchestrator/function_worker_supervisor.py`,
`orchestrator/runtime_app_host.py`, `orchestrator/telegram_ingress.py`,
`orchestrator/workbench_auth.py`,
`remote/api/server.py`, `bin/bridge_ctl.ps1`,
`bin/hashi_restart_ctl.ps1`, and `bin/hashi_restart_task_runner.ps1`.

## Technical findings

No blocking source finding remains in the reviewed candidate.

- Deep runtime qualification remains the installation/adoption/audit boundary.
  It hashes every authoritative file and rejects missing or modified files,
  redirected paths, unlisted `.pth` files, startup hooks, DLLs, executables,
  modules, and other closure additions. The only extra-file exception is a
  PEP 3147 `__pycache__/*.pyc` whose source path is present in the sealed table.
- Fast admission validates the trusted manifest digest, selected role and
  runtime identity, interpreter/base bytes, venv binding, ABI/protocol/policy,
  process identity, private-channel nonce, and generation identity. It does not
  recursively hash the installation, rediscover packages, or rehash protected
  Core source for each child. A generated candidate exposed normal empty venv
  scaffold directories (`Include` and `Lib`) that have no sealed files. The
  reviewed correction permits a bounded file-free scaffold tree and permits
  declared import-root directories at their parent level only because each
  declared root is then shallow-scanned itself. A scaffold containing a file,
  redirect, or more than 128 directory entries is rejected; injected entries
  inside `Lib` or `site-packages` are rejected by those roots' own scans.
- `-B` prevents HASHI-managed Python processes from writing bytecode; it does
  not prevent existing bytecode from being read. The narrow cache exception
  therefore retains a documented residual local-writer risk rather than
  claiming all bytecode is safe.
- A generation receives one full byte qualification before newly adopted code
  executes. Direct/ad-hoc launches and externally supplied Kernel releases are
  fail-closed and perform that scan themselves. A generation produced by the
  Kernel qualifier, or materialized and verified by the Worker supervisor,
  carries only an in-process lifecycle fact into routine child launches; the
  child then validates canonical manifest identity and the private admission
  contract without another traversal. Same-Kernel recovery reuses only the
  exact committed release object. The reuse basis is the prior byte scan,
  atomic content-addressed materialization, no HASHI writer after publication,
  the live parent's in-memory fact, and its nonce-bound private channel. It
  does not prove Windows disk bytes are immutable; a later privileged/local
  writer remains a documented residual risk handled by explicit audits.
- Connector retries are per connector. Permanent Telegram credential errors
  stop that connector until configuration changes. Connector activation no
  longer gates Core/Worker readiness or triggers all-Agent topology work.
- When Remote is explicitly disabled by its authoritative lifecycle setting,
  Functions no longer queries Task Scheduler for a Remote runtime principal.
  A disabled connector therefore cannot add a 30-second credential-publication
  failure to Core/Functions startup; enabling Remote restores the exact
  registered-principal lookup and private-file protection boundary.
- Cold restart now validates the exact task and transaction before stopping the
  old process, distinguishes acceptance from a launcher PID, uses one deadline,
  and preserves precise failure stages. Malformed root-bound requests fail
  before path resolution.
- Replacement receipts preserve the original exception, stage, candidate PID,
  product-execution state, timings, cleanup, and rollback result.
- Receipt sanitization is implemented inside protected Core, so recording the
  underlying error does not lazily import product logging code. The focused
  regression verifies that credential-shaped text is redacted.
- All lifecycle roots and receipts remain bound to the requested HASHI4 code
  and instance roots. No cross-instance selector or broad process action was
  introduced.

## Evidence reviewed

- Static validation: `git diff --check`; 43 changed Python files compiled with
  bytecode disabled; all changed PowerShell scripts parsed; 3 changed shell
  scripts passed `bash -n`.
- Runtime/installation offline suite: 85 passed, 1 deliberately deselected
  because the development `.venv` contains newer/older packages than the
  repository lock.
- Kernel, generation, Worker and connector offline suite: 76 passed, with the
  remaining integration cases blocked before their assertions by that same
  development-environment dependency mismatch.
- Windows cold-restart and fail-fast suite: 44 passed.
- MCP ownership and readonly Python launch suite: 46 passed, 1 platform skip.
- Focused cache/deep-fast tests: 38 passed; replacement receipt and
  one-scan tests passed; launcher/base-child acceptance tests passed.
- Final platform-path correction: 57 focused runtime-installation,
  Remote-principal, and minimal-Core tests passed. The expanded runtime,
  restart, Worker, connector, and receipt suite reached 206 passed; its sole
  pre-commit rejection was the intentional guard against generating a product
  artifact from modified tracked Core source.
- Testing under the selected official v6 Functions interpreter exposed two
  real regressions in the first candidate: a direct generation launch and an
  externally supplied Kernel release could reach identity-only admission
  without the parent's byte-qualification fact. The reviewed correction keeps
  routine Kernel/24-Worker launch fast while restoring a default full scan for
  those untrusted entry paths. The two tamper regressions, the preserved-error
  rollback test, and all 18 focused process/adoption tests that do not require
  a clean committed checkout passed under that official interpreter.
- The first official v6 build installed the exact locked Functions packages.
  Role publication then correctly refused the dirty tracked checkout, proving
  that build/adoption still requires an immutable Git source. Actual runtime
  startup acceptance remains a separate required step after this branch commit.

## Residual risks and required next gates

- The fast path relies on the prior deep-qualified manifest and the absence of
  a later privileged local writer. It intentionally adds no ACL daemon or
  background re-verifier.
- Optional Kokoro/TTS preparation exceeded the managed command's five-minute
  ceiling. It is not part of Core/Functions readiness and remains separately
  incomplete until its sidecar installer finishes.
- This review approves the exact source digest for a branch commit and
  non-production HASHI4 build/acceptance only. A clean official runtime build,
  actual 24-Agent readiness, `/reboot min`, `/reboot max`, cold `/restart`,
  normal Agent work, rollback, scan counts, and measured timings must still
  pass. Production adoption and merge remain unapproved.

## Non-production acceptance completed

- HASHI4 cold `/restart` completed in 106.962 seconds. Core and shared
  Functions became usable and all 24 Workers reached ACTIVE; the fixed-task
  precheck took 4.067 seconds and runtime qualification took 0.798 seconds.
- The selected Core and Functions venvs use the shared CPython base while its
  204 additional files remain source-backed PEP 3147 `__pycache__/*.pyc`
  entries. Startup succeeded without deleting them.
- `/reboot min` completed in 6.6 seconds and replaced only the requested Agent
  Worker. `/reboot max` completed in 39.1 seconds, retained the Core process,
  and replaced shared Functions plus all 24 Workers; none of the old process
  IDs remained alive.
- A post-replacement arale request completed with the exact response
  `HASHI4_MAX_FINAL_OK`. A further 45-second observation retained the same Core,
  Functions and 24 ACTIVE Worker processes. The only degraded health item was
  the isolated `temp` Telegram connector; normal Agent work remained usable.
- Known task-definition failures returned in about 3.3 seconds with their
  precise stage and reason, while the existing Core remained online. Failed
  replacement candidates rolled back without losing the underlying error.
- This evidence is scoped to HASHI4 and pull request 166. Production adoption
  and merge remain unapproved.
