# Nagare Release Notes v0.1.0

## Highlights

- extracted `nagare` core package with runner, state, artifact, YAML, logging, and API surfaces
- HASHI compatibility wrapper and adapter layer preserved through `flow.adapters.hashi`
- deterministic CLI smoke mode added for package-install and CI verification
- `nagare-viz` build wired into release-readiness checks

## Verification Baseline

- `pytest -q tests/contract`
- `python -m nagare.cli run tests/fixtures/smoke_test.yaml --yes --silent --smoke-handler`
- `cd nagare-viz && npm run build`

## Known Limitations

See `docs/KNOWN_LIMITATIONS_NAGARE.md`.
