# Nagare Release Checklist

## Package

- Build artifacts: `python -m build`
- Fresh install smoke: install the built wheel into a clean environment
- CLI smoke: `nagare run tests/fixtures/smoke_test.yaml --yes --silent --smoke-handler`
- Contract tests: `pytest -q tests/contract`

## Docs

- `docs/MIGRATION_FROM_HASHI.md` reflects the current adapter boundary
- `docs/HANDLER_GUIDE.md` matches the live protocol
- `docs/ADAPTER_GUIDE.md` matches the host integration path
- `docs/LOGGING.md` matches emitted event names and snapshot fields
- `docs/INSTALL.md` covers both Python package and `nagare-viz`

## Frontend

- `cd nagare-viz && npm ci`
- `cd nagare-viz && npm run build`

## Release Notes

- Update known limitations
- Record contract and smoke commands used for verification
- Note any fidelity gaps between HASHI host behavior and standalone `nagare`
