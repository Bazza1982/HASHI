# Core/Functions runtime decoupling — Gate 0 reference

Status: source-only reference gate recorded; runtime separation and adoption are NOT complete.

Functional owner: PAO release governance. Engineering layer: protected-Core migration preparation.
Reference source: `abe065907e03805d931c5d2d38dc9989a9fe55a3`; upstream main: `6f78ac7f45b25111e68a737a5e6588152341b6a2`.

## Safety and observed baseline

The selected existing non-production checkout is clean, has one registered writer, and no competing test/build process. The running instance remains on its existing sealed Function artifact. Intake was idle at the start. Source checkout and running adoption are separate; no service, credential, database, active environment or durable task payload was changed.

The observed Core, shared and Agent interpreter/prefix, exact installed distribution metadata, current generation, health and PID references are retained in ignored qualification receipts. The complete observed distribution digest matches the running Core fingerprint. Machine paths, account identities, credentials and user/task data are intentionally absent here.

The canonical protected manifest has nine paths. Its complete static import closure has no application/third-party importer. A real CPython 3.12.13 process with `-I -S` imports the protected supervisor, transport, artifact verifier and child bootstrap successfully with no site-package module loaded.

**Expected existing coupling:** importing `main` in the same clean process exits 78 because the current startup contract requires the entire application lock. This observed failure is preserved; it is the boundary the next gates must remove, not evidence of a newly introduced failure.

## Dependency ownership inventory

All 50 currently applicable standard-profile requirements stay in common Functions for the first split. No standard package is reassigned to the Core host. Existing optional OCR/STT/TTS/browser/GPU profiles and sealed L2 Safety/Runner capsules retain their existing owners and qualification requirements. An importer below can also belong to an existing sidecar; retaining the common Functions baseline does not merge those sidecar installations.

This dated inventory is evidence, not a second lock or registry. Exact full importer and native-file lists are in ignored audit receipts. Requirements without a direct importer remain required through their declared dependency closure; they are not dropped.

| Distribution | Original version | Direct tracked import examples / declared users | Native files observed |
| --- | --- | --- | --- |
| aiohappyeyeballs | 2.7.1 | Transitive: aiohttp | 0 |
| aiohttp | 3.14.3 | `browser_gateway/server.py`, `onboarding/connection.py`, `orchestrator/agent_presence_call_video.py` (+31 importers) | 4 |
| aiosignal | 1.4.0 | Transitive: aiohttp | 0 |
| annotated-doc | 0.0.5 | Transitive: fastapi | 0 |
| annotated-types | 0.8.0 | Transitive: pydantic | 0 |
| anyio | 4.15.0 | Transitive: httpcore, httpx, starlette | 0 |
| attrs | 26.1.0 | Transitive: aiohttp, jsonschema, referencing | 0 |
| certifi | 2026.7.22 | Transitive: httpcore, httpx | 0 |
| cffi | 2.1.1 | Transitive: cryptography, python-telegram-bot | 1 |
| click | 8.5.0 | Transitive: httpx, uvicorn | 0 |
| croniter | 6.2.4 | `orchestrator/scheduler.py` | 0 |
| cryptography | 50.0.1 | `orchestrator/agent_move/package.py`, `orchestrator/agent_move/transport_crypto.py`, `orchestrator/canonical_audit.py` (+16 importers) | 1 |
| fastapi | 0.141.1 | `remote/api/server.py`, `remote/security/auth.py`, `tools/windows_helper/server.py` | 0 |
| frozenlist | 1.8.0 | Transitive: aiohttp, aiosignal | 1 |
| h11 | 0.16.0 | Transitive: httpcore, uvicorn | 0 |
| httpcore | 1.0.9 | Transitive: httpx, python-telegram-bot | 0 |
| httpx | 0.28.1 | `adapters/deepseek_api.py`, `adapters/hashi_api.py`, `adapters/hashi_xai_oauth.py` (+17 importers) | 0 |
| idna | 3.19 | Transitive: anyio, httpx, jsonschema, yarl | 0 |
| ifaddr | 0.2.0 | `orchestrator/service_endpoints.py`, `remote/local_http.py`, `remote/peer/lan.py` | 0 |
| jsonschema | 4.26.0 | `adapters/deepseek_tool_protocol.py`, `tools/gateway/mcp_stdio.py` | 0 |
| jsonschema-specifications | 2025.9.1 | Transitive: jsonschema | 0 |
| linkify-it-py | 2.2.0 | Transitive: markdown-it-py | 0 |
| markdown-it-py | 4.2.0 | Transitive: mdit-py-plugins, rich, textual | 0 |
| mdit-py-plugins | 0.6.1 | Transitive: markdown-it-py, textual | 0 |
| mdurl | 0.1.2 | Transitive: markdown-it-py | 0 |
| multidict | 6.7.1 | Transitive: aiohttp, yarl | 1 |
| pillow | 12.3.0 | `orchestrator/file_content_service.py`, `orchestrator/media_utils.py`, `skills/vision/scripts/vision.py` (+7 importers) | 8 |
| platformdirs | 4.11.7 | Transitive: textual | 0 |
| propcache | 0.5.2 | Transitive: aiohttp, yarl | 1 |
| psutil | 7.2.2 | `scripts/hashi_instance_cli.py`, `tools/builtins.py`, `tools/pii_model_probe.py` | 1 |
| pycparser | 3.0 | Transitive: cffi | 0 |
| pydantic | 2.13.5 | `remote/api/server.py`, `specs/ete/v1/ete_types.py`, `tools/windows_helper/server.py` | 0 |
| pydantic-core | 2.46.5 | Transitive: pydantic | 1 |
| pygments | 2.21.0 | Transitive: httpx, rich, textual | 0 |
| pymupdf | 1.28.2 | `tools/media_read.py`, `veritas/adapters/mineru_extractor.py` | 3 |
| python-dateutil | 2.9.0.post0 | Transitive: croniter | 0 |
| python-telegram-bot | 22.8 | `orchestrator/command_registry.py`, `orchestrator/command_request_context.py`, `orchestrator/commands/anatta.py` (+41 importers) | 0 |
| pyyaml | 6.0.3 | `flow/agents/evaluator/evaluator.py`, `flow/flow_registry.py`, `flow/flow_trigger.py` (+8 importers) | 1 |
| referencing | 0.37.0 | Transitive: jsonschema, jsonschema-specifications | 0 |
| rich | 15.0.0 | `tui/app.py`, `tui/side_panel.py`, `tui/telegram_rendering.py` (+1 importers) | 0 |
| rpds-py | 2026.6.3 | Transitive: jsonschema, referencing | 1 |
| six | 1.17.0 | Transitive: python-dateutil | 0 |
| starlette | 1.6.0 | Transitive: fastapi | 0 |
| textual | 8.2.8 | `tui/app.py`, `tui/connection.py`, `tui/side_panel.py` (+1 importers) | 0 |
| typing-extensions | 4.16.0 | Transitive: aiohttp, aiosignal, anyio, cryptography, fastapi, multidict, pydantic, pydantic-core, referencing, starlette, textual, typing-inspection, uvicorn | 0 |
| typing-inspection | 0.4.4 | Transitive: fastapi, pydantic | 0 |
| tzdata | 2026.3 | Transitive: pydantic | 0 |
| uvicorn | 0.52.4 | `remote/main.py`, `tools/windows_helper/server.py` | 0 |
| yarl | 1.24.5 | Transitive: aiohttp | 1 |
| zeroconf | 0.151.3 | `remote/peer/lan.py` | 18 |

