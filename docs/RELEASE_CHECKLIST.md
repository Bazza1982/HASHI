# Release Checklist

## HASHI Bridge

- Testing scope follows `docs/TESTING_POLICY.md`; focused, core, offline
  product, contract, platform, and live results are reported separately.
- Static compile: `python3 -m py_compile main.py orchestrator/*.py`
- Core gate: `python -m pytest -q`
- Offline product suite for the release candidate: `python -m pytest -q tests -m "not contract and not live and not platform"`
- Relevant contract and platform scopes are run and reported separately; live
  scope requires explicit authorization
- Architecture boundaries:
  - `python scripts/check_protected_core_changes.py --validate-manifest`
  - `python scripts/check_protected_core_changes.py --base main` (or
    `--authorized` only when the current task explicitly approves core edits)
  - architecture-boundary CI is green
  - `tests/test_architecture_boundaries.py` keeps private paths and old global
    process files from regressing
  - no new model, command, manager, workspace-state, instance-lock, or
    platform fact source duplicates an existing owner
- Workbench health: `curl http://127.0.0.1:<workbench_port>/api/health`
- API Gateway health when enabled: `curl http://127.0.0.1:<api_gateway_port>/health`
- Live reboot smoke:
  - `/reboot min`
  - `/reboot max`
  - while at least one other Agent remains online, adopt a disposable public
    class/member change through `/reboot min`; verify only the requester stops
    and returns, with no implicit `max` and no targeted-interface rejection
  - verify malformed `min`/number requests are rejected before preflight and
    never fall back to all running Agents
  - no function change or failed reload directs the operator to a cold process restart
  - verify agents return to `ONLINE`
  - verify Workbench API, enabled API Gateway, scheduler, delivery watcher, and
    background jobs are recreated and healthy
  - introduce a syntax error in a disposable fixture and verify preflight
    rejects `/reboot` without stopping live agents
  - scan bridge logs for post-reboot `ERROR`, `CRITICAL`, `Traceback`, `failed`, and `LOCAL MODE`
- Slim core docs:
  - `docs/HASHI_SLIM_CORE_ARCHITECTURE.md` reflects current manager boundaries
  - `docs/HASHI_CORE_SLIMMING_PLAN.md` reflects latest implementation and validation status
  - `CHANGELOG.md` records structural changes and residual notes
- HER v2 mode gates:
  - release scope names HASHI `v4.0.0-alpha.2` separately from Enterprise AAI
    `v0.1.0-alpha.1` / package `0.1.0a1`
  - HER v2 changes run the touched v2 module plus its direct adapter/runtime
    consumers; no active path imports the retired HER v1 implementation
  - `her` resolves forward to `her-v2`, while `claw-cli` is rejected
  - the release candidate passes the explicit offline product suite once;
    repeated overlapping HER bundles are not separate gates
  - `python -m py_compile adapters/her_v2.py adapters/her_v2_provider.py orchestrator/her_v2/prompt_catalog.py orchestrator/her_v2/prompts.py orchestrator/her_v2/runtime.py orchestrator/her_v2/runtime_invocation.py orchestrator/her_v2/runtime_support.py adapters/her_habits.py orchestrator/runtime_her_habits.py tools/media_read.py tools/gateway/mcp_stdio.py`
  - Fixed mode proves incremental resume for session-based CLI backends; Flex,
    Wrapper, Audit, and Dual Brain preserve their full-context contracts
  - HER v2 remains the active Habit/Meditation owner and request-scoped
    eligibility prevents internal or ephemeral work from entering `/habit`
  - HER UI and status show Fast path, Planned, Adaptive, Reviewed, and Assured
    while persisted/API values remain `low`, `medium`, `high`, `xhigh`, and
    `max`; `/effort reviewed` and `/effort assured` normalize correctly
  - Reviewed and Assured regressions prove read-only tool delegation, exact
    current-invocation receipts, stable before/after snapshots, one Reviewed
    closure check, and the three-attempt Assured Verification ceiling
  - `verification_run` accepts only registered recipes, excludes workspace and
    environment credentials, uses a temporary `HOME`, disables network, cleans
    the temporary copy, and refuses host fallback when isolation is unavailable
  - Review/Verification unavailable, partly verified, and not-AI-verifiable
    results are reported honestly without replacing Execution disposition
  - `/rebuild` is a side-effect-free one-version retirement notice and no
    native HER manager/source/package is initialized at startup
  - At least one live `her-v2` canary after `/reboot min` validates
    provider/model selection, Flex Fixed-mode continuation for session CLI
    backends, repo-root read/write/edit, `media_read` for image/PDF/audio,
    canonical and legacy screenshot image results,
    `/habit` no-change/change/failure recovery, and Verbose notification
    behavior before release notes claim those capabilities
  - `/reboot max` and wider rollout happen only after the canary is green and
    logs contain no unexplained HER, Gateway, media, Habit, or reload errors
