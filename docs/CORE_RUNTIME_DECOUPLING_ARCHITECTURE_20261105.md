# HASHI Core / Core Runtime / Functions Runtime Decoupling — Architecture Decision

Status: approved design direction; implementation and production adoption NOT yet performed.
Date: 2026-11-05. Functional owner: PAO release governance; engineering owners: protected Core bootstrap/runtime-contract and replaceable Functions lifecycle.
Parent starting point: main commit `6f78ac7f45b25111e68a737a5e6588152341b6a2`. Use PR #137 as verified dependency **input**, not as a mergeable implementation of this architecture.

## 1. Decision and user intent

The user's product is not widely distributed: do not preserve unrelated legacy installation paths or outdated third-party compatibility for their own sake. Preserve **currently working functionality and durable state** without exception. This is a one-off, explicitly authorized *design direction* toward a Core-major architecture change; writing these docs is NOT approval to deploy, edit a running interpreter, close open tasks, merge to main, or publish Core 5.

**Principle:** a vulnerability in aiohttp/Pillow/certifi/multidict/yarl must not automatically cause a new HASHI Core code generation or a Core host restart. Core source, Core host runtime, and Functions runtime must have separate identities, release lifecycles and validation rules. "Separate" is NOT "unmanaged". Core must still run on an approved Python interpreter/OS, and a CPython/stdlib security update might require a **Core runtime** replacement and cold restart; it need not automatically increase the product **Core code** major version when the protected implementation and protocol remain unchanged.

Today's problem is precise: `runtime_contract.py` is standard-library-only but `main.py` calls `enforce_runtime_contract` against the entire `constraints/standard-py312.lock`; `runtime_policy_digest` incorporates that lock, `compare_runtime_fingerprints` requires the same interpreter/prefix/lock/dependencies; `kernel_process.ProcessClient.prepare`, `function_worker_supervisor.prepare_worker` use multiprocessing spawn from the current executable; `function_generation.run_candidate_probe` uses `sys.executable`. Function code is already separated by process, sealed artifact and JSON IPC, but dependency ownership is not.

## 2. Four independent identities

| Identity | Name / source of truth | What changes it | Result |
|---|---|---|---|
| **C — Core code** | protected `CORE_SOURCE_PATHS`, digest, Core API and process protocol | protected implementation/contract change | controlled Core major generation (one-off 5.x migration now) |
| **CR — Core host runtime** | immutable CPython executable/build, platform ABI, OS and minimal Core runtime manifest | interpreter/stdlib/runtime-security maintenance | new Core runtime ID, cold host handoff/rollback; **not automatically C major** |
| **FR — Functions runtime** | immutable dedicated Python env, exact dependency versions/artifact hashes, ABI and runtime manifest | aiohttp/Pillow and other Function-package upgrades | new Function runtime ID, canary and verified function cutover; **no Core major** |
| **FG — Function code generation** | existing manifest digest and source revision | PAO/HERV3/Frontend Function code change | existing sealed Function candidate/handoff mechanism, subject to CR↔FR compatibility |

Optional execution profiles/sidecars (OCR, transcription, TTS, browser, GPU, privileged L2 sealed Windows capsule) remain independent and must not be silently incorporated into CR. A Function dependency need not be moved into its own sidecar until imports and behaviour prove that division safe.

**Do not conflate product version, Core code generation, Core runtime ID, Function runtime ID, Function source generation, and existing wire-protocol versions.** The first implementation may use CPython 3.12.13 as the *base* for CR and FR to reduce migration risk, but their virtual environments, site-packages and identities must be separate; later compatible Python patches can be qualified independently by role.

## 3. Process and dependency boundaries

~~~text
                    HASHI code / product release identity
                                |
               +----------------+----------------+
               |                                 |
        CORE host process                   FUNCTIONS plane
        pinned CR runtime                   pinned FR runtime
        standard library only               shared Functions process
        instance lock                       Agent Worker processes
        lifecycle/handoff                   PAO / HERV3 / FC / Telegram
        immutable-artifact verifier         Function sealed code generation
        JSON IPC / process manager          HTTP and regular app dependencies
               |                                 |
               +==== authenticated JSON IPC =====+
                                                 |
                                     isolated optional sidecars
                                     OCR / STT / TTS / browser / L2
~~~

Required hard invariants:

