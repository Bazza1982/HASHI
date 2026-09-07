# Nagare Release Checklist

Release readiness means the engine, editor, workflows, and docs can be installed, built, and sanity-checked without relying on unstated tribal knowledge.

## Python package

- `pip install .` succeeds from the repo root
- `python -m nagare.cli --help` exits successfully
- `python -c "import nagare"` exits successfully
- the built wheel installs outside the source tree and completes the deterministic smoke workflow
- contract tests pass:
  - `tests/contract/test_nagare_core_contract.py`
  - `tests/contract/test_subprocess_handler_contract.py`
  - `tests/contract/test_logging_contract.py`
  - `tests/contract/test_round_trip_contract.py`
  - `tests/contract/test_nagare_api_contract.py`
  - `tests/contract/test_hashi_adapter_contract.py`
  - `tests/contract/test_release_readiness_contract.py`

## Editor

- `npm install` succeeds in [`nagare-viz/`](../nagare-viz/)
- `npm test` succeeds
- `npm run build` succeeds

## Docs

- migration boundary documented in [MIGRATION_FROM_HASHI.md](MIGRATION_FROM_HASHI.md)
- handler contract documented in [HANDLER_GUIDE.md](HANDLER_GUIDE.md)
- adapter model documented in [ADAPTER_GUIDE.md](ADAPTER_GUIDE.md)
- logging schema documented in [LOGGING.md](LOGGING.md)
- YAML fidelity limits documented in
  [ROUND_TRIP_CONTRACT.md](ROUND_TRIP_CONTRACT.md) and
  [KNOWN_LIMITATIONS_NAGARE.md](KNOWN_LIMITATIONS_NAGARE.md)

## Manual smoke

- `nagare run tests/fixtures/smoke_test.yaml --silent --yes --smoke-handler`
- `nagare list`
- `nagare status <run_id>`
- `nagare api --host 127.0.0.1 --port 8787`

## Security and workflow contracts

- the API refuses non-loopback bind addresses
- browser POST requests from untrusted origins are rejected before side effects
- the editor cannot read or write outside `flow/workflows/`, including through symlinks
- every published workflow has a valid DAG, resolvable `agent_md` files, and a supported backend
- required artifacts and automatic quality gates fail closed
- required pre-flight values fail before execution, including for direct Python callers
- duplicate YAML mapping keys are rejected instead of being silently overwritten
- subprocess-reported artifacts cannot escape their run-scoped worker workspace
- published workflows contain no retired fixed-timeout or fixed-retry controls
- source workflows and their runtime fixtures are byte-for-byte identical

## Release note minimums

- summarize user-visible changes by component
- list known fidelity and runtime limits
- call out that the API is a trusted-local control/inspection boundary, not an authenticated
  network service
- call out that delivered callable code executes in-process without a sandbox
- call out that raw YAML remains required for unsupported workflows
