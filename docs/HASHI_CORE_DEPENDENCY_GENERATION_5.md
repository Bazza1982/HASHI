# Core standard dependency generation 5

Status: source candidate for review; running adoption has not been performed.
Functional owner: PAO release governance.
Engineering layer: Core runtime dependency baseline; no product policy is added
to protected Core.

## Decision and scope

The owner approved a Core major-version migration limited to independent
preparation, offline compatibility checks and independent review on 2026-11-05.
Deployment, service switching and restarts are excluded.

The product version advances from 4.0.0b1 to 5.0.0a1, represented as
5.0.0-alpha.1 by npm and Helm application metadata. The independent chart
version is unchanged; no package registry, Git tag or release is published. CPython 3.12.13, Core API 3,
Function API 3, Worker protocol 1 and generation schema 2 remain unchanged.
The exact standard lock changes, so its runtime-policy fingerprint changes even
though the protected Core source files and public protocols do not.

| Standard package | Previous lock | Candidate lock |
| --- | --- | --- |
| aiohttp | 3.14.3 | 3.14.4 |
| certifi | 2026.7.22 | 2026.10.19 |
| multidict | 6.7.1 | 6.9.1 |
| Pillow | 12.3.0 | 13.0.0 |
| yarl | 1.24.5 | 1.25.1 |

The direct aiohttp and Pillow lower bounds in packaging and the standard
requirements also advance to the tested fixed versions. Other standard pins,
platform markers and optional sidecar locks are preserved. The Windows portable
hash lock is regenerated from its existing input, which references the standard
lock and requirements rather than duplicating their version authority. Its
55-package closure changes only these same five package versions; portable-only
pins remain unchanged. yarl is aligned
with the tested HTTP dependency combination; this does not claim a separate
locally exploitable yarl vulnerability.

The changes address published HTTP/cookie, certificate-bundle, reference
lifetime and image-decoder/encoder advisories. Version matches establish
applicability for investigation, not evidence that an instance was attacked.
A clean public advisory audit is not proof that every native library or call
path is safe.

## Verification

Validation uses a separate CPython 3.12.13 environment. No installer targets a
running Core interpreter. The downloaded Windows wheels were checked against
their official full-file PyPI SHA256 values.

The initial-preparation counts below describe commit
`db4fde7ebdf69434b4622a861dee354d80a58bd7` against baseline
`25f88330cd91dd6d1541fcc05d155aeedd1c9a29`. They are historical evidence,
including the wheel/sdist build, rather than a full-suite or artifact claim for
the later integrated source. Integrated validation is recorded separately.

Two bounded regression checks use synthetic values and no network:
a Secure shared cookie must be excluded from HTTP while remaining available
over HTTPS, and multidict reflected-union/subtraction must release operand
references. Both checks fail with the previous affected packages and pass
with the candidate. They are retained in
`tests/test_dependency_profiles.py`.

The focused runtime/Worker/generation/reboot/kernel/handoff and dependency
checks passed: 229 tests. Dependency consistency and the public package audit
also passed. The default Core gate passed 883 tests with one skip. The real npm dry-run
contract passed two checks, and an isolated Python sdist/wheel build carries
5.0.0a1 metadata. All 55 portable wheels passed `pip download --require-hashes`,
their public advisory audit returned no findings, and all 38 portable packaging
tests passed after regenerating that derived lock. Three portable drift failures
found in the broad run were thereby repaired without weakening the validator.
The broad offline selection completed with 8,804 passed, 34 failed and 33 skipped;
the separate serial Antigravity module passed 38 tests. These counts overlap other
focused checks and are not added together as a unique-test total.

Marker/dependency projections can check Windows, Linux and macOS metadata
consistency, but the local execution evidence is Windows only. No production
frontend, provider, browser, external database, optional native runtime, or
model workflow is declared accepted by these offline results.

## Known baseline test failures

Independent serial comparisons against the unmodified source baseline and its
previous dependency lock reproduce all 31 remaining failures in the broad run:

| Test module | Matching failures in each environment |
| --- | ---: |
| `test_request_visibility.py` | 15 |
| `test_wrapper_commands.py` | 1 |
| `test_remote_peer_status.py` | 1 |
| `test_frontend_connector_policy.py` | 1 |
| `test_hashi_cli_process_completion.py` | 2 |
| `test_queue_command.py` | 1 |
| `test_portable_bootstrap_failure_details.py` | 7 |
| `test_forget_api_auth.py` | 2 |
| `test_frontend_settings_api.py` | 1 |

They are recorded as baseline test failures, without claiming that the tests
or product behavior have been adjudicated and repaired by this migration.
The request-visibility failures involve fake stores lacking `get_run`; the other
failures retain their original assertions and evidence. Their presence remains
a release-readiness limitation.