Native file observations are from the approved Windows CPython 3.12 / AMD64 reference. Windows ABI, Linux wheels and platform markers must be qualified separately when the role builder is introduced; a pure-Python importer does not prove cross-platform native compatibility.

## Existing authorities to preserve

- Runtime policy/identity: `orchestrator/runtime_contract.py` and `[tool.hashi.runtime]`.
- Sealed Function code and probe: `function_generation.py`; shared release: `runtime_release.py`; worker/rollback: `function_worker_supervisor.py` and `kernel_process.py`.
- Fixed child import boundary: `function_worker_bootstrap.py`; existing bounded JSON IPC: `function_worker_protocol.py`.
- Git/Windows launch: `bin/bridge-u.bat`, `bin/bridge_ctl.ps1`, `scripts/check_runtime_contract.py` and `scripts/resolve_instance_runtime.py`.
- Portable: `packaging/portable_windows/build.py`, `requirements.in` and derived hash lock.
- Distribution projections: `pyproject.toml`, `package.json`, `deploy/helm/hashi-enterprise/Chart.yaml` and existing npm bootstrap.
- L1/L2 installation, Safety/Runner and runtime capsules remain independent; no permission implementation or release assertion is waived by this inventory.

## Baseline regression evidence

Twelve initial runtime, lifecycle, release, dependency and Portable selectors pass: **277 passed**. The remaining 30 canonical Core-gate modules pass **654 passed, 1 skipped**. Reading the JUnit case identities verifies the union covers the unchanged 39-module curated Core gate: **881 passed, 1 skipped**; all extra focused cases are separately counted. No test is weakened or deselected to produce this result.

The initial combined invocation hit its external 250-second command limit before completion. Its original log/timeout receipt is retained. The same unmodified selectors were partitioned and all completed; no product watchdog or assertion was changed. The real full-product preparation is the largest observed baseline cost (128.42 seconds); candidate probe and startup timing remain explicit comparison inputs for later gates.

Checks used the existing tests: `test_runtime_contract`, `test_minimal_core`, `test_kernel_process`, `test_function_generation`, `test_function_worker_protocol`, `test_function_worker_supervisor`, `test_runtime_handoff`, `test_reboot_manager`, `test_runtime_release`, `test_version_info`, `test_dependency_profiles`, `test_portable_windows_packaging`, plus exactly the remaining `testpaths` entries. Each process uses its own temporary root and preserves JUnit and full command receipts.

This is a reference baseline, not full Function parity, full native L2/WAC, external-provider/device acceptance or release approval. PR #137's separately recorded broad-suite failures are not silently accepted or claimed fixed; the eventual complete Function parity matrix must preserve/adjudicate them against its actual baseline.

## Next gate

Implement strict role-specific policy/fingerprints and trusted manifest admission; prove a zero-dependency Core profile and exact Function profile, then qualify one cross-interpreter launcher for shared, Agent, probe and recovery. Only after original-dependency full parity may the five reviewed security updates enter a new Functions runtime. Existing service adoption, Core restart, merge and installation remain separately gated.