- Superloop alpha gates:
  - `python -m pytest tests/test_superloop_store.py tests/test_superloop_taskboard.py tests/test_superloop_waits.py tests/test_superloop_runner.py tests/test_superloop_scheduler.py tests/test_superloop_compiler.py tests/test_superloop_issues.py tests/test_superloop_commands.py tests/test_superloop_recording.py tests/test_superloop_nagare_adapter.py -q`
  - Taskboards use `task_id`, not `id`, and every in-progress or next-action task resolves to a real task
  - `waits.json` entries include `wait_id`, `kind`, `status`, `entered_at`, deadline/follow-up fields, and a `resume_policy`
  - HChat/protocol replies are classified into loop evidence before task advancement or closeout
  - Closeout includes an inbox-drain barrier and records stale/contradictory/late replies
  - At least one template dry-run or live controller loop records taskboard, waits, issues, evidence, and final closeout state before claiming superloop functionality

## GitHub Publication

- Destination:
  - approved GitHub owner/repository URL is recorded
  - branch, visibility, and upstream tracking are intentional
  - a LAN/debug remote is not treated as a GitHub publication target
- License and IP boundary:
  - `LICENSE` and third-party notices agree with the files being published
  - retired HER v1 source, binaries, and Claw notices are absent from the
    application package and remain only in the external retirement archive
- Repository hygiene:
  - `git status --short` contains only intended changes
  - staged diff and commit range contain no credentials, workspace state, logs,
    private media/cache content, local operator notes, or unrelated user edits
  - generated binaries are included only when their provenance, platform,
    checksum, license, and release purpose are reviewed
  - optional EXP binary assets are absent from Git/source distributions; the
    independent pack checksum and safe restore test pass
  - scan the exact outbound range for private-key blocks, access-token formats,
    credentials in assignments/URLs, personal filesystem roots, live chat or
    account identifiers, private IP/host records, and tracked local runtime
    state; review every match rather than publishing on pattern count alone
  - `git ls-files` contains no live `.env`, `secrets.json`, private key,
    workspace transcript, bridge log, local candidate, rebuild state, or
    operator backup file
- Documentation:
  - `README.md`, `CHANGELOG.md`, `docs/README.md`, active contracts, known
    issues, and release notes agree on released versus unreleased status
  - internal Markdown links resolve and `git diff --check` passes
  - current validation evidence is distinguished from live rollout evidence
- Git operation:
  - create one coherent reviewed commit for the checkpoint
  - review `git show --stat --oneline HEAD` and the exact outbound commit range
  - push only after the destination and publication scope are approved; never
    overwrite remote history implicitly

## HASHI AAI Enterprise 0.1 Alpha

This gate is for `HASHI AAI Enterprise v0.1.0-alpha.1`. It confirms that the
enterprise control plane and deployment artifacts are coherent for alpha
testing. It does not certify a production enterprise-server rollout.

- Version metadata:
  - `pyproject.toml` uses `0.1.0a1`
  - `setup.py` uses `0.1.0a1`
- Scope docs:
  - `docs/HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md` includes the
    `HASHI AAI Enterprise 0.1 Alpha` cut line
  - `docs/HASHI_ENTERPRISE_AAI_READINESS_REVIEW.md` marks production validation
    as pending
  - `docs/RELEASE_NOTES_HASHI_AAI_ENTERPRISE_v0.1.0-alpha.1.md` records known
    alpha limits
- Static compile:
  - `python3 -m py_compile hashi.py setup.py`
- Connector and policy smoke:
  - `pytest -q tests/test_enterprise_connectors.py tests/test_workbench_enterprise_connectors.py tests/test_enterprise_policy.py`
- Approval, audit, and export smoke:
  - `pytest -q tests/test_workbench_enterprise_policies.py tests/test_workbench_enterprise_audit.py tests/test_enterprise_audit_ledger.py tests/test_enterprise_audit_export.py tests/test_enterprise_audit_live_export.py`
- Deployment artifact smoke:
  - `helm lint deploy/helm/hashi-enterprise`
  - render the chart through `.github/workflows/enterprise-helm-render.yml`
  - generate and parse the deployment plans through their dedicated GitHub
    workflows
  - `pytest -q tests/contract/test_enterprise_plan_contract.py`
  - `pytest -q tests/test_enterprise_siem_assets.py`
- CLI smoke:
  - `python3 hashi.py --help`
  - `python3 hashi.py enterprise --help`
- Workbench build:
  - `cd workbench && npm run build`
- Final hygiene:
  - `git diff --check`

## Nagare

### Package

- Build artifacts: `python -m build`
- Fresh install smoke: install the built wheel into a clean environment
- CLI smoke: `nagare run tests/fixtures/smoke_test.yaml --yes --silent --smoke-handler`
- Contract tests: `pytest -q tests/contract`

### Docs

- `docs/MIGRATION_FROM_HASHI.md` reflects the current adapter boundary
- `docs/HANDLER_GUIDE.md` matches the live protocol
- `docs/ADAPTER_GUIDE.md` matches the host integration path
- `docs/LOGGING.md` matches emitted event names and snapshot fields
- `docs/INSTALL.md` covers both Python package and `nagare-viz`

### Frontend

- `cd nagare-viz && npm ci`
- `cd nagare-viz && npm run build`

### Release Notes

- Update known limitations
- Record contract and smoke commands used for verification
- Note any fidelity gaps between HASHI host behavior and standalone `nagare`