An additional baseline failure,
`tests/test_delay_command.py::test_delay_command_schedules_lists_and_cancels_persistent_message`,
was reproduced with both dependency sets before being explicitly deselected
from the continued broad run. The Antigravity module was run serially because
its collection writes a shared mock executable. Contract/live/platform/wall-clock
tests were excluded by the offline marker selection. The three repaired portable
failures stay in the original 34-failure receipt; a focused green rerun does not
rewrite that historical result. The whole offline product gate is therefore
not reported as a clean pass or as production acceptance.

## Integrated source closeout

The branch integrates main through
`6f78ac7f45b25111e68a737a5e6588152341b6a2`, including lifecycle changes and
the FYI v2 catalogue. Upstream program files and the canonical nine protected
Core files are unchanged from that main; Core 5 guidance is linked from the
engineering domain guide. The complete five-package migration and derived
portable hash lock are retained.

On the integrated source, runtime enforcement and dependency consistency pass
with CPython 3.12.13. Python/npm/Helm application versions agree, and the separate
Helm chart version is preserved. Rechecking official wheel hashes, the complete
55-package portable hash closure and public advisory audits of the 50 standard
and 55 portable packages passes. The two bounded security probes again fail on
the old components and pass on the candidate.

The guard regression suite passes 41 checks. The actual Architecture workflow
script rejects the simulated missing-label branch and accepts the authorized
branch only with the existing major reset and matching independent runtime
seals. These are local workflow simulations; they do not apply a GitHub label.
The new FYI navigation/migration validator also passes. Local selections are
derived from the actual integrated Architecture workflow:

| Workflow selection | Passed | Skipped |
| --- | ---: | ---: |
| Architecture contracts | 905 | 1 |
| Frontend Session and native file content | 755 | 3 |
| Telegram activation and readiness | 189 | 0 |
| Shared voice and personal authorization | 395 | 1 |

The frontend selection runs in two bounded batches whose concatenated selectors
exactly match its workflow list. The selections overlap and are not a unique-test
total. All 38 portable packaging tests also pass on the integrated source. Final
npm/build checks and GitHub job conclusions are recorded separately in the PR
validation report; an absent maintainer label must block Architecture approval.

HCC also consumes the same Core guard. Its PR workflow now maps the trusted
GitHub approval label to the full major/review checks and reruns on label addition
or removal. Existing path filters, the feature-branch push policy, whitespace,
lint and component tests are preserved. Replaying its actual script demonstrates
that the previous workflow still rejected an approved migration, while the new
workflow rejects no-label changes and accepts the fully reviewed authorized
candidate. The HCC component selection passes 506 tests with 11 skips; its
unchanged lint selection passes against main and the candidate with Ruff 0.17.3.
These are offline checks; the actual PR jobs remain gated by the maintainer label.

The exact 32 historical failure nodes were rerun against this main with its
unchanged old lock and against the integrated candidate. Main fails all 32;
the candidate initially fails 31 and passes the gateway variant of
`test_stop_cannot_report_success_with_an_owned_process_or_port_left` once.
Three isolated repetitions then fail in each environment for that same gateway
variant. All 32 baseline issues therefore remain unclosed, including that
inconsistent outcome; its cause is unadjudicated. Test modules are unchanged,
and no unrelated baseline repair is
included. The original approximately nine-thousand-test selection has not been
rerun for this integration. These focused checks do not establish a clean whole
product gate or production acceptance.

## Release and adoption boundaries

The standard lock is part of the immutable runtime identity. A Function
`/reboot` must reject a candidate whose standard dependency fingerprint differs
from the running Core; it cannot adopt this dependency generation.

Before merge, retain the major-version increment, `core-change-approved` label
and a new independent record under `docs/core-reviews/`. The implementer and
reviewer must differ. The record seals the standard lock and project metadata
as well as the protected-source digest. The Architecture CI guard now enforces
this policy for changes to the product major version, the standard lock or its
declared owner, even when protected source is unchanged. The standard-lock owner
is derived from `[tool.hashi.runtime]`; staged metadata and the baseline owner
cannot be hidden by restoring or redirecting a working file. Missing approval,
missing major reset or stale metadata/lock review seals fail closed. The trusted
`core-change-approved` PR label remains a maintainer action before merge;
local authorization flags do not grant or replace that approval.

Deployment needs a separate, explicitly approved maintenance plan: prepare a
complete new runtime, preserve instance configuration and the old release,
quiesce work, switch the Core/runtime together, and verify real frontend and
Worker health. None of those live operations is performed by this preparation
PR. Rollback restores the previous complete release/runtime pair; do not mix
a new lock with old running processes or repair a live interpreter in place.

See [runtime compatibility](HASHI_PYTHON_RUNTIME_COMPATIBILITY.md),
[testing policy](TESTING_POLICY.md), and
[independent review format](core-reviews/README.md).
