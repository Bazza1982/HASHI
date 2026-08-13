# Release Checklist

## HASHI Bridge

- Static compile: `python3 -m py_compile main.py orchestrator/*.py`
- Full test suite: `pytest`
- Architecture boundaries:
  - `python scripts/check_protected_core_changes.py --validate-manifest`
  - `python scripts/check_protected_core_changes.py --base main` (or
    `--authorized` only when the current task explicitly approves core edits)
  - architecture-boundary CI is green
  - `tests/test_architecture_boundaries.py` keeps private paths, old global
    process files, and active-runtime size from regressing
  - no new model, command, manager, workspace-state, instance-lock, or
    platform fact source duplicates an existing owner
- Workbench health: `curl http://127.0.0.1:<workbench_port>/api/health`
- API Gateway health when enabled: `curl http://127.0.0.1:<api_gateway_port>/health`
- Live reboot smoke:
  - `/reboot min`
  - `/reboot max`
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
- HER mode gates:
  - Manifest review confirms runtime version, source commit, platform target,
    executable path, SHA-256, upstream license, and certification baseline all
    describe the same artifact
  - `python scripts/her_runtime_probe.py --check version` resolves the packaged
    HER binary and returns a successful version diagnostic
  - `python scripts/verify_her_certification.py --source-root <pinned-her-source>`
    passes full Rust workspace tests plus workspace/all-target Clippy with
    warnings denied
  - `python -m pytest -q tests/test_her_adapter.py tests/test_her_certification_baseline.py tests/test_tool_gateway_mcp.py tests/test_media_read.py tests/test_runtime_media.py`
  - `python -m pytest -q tests/test_her_habit_meditation.py tests/test_runtime_her_habits.py tests/test_flexible_backend_state.py tests/test_runtime_pipeline.py`
  - `python -m pytest -q tests/test_her_debug_lab.py tests/test_her_debug_restart_guard.py tests/test_her_debug_superloop_template.py`
  - `python -m py_compile adapters/her.py adapters/her_habits.py orchestrator/runtime_her_habits.py tools/media_read.py tools/gateway/mcp_stdio.py`
  - Fixed mode proves incremental resume only after a HER session ID exists;
    Flex, Wrapper, Audit, and Dual Brain prove full-context turns do not also
    pass `--resume`
  - The HER adapter is the one active Habit/Meditation owner and request-scoped
    eligibility prevents internal or ephemeral work from entering `/habit`
  - At least one live `her` canary after `/reboot min` validates provider/model
    selection, fixed-mode continuation, repo-root read/write/edit, `media_read`
    for image/PDF/audio, canonical and legacy screenshot image results,
    `/habit` no-change/change/failure recovery, and Verbose notification
    behavior before release notes claim those capabilities
  - `/reboot max` and wider rollout happen only after the canary is green and
    logs contain no unexplained HER, Gateway, media, Habit, or reload errors
  - Certification is platform-specific. A Linux `.11` result must not be used
    to claim Windows `.11` parity
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
  - `LICENSE`, packaged `CLAW_LICENSE`, and third-party notices agree with the
    files being published
  - packaged HER retains `CLAW_LICENSE` and its reviewed provenance
- Repository hygiene:
  - `git status --short` contains only intended changes
  - staged diff and commit range contain no credentials, workspace state, logs,
    private media/cache content, local operator notes, or unrelated user edits
  - generated binaries are included only when their provenance, platform,
    checksum, license, and release purpose are reviewed
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
  - `pytest -q tests/test_enterprise_deploy_skeleton.py tests/test_enterprise_helm_chart.py tests/test_enterprise_production_validation_plan.py tests/test_enterprise_siem_assets.py`
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
