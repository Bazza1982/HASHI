# HASHI1 unified release preflight — 2026-09-08

Status: source and offline preflight complete; live adoption is blocked pending
explicit HASHI1 Core-migration authorization and operator coordination

Owners: PAO / Functions, with HASHI1 platform and instance configuration
preservation

## Scope and immutable facts

This checkpoint was prepared in the isolated
`release-h1-preflight-20260908` branch based on `bef485fe`. HASHI1's root
checkout remained on `main@37b59dc6073dc86401d19fd54cced462d87fe8b2` and no
source, configuration, process, route or Agent was replaced. No reboot,
restart, model request, target staging or Agent migration was performed.

The shared candidate is a strict descendant of HASHI1 main. Before this
checkpoint's preflight-only files, its range contains 20 commits and changes
126 tracked files (8,719 insertions and 2,653 deletions). The range combines
Minimal Core/API 3, reboot receipts, Remote `/move`, instance model opt-ins,
Windows adapters, visible delivery/HChat, OCR, startup/Observer/Worker logging,
Superloop receipts and the negotiated Worker-log capability.

HASHI1 had no tracked or staged changes. Its four existing untracked paths have
no matching path in the candidate tree, so a checkout does not overwrite them.
The ignored `agents.json`, `secrets.json`, `tasks.json`, `instances.json`,
`agent_capabilities.json`, `state/`, workspaces, ports and local Remote state
are also absent from the candidate tree. They remain instance-owned and must be
backed up and hash-checked around adoption.

## Core and ABI decision

This is not an Agent-only Function update. The exact HASHI1 runtime and the
candidate agree on:

- CPython 3.12.13, `cpython-312-x86_64-linux-gnu`, 64-bit Linux;
- the standard dependency digest
  `sha256:7fbad45ee0b496c4f5279c54b91b99b3428198dce42c6f5439aacdbc87f37900`;
- Worker model `per-agent-process`, Worker protocol 1 and generation schema 2.

They differ on Core API 2 to 3, Function API 2 to 3, runtime-policy digest and
protected-Core source digest. The candidate's current Core manifest has seven
changed files, while the branch guard correctly unions the old and new
manifests and blocks 27 protected paths across the migration range. Therefore:

1. the running API-2 Core cannot adopt this candidate with `/reboot`;
2. a planned cold migration is required;
3. the current task did not authorize that HASHI1 Core adoption, so
   `check_protected_core_changes.py --base 37b59dc6` remains blocked by design;
4. the current checkpoint itself adds no protected-Core edit.

## Real Codex READY evidence

The first real probe used HASHI1's CPython 3.12.13 environment and a secure
isolated copy of Zelda's active `codex-cli` configuration. It safely rejected
the Worker before READY because the generic probe did not publish the already
running Backend API endpoint needed to construct the Codex Tool Gateway.

The probe now accepts `--service-endpoints`. It validates exact instance
ownership before republishing the snapshot only into the isolated probe home.
With HASHI1's live endpoint snapshot and independently observed Backend API
HTTP health, the same Function generation reached READY:

```text
Function Worker READY: agent=zelda pid=420871
generation=sha256:b19f60a3b0b33098fab20d541370a882c9459889d52a8c8e3a80142163dcb23f
phase=READY
```

The Worker log records `codex-cli 0.153.4`, Fixed session mode, and clean
shutdown. The candidate never became ACTIVE, never received a route or
Telegram token, and did not call a model. The pre-fix failure log is
`tmp/h1-release-preflight-instance/logs/function-workers/worker-419776.log`;
the passing log is `worker-420871.log` in the same directory.

## `/move` equivalence and live preflight

The independent HASHI1 `/move` commit `d09b5a49` was compared with the shared
candidate rather than transplanted again. `runtime_remote.py`, both owning test
modules, the three `/move` methods in `FlexibleAgentRuntime`, and all 66 move
catalog values in each English and Chinese locale agree exactly. The candidate
therefore retains the reviewed shared implementation and stronger checkpoint
tests without an instance-specific fork.

Candidate code queried HASHI1's real trusted `/peers` endpoint. At observation
time it discovered four peers: HASHI3 was online at its resolved Windows route
and advertised `agent_move_receive_v1`; HASHI2, INTEL and HASHI-PORTABLE were
stale or offline. The no-staging preview used a secure isolated snapshot of
Mimi's HASHI1 configuration/workspace and the real HASHI3 receiver:

- authenticated source `HASHI1` and target `HASHI3` with response proof;
- schema 1 accepted, AES-256-GCM transport advertised;
- 762,314-byte disposable package, 47 workspace files, eight exclusions;
- only `GET /peers`, `GET /health` and
  `GET /agent-move/v1/capabilities`; no request carried a payload;
- 64 watched HASHI1 source records and four HASHI3 config/migration records had
  identical before/after digests;
- HASHI3 `state/agent_moves` remained absent.