1. Core has no direct imports of `aiohttp`, `PIL`, `multidict`, `yarl`, `certifi`, application managers, provider SDKs, or Function business modules. Confirm by static import graph AND a clean isolated-Core process test. Core runtime build must not install `hashi-bridge` with its dependency-bearing default extras.
2. An FR update never overwrites CR's Python, site-packages, stdlib or running process; an old FR environment remains available until all owned processes are retired and the new generation is proven. No `pip install -U`/project-aware `uv sync` in an active environment.
3. Core may launch only a **trusted, immutable, explicitly qualified FR executable** selected by the installation/runtime authority, never by an Agent/LLM/Prompt/Workzone. Core verifies exact executable, manifest, checksum and executable provenance before launch; child independently reports observed version, interpreter and dependencies **before READY**, and owner confirms them.
4. Core/Shared/Agent communications remain the existing `function_worker_protocol.py` JSON envelope with strict schema, size limits, timeouts and request/response identity. No pickle across the boundary; do not invent a second protocol. If an added launch handshake is needed, it is an implementation of the existing private transport and authenticated parent/child identity, not an alternate unaudited permission channel.
5. Core↔FR verification checks **the boundary that matters**: protocol/API/manifest schema compatibility, expected source-generation digest, requested execution identity, installed runtime qualification, platform support and explicit capabilities. **Do not require matching executable, environment prefix or third-party dependency digest across CR and FR**. Within each role, exact runtime fingerprint checks remain strict.
6. Core must not implicitly import business modules via startup, release qualification or lifecycle callbacks. All optional drivers, codecs, database clients and native libraries run in qualified FR/sidecars.
7. Per-Agent Worker can share the single qualified FR with Shared Functions initially; no per-Agent venv explosion. Existing Windows L1/L2 Safety/Runner/LPAC service guarantees, ownership, consent, audit, WAC gates and sealed capsules are not weakened by this migration.
8. A Function runtime change may happen under a running Core only **after** the cross-interpreter process launcher, identity checks, promotion and rollback are proven. A Core host-runtime patch or Core bootstrap code change still requires its own controlled process cutover.

## 4. Source and packaging ownership

Proposed policy files (names may adjust once mapped against build/Portable consumers, but owners may not duplicate):

- `constraints/core-host-py312.lock`: minimal Core host dependencies, ideally **zero** third-party requirements. An empty approved dependency list is valid; the gate must not require a nonempty list for this role.
- `constraints/functions-py312.lock`: all required Shared/Agent Function packages, exact pinned versions. Candidate #137 verified five versions belong here *if import/usage inventory proves the owner*.
- `constraints/sidecar-<profile>-py312.lock`: retain existing specialised runtime locks; isolate additional packages only after real consumer analysis.
- `[tool.hashi.runtime]` / `runtime-entry.json` may be migrated once to a role-aware runtime manifest/installer-controlled profile. Avoid two competing declarative registries: prefer a single release manifest whose role lock digests and executable provenance are read by the existing trusted contract.
- Preserve `pyproject.toml` package metadata as distribution metadata, **not the authoritative Core host dependency list**. Update launch/build scripts to materialize CR without pulling application dependencies, and FR from its explicit own lock and wheel hashes.

Record separate fingerprints in runtime topology and `/version`: Core code, CR, FR, FG, optional sidecar profiles; show each observed/adopted state and distinguish candidate vs running. A GitHub commit, clean wheel audit, or new version string is never adoption proof.

## 5. Versioning and governance

Update `docs/HASHI_VERSIONING_POLICY.md` during implementation: `X` identifies protected Core implementation/contract generation, not vendor dependency changes; `CR` updates carry their own immutable runtime revision and may require a cold restart; `FR` updates are function-runtime maintenance even if only dependencies changed; product `Y/Z` classification remains tied to shipped functionality/maintenance. The protected Core change in the decoupling implementation **does** qualify for a separately authorized major migration and independent review under `docs/core-reviews`. The `core-change-approved` GitHub label and policy gate cannot be bypassed.

PR #137 contains valuable five-version pin selection, wheel-hash lock, red/green tests, independent review and broader baseline test evidence. **Do not merge #137 unchanged into current main**: its standard lock still changes the Core runtime identity. Reuse its tested package versions, security probes and metadata work selectively in FR and Portable's derived lock *after* the new owner and build pipeline are established. Retire/supersede #137 with an explicit PR decision to prevent parallel source-version changes. Whether final first release is called Core 5.0.0a1 is governed by the single finalized decoupling release plan; do not publish two competing 5.0.0a1 candidates.

## 6. Explicit non-goals and protected behaviours

- No new remote compute execution authority, no relaxing L2 Workzone ACLs/permissions, no change to L1 decision semantics.
- No replacement of PCM, HERV3, Frontend Connector, Workbench or Telegram business logic.
- No all-at-once migration to Python 3.13 or mandatory per-Agent env.
- No attempt to support arbitrary old distributed HASHI releases. **Known-good runtime rollback** and durable task/session/permission state preservation are still mandatory and are NOT legacy support.
- No live migration in this architecture-only branch or without separate user approval.
- Do not silently weaken a failure, enlarge watchdogs, skip regressions or bypass fingerprints to make an incompatible build pass.

Implementation phases, source map and acceptance criteria are recorded in the companion docs.
