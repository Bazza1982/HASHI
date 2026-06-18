# Release Checklist

## HASHI Bridge

- Static compile: `python3 -m py_compile main.py orchestrator/*.py`
- Full test suite: `pytest`
- Workbench health: `curl http://127.0.0.1:<workbench_port>/api/health`
- API Gateway health when enabled: `curl http://127.0.0.1:<api_gateway_port>/health`
- Live reboot smoke:
  - `/reboot min`
  - `/reboot max`
  - verify agents return to `ONLINE`
  - verify scheduler is recreated and started
  - scan bridge logs for post-reboot `ERROR`, `CRITICAL`, `Traceback`, `failed`, and `LOCAL MODE`
- Slim core docs:
  - `docs/HASHI_SLIM_CORE_ARCHITECTURE.md` reflects current manager boundaries
  - `docs/HASHI_CORE_SLIMMING_PLAN.md` reflects latest implementation and validation status
  - `CHANGELOG.md` records structural changes and residual notes
- Claw mode gates:
  - `python -m pytest tests/test_claw_cli_adapter.py -q`
  - `python -m py_compile adapters/claw_cli.py tests/test_claw_cli_adapter.py`
  - `python scripts/claw_code_probe.py --check version` returns a clear success or expected `ClawBinaryNotFound` diagnostic while packaged binaries are absent
  - At least one live `claw-cli` agent workzone smoke validates repo-root read/write/edit before release notes claim agentic file work support
- Superloop alpha gates:
  - `python -m pytest tests/test_superloop_store.py tests/test_superloop_taskboard.py tests/test_superloop_waits.py tests/test_superloop_runner.py tests/test_superloop_scheduler.py tests/test_superloop_compiler.py tests/test_superloop_issues.py tests/test_superloop_commands.py tests/test_superloop_recording.py tests/test_superloop_nagare_adapter.py -q`
  - Taskboards use `task_id`, not `id`, and every in-progress or next-action task resolves to a real task
  - `waits.json` entries include `wait_id`, `kind`, `status`, `entered_at`, deadline/follow-up fields, and a `resume_policy`
  - HChat/protocol replies are classified into loop evidence before task advancement or closeout
  - Closeout includes an inbox-drain barrier and records stale/contradictory/late replies
  - At least one template dry-run or live controller loop records taskboard, waits, issues, evidence, and final closeout state before claiming superloop functionality

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
