# HASHI Testing Policy

## Purpose

HASHI optimizes for confidence per minute, not test count or a green dashboard.
A test is useful only when a realistic defect in owned behavior can make it
fail and the failure identifies a meaningful boundary.

The pre-policy suite had 2,607 collected cases across 265 modules and 74,879
lines of test code. It mixed local, compatibility, deployment, platform, and
live-oriented checks in one flat suite. Bare pytest could also recurse into
Agent workspaces and duplicate repositories. Those are collection and policy
defects, not evidence of thoroughness.

## Retention rule

Keep a test when it protects at least one of these:

- observable product behavior;
- a public protocol or persisted-data contract;
- a security, authority, privacy, idempotency, or recovery invariant;
- a previously observed defect that is not already covered by a stronger
  scenario;
- a real package, renderer, parser, compiler, or service boundary.

Delete or replace a test when it primarily:

- tests a mock, fixture, test logger, or test runner rather than product code;
- copies source code, documentation prose, UI wording, YAML text, or workflow
  text into substring assertions;
- asserts an arbitrary implementation metric such as a source-file line count;
- repeats the same branch for every enum value even though the parameter cannot
  affect that branch;
- duplicates an existing scenario at a lower and less useful level;
- proves only that planned or documented behavior exists;
- cannot fail under a plausible defect in the behavior named by the test.

Mocks may arrange inputs or observe calls. They are never the subject under
test. Deployment checks should invoke the real parser or renderer, such as
`helm lint` and `helm template`, instead of mirroring file text.

## Failure proof

Every new or materially rewritten behavioral test needs a red/green reason:

1. identify the defect or invariant;
2. show that the test fails on the defective implementation, a safe temporary
   mutation, or the pre-fix commit;
3. show that it passes on the intended implementation.

The proof may be recorded in the commit or review notes. A test added after the
implementation without a credible red state is presumed redundant until shown
otherwise. Do not permanently keep a mutation; use it only as a reversible
local check.

## Execution layers

### 1. Static check

Use syntax, format, manifest, schema, or diff checks when they directly match
the edited asset. Documentation-only changes normally stop here.

### 2. Focused test

Run the owning test node or module for every behavioral change:

```bash
python -m pytest -q tests/test_component.py::test_changed_contract
python -m pytest -q tests/test_component.py
```

An explicit pytest path must collect only that path. A test, fixture, hook, or
wrapper must never start another pytest process or silently add unrelated
modules.

Do not launch independent pytest processes concurrently against pytest's
shared default temporary root. Use one process, or give each process an
explicit isolated `--basetemp`, so cleanup races cannot create false warnings.

### 3. Component test

Add direct consumers only when the changed contract crosses their boundary:

```bash
python -m pytest -q tests/test_component.py tests/test_direct_consumer.py
```

Do not add tests merely because their names share a broad word such as
`runtime`, `enterprise`, or `HER`.

### 4. Core gate

Bare pytest runs the curated, deterministic core gate declared by `testpaths`
in `pyproject.toml`:

```bash
python -m pytest -q
```

Run it for shared registries, configuration, hot reload, lifecycle, gateway,
or central runtime boundaries. The target is at most 30 seconds on the HASHI1
development machine. It is not a full-suite alias.

### 5. Offline product suite

The offline product suite is always explicit and excludes separately governed
contract, live, and platform checks:

```bash
python -m pytest -q tests -m "not contract and not live and not platform"
```

Run it only for:

- a release candidate;
- changes to `pyproject.toml`, `tests/conftest.py`, or shared test
  infrastructure;
- a broad refactor whose affected owners cannot be bounded reliably;
- an explicit user or release-gate request.

Passing this suite does not validate real credentials, external providers,
browser state, PostgreSQL, Windows-only behavior, or live adoption. Running
`python -m pytest -q tests` means "all pytest inventory" and is reserved for
auditing collection or shared test infrastructure; it is not a routine product
or release gate.

### 6. Contract, platform, and live checks

These are separately named scopes. Run them only when their product surface is
changed or a release gate calls for them. A live check requires explicit
authorization and must never be reached transitively from an offline test.

```bash
python -m pytest -q tests -m contract
python -m pytest -q tests -m platform
python -m pytest -q tests -m live
```

## Change-to-test selection

| Change | Minimum evidence | Escalate when |
|---|---|---|
| Documentation only | link/schema check and `git diff --check` | executable examples or release metadata changed |
| Leaf module | owning node or module | a public contract changed |
| Adapter/provider | adapter module plus registry/binding consumer | shared response or delivery contract changed |
| Shared runtime/registry | focused test plus core gate | ownership spans otherwise unrelated components |
| Deployment asset | native parser, renderer, or dedicated workflow | preparing a deployment release |
| Test config/fixture | core gate plus offline product suite | always, because collection semantics changed |
| Live system | focused offline proof first | then only the explicitly authorized canary |

## Collection boundaries

- Automated pytest files use `test_*.py` and live under `tests/`.
- Manual probes and operational utilities must not use pytest naming.
- `workspaces/`, `superloops/`, logs, build outputs, and local virtual
  environments are never repository test roots.
- A skip is not a pass. Optional dependencies and platform tests must report
  the reason and remain outside claims about the exercised behavior.

## Test maintenance

- Prefer one end-to-end scenario over several tests of intermediate JSON
  builders when the scenario provides better failure evidence.
- When fixing a bug, first look for an existing test to strengthen or replace.
  Do not append a permanent one-test-per-bug archive indefinitely.
- Review assertions when behavior changes. Never update expected text merely to
  make a failing test green; decide whether the assertion is still a contract.
- Delete stale tests in the same change that retires their behavior.
- During component work, inspect duplicate bodies, constant assertions, tests
  of mocks, and source-text mirrors in the touched area.

## Reporting

Report the exact command and scope, including passed, failed, skipped, and
deselected counts. Use terms such as `focused`, `core gate`, `offline product
suite`, `contract`, or `live canary`.

Do not call a run "authoritative" merely because every collected test passed.
Authority comes from choosing the correct scope and recording what was not
exercised.
