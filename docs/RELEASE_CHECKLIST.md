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