A first direct preview against the live Mimi workspace also completed, but its
file-level audit found the mtime of `habits.sqlite-shm` advanced while size and
content hash stayed identical. SQLite's read-only online backup can touch that
volatile shared-memory metadata. It did not alter durable content,
configuration or target state, but it means a strict claim that *all* live
source filesystem metadata remains untouched would be false. The release
acceptance therefore uses the isolated snapshot for the no-write claim; normal
live `/move --dry-run` retains this documented SQLite metadata limitation.

## Offline qualification

The release suite first failed with 3,815 passing and 21 failing tests. One
failure was the existing FYI size gate, 19 assertions still expected the
retired hidden-delivery behavior, and one HER parallel-wave test treated a
one-second deadlock guard as a latency requirement. The checkpoint compacts the
FYI, aligns those assertions with the accepted visible-delivery decision, and
widens only the deadlock guard while preserving the ordering assertions.

Final results in the qualified HASHI2 test environment are:

- Codex preflight option tests: 2 passed;
- `/move`, discovery, package and language tests: 72 passed, one third-party
  deprecation warning;
- runtime ABI, generation, IPC, lifecycle and rollback tests: 131 passed;
- corrected FYI, delivery and HER wave tests: 154 passed;
- Core gate: 606 passed;
- standard offline release suite: 3,836 passed, 161 deselected, one third-party
  deprecation warning in 454.14 seconds;
- Ruff, Python compilation, locale parsing and `git diff --check`: passed;
- default, staged and checkpoint-only Core guards: passed.

A deliberately stricter repeat put pytest's temporary workspace underneath the
checkout's ignored `tmp/` directory. It found a pre-existing HER v2
`workspace_inspect` limitation: Git-backed snapshot mode does not see a
same-size content change inside a Git-ignored nested workspace. That run was
3,835 passed and one failed; the same test passes in the policy-standard
external temporary directory. This checkpoint does not alter the HER v2 tool
outside its PAO release scope. The limitation needs separate owner triage and
must not be represented as fixed by this release.

## Coordinated cold adoption

These are operator steps, not actions completed by this checkpoint. Run them
from the HASHI1 root only after explicit HASHI1 Core-migration approval.

1. Fetch and pin the reviewed candidate without moving `main`:

   ```bash
   git fetch ../hashi2 \
     refs/heads/release-h1-preflight-20260908:refs/remotes/hashi2/release-h1-preflight-20260908
   hashi1_release_commit=$(git rev-parse refs/remotes/hashi2/release-h1-preflight-20260908)
   hashi1_rollback_head=$(git rev-parse main)
   test "$hashi1_rollback_head" = 37b59dc6073dc86401d19fd54cced462d87fe8b2
   git merge-base --is-ancestor "$hashi1_rollback_head" "$hashi1_release_commit"
   git branch backup/hashi1-pre-release-20260908 "$hashi1_rollback_head"
   git diff --quiet && git diff --cached --quiet
   ```

2. Create a mode-preserving private backup and checksum the files that the
   instance owns. Include any additional operator-installed configuration in
   the same directory:

   ```bash
   hashi1_release_backup=".rollback/hashi1-release-$(date +%Y%m%dT%H%M%S)"
   install -d -m 700 "$hashi1_release_backup"
   for hashi1_item in agents.json secrets.json tasks.json instances.json \
     agent_capabilities.json .bridge_u_last_agents.txt \
     runtime_port_assignments.json remote/config.yaml \
     state/service_endpoints.json state/instance; do
     if test -e "$hashi1_item"; then
       cp -a --parents "$hashi1_item" "$hashi1_release_backup"
     fi
   done
   for hashi1_file in agents.json secrets.json tasks.json instances.json \
     agent_capabilities.json .bridge_u_last_agents.txt \
     runtime_port_assignments.json remote/config.yaml; do
     if test -f "$hashi1_file"; then sha256sum "$hashi1_file"; fi
   done >"$hashi1_release_backup/live-config.sha256"
   chmod 600 "$hashi1_release_backup/live-config.sha256"
   ```

3. After the explicit Core-migration approval is recorded by the release
   coordinator, gracefully stop only HASHI1's PID from
   `state/instance/process.pid`. Do not escalate to SIGKILL automatically;
   abort adoption if it does not exit cleanly.

   ```bash
   hashi1_core_pid=$(tr -d '\r\n' <state/instance/process.pid)
   kill -TERM "$hashi1_core_pid"
   for hashi1_wait in $(seq 1 60); do
     if ! kill -0 "$hashi1_core_pid" 2>/dev/null; then break; fi
     sleep 1
   done
   ! kill -0 "$hashi1_core_pid" 2>/dev/null
   ```

4. Check out the candidate detached, preserving `main` as the rollback pointer;
   revalidate local configuration, runtime and source before launch:

   ```bash
   git switch --detach "$hashi1_release_commit"
   sha256sum -c "$hashi1_release_backup/live-config.sha256"
   python scripts/check_protected_core_changes.py \
     --base "$hashi1_rollback_head" --authorized
   .venv-wsl/bin/python scripts/check_runtime_contract.py --code-root .
   python scripts/check_protected_core_changes.py
   source .venv-wsl/bin/activate
   bin/bridge-u.sh --resume-last --api-gateway
   ```

