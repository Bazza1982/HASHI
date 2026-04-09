# Nagare Release Checklist

Phase 8 release readiness means the extracted engine, editor, and docs can be installed, built, and sanity-checked without relying on unstated tribal knowledge.

## Python package

- `pip install .` succeeds from the repo root
- `python -m nagare.cli --help` exits successfully
- `python -c "import nagare"` exits successfully
- contract tests pass:
  - `tests/contract/test_nagare_core_contract.py`
  - `tests/contract/test_logging_contract.py`
  - `tests/contract/test_round_trip_contract.py`
  - `tests/contract/test_nagare_api_contract.py`
  - `tests/contract/test_hashi_adapter_contract.py`

## Editor

- `npm install` succeeds in [`nagare-viz/`](/home/lily/projects/hashi/nagare-viz)
- `npm test` succeeds
- `npm run build` succeeds

## Docs

- migration boundary documented in [`docs/MIGRATION_FROM_HASHI.md`](/home/lily/projects/hashi/docs/MIGRATION_FROM_HASHI.md)
- handler contract documented in [`docs/HANDLER_GUIDE.md`](/home/lily/projects/hashi/docs/HANDLER_GUIDE.md)
- adapter model documented in [`docs/ADAPTER_GUIDE.md`](/home/lily/projects/hashi/docs/ADAPTER_GUIDE.md)
- logging schema documented in [`docs/LOGGING.md`](/home/lily/projects/hashi/docs/LOGGING.md)
- YAML fidelity limits documented in [`docs/ROUND_TRIP_CONTRACT.md`](/home/lily/projects/hashi/docs/ROUND_TRIP_CONTRACT.md) and [`docs/NAGARE_KNOWN_LIMITATIONS.md`](/home/lily/projects/hashi/docs/NAGARE_KNOWN_LIMITATIONS.md)

## Manual smoke

- `nagare run tests/fixtures/smoke_test.yaml --silent --yes`
- `nagare list`
- `nagare status <run_id>`
- `nagare api --host 127.0.0.1 --port 8787`

## Release note minimums

- summarize user-visible changes by phase
- list known fidelity and runtime limits
- call out that the API is read-only and polling-based
- call out that raw YAML remains required for unsupported workflows