5. From a second terminal require Backend API `ready=true`, `degraded=false`,
   runtime `core_api=3`, `function_api=3`, Python 3.12.13, a distinct shared
   Functions PID, and all 20 Workers `ACTIVE`, alive, accepting and Telegram
   connected. Require one common new generation, unchanged config checksums,
   Remote health, and no pre-READY errors. Then run `/move list`; if HASHI3 is
   still online, run only `/move mimi hashi3 --dry-run` and confirm that no
   target move record appears. Do not confirm, copy or migrate an Agent.

6. Only after those observations may the release coordinator fast-forward the
   source pointer without changing the already checked-out bytes:

   ```bash
   git branch -f main "$hashi1_release_commit"
   git switch main
   ```

## Rollback

Before step 6, rollback preserves both commits and needs no branch rewrite:

1. gracefully terminate only the new HASHI1 Core and require it to exit;
2. run `git switch main` (which still points at `37b59dc6`);
3. run `sha256sum -c "$hashi1_release_backup/live-config.sha256"`;
4. if a local configuration checksum changed, inspect and restore only that
   exact file from the private backup; never replace the whole instance root;
5. activate `.venv-wsl` and run
   `bin/bridge-u.sh --resume-last --api-gateway`;
6. require the old API-2 health, 20 Agents and generation
   `sha256:f57fa7eccfac40e03b461555c126379460e41cbd5f1eb2aa90ba181453c430dc`.

After step 6, use `backup/hashi1-pre-release-20260908` as the old source
pointer, check it out detached after stopping the candidate, and repeat the
same config-hash and launch checks. Leave `main` at the reviewed release until
the coordinator explicitly decides whether to move it back; do not use a broad
clean/reset operation as a recovery shortcut.

## Remaining acceptance and blockers

- Explicit HASHI1 Core-migration authorization is absent; this is the adoption
  blocker, not a source/test blocker.
- While this requested `bef485fe`-based checkpoint was being qualified,
  HASHI2 `main` advanced independently by one commit to `4b5497cb` for
  Superloop controller follow-through. That late commit is neither included nor
  qualified here and also changes `AGENT_FYI.md`. If “unified release” is to
  mean the latest shared `main` rather than the explicitly pinned candidate,
  the coordinator must decide to include it, reconcile the FYI size gate, and
  repeat the affected and release suites.
- HASHI2 was stale in HASHI1's peer view. A HASHI1 to HASHI2 dry-run is blocked
  until Remote discovery reports it online again; HASHI3 was usable.
- HASHI1's production environment has no pytest. Real candidate READY used that
  exact runtime; test gates use the qualified HASHI2 test environment.
- A Git-ignored nested workzone can evade HER v2 `workspace_inspect` snapshot
  drift detection as described above; this is an existing cross-owner risk,
  not a regression introduced by the HASHI1 candidate range.
- No all-Agent cold start, shared-service handoff, Telegram delivery, live
  HChat/Superloop behavior, real OCR inference, Windows launcher, external
  model/provider request, terminal `/move` card, actual target stage, Agent
  transfer or rollback was exercised.
- The isolated no-write preview does not prove a future live workspace remains
  quiescent, and live SQLite preview may advance `*-shm` metadata as recorded
  above.


## Controller integration checkpoint — 2026-09-08 13:09 AEST

The controller independently reviewed `97a35a7b` and combined it with
`4b5497cb` in `review/release-h1-unified-20260908`. This checkpoint preserves
both parents and the Superloop follow-through FYI. It is not production
adoption or final release qualification. HASHI2 main remains unchanged until
the shared candidate is qualified with the outstanding HASHI3 preflight.

Independent validation on the combined tree: 606 Core-gate tests passed.
The seven affected/consumer modules produced 293 passes, one explicitly
opt-in 601-second wall-clock canary skipped, and one one-second startup timeout
in the existing high-volume cancellation test. That test, the changed ordered
waves test and the endpoint validation tests were then run in a separate
explicit temporary root: all four passed. This does not erase the first timeout;
the final shared release suite must still cover the cancellation boundary.
The initial focused and gate runs overlapped; future suites must be sequential
and use explicit separate temporary roots. No assertion was weakened by the
controller and no timeout change was added to this checkpoint.

The Core protection check against `4b5497cb` passes; against HASHI1
`37b59dc6` it blocks the migration as expected. No authorization bypass was used.
The ignored nested workspace drift limitation was independently reproduced
with the existing inspection test. It is not limited to same-size edits:
`alpha\n` to `beta\n` also yields an unchanged Git-backed snapshot digest.
HER v2 owns that remaining Functions defect; do not claim this checkpoint fixes it.

The final platform candidate must incorporate HASHI3 evidence before its
release suite; HASHI3's current offline suite is active on the shared WSL host.
Avoid concurrent release-suite load. Runtime adoption additionally requires
explicit HASHI1 API-2-to-3 Core-migration approval and fresh all-Agent,
background, queue and upcoming-30-minute schedule checks immediately before
interruption. The commands above describe the older pinned candidate; replace
the candidate pin with the final independently qualified shared commit before
requesting migration approval or executing them.
