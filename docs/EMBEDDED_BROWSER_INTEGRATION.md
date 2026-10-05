# Native desktop browser integration

Branch: `simple-upgrade`, based on main `6a4b29b881dd3077c1c2302e4bbc6e41b0277bf9`.

This adds a generic local desktop-browser provider to the existing Browser capability Worker. HASHI remains the authoritative Agent/Session/Run/tool runtime. The desktop owns its pages and final local user consent. No desktop UI, private product code, model routing or second Session store is included here.

## Contract and activation

Run the existing native `tools.device_control_worker` with `--kind browser_control --browser-provider embedded --browser-descriptor <configured path>` and its normal instance source/config/bootstrap options. The desktop integration starts this Worker once; routine actions use authenticated persistent worker HTTP plus local JSONL pipe/socket. The local key is read only beside the configured descriptor and never from model arguments. Windows uses raw overlapped named-pipe I/O, not multiprocessing AF_PIPE framing. Unix uses a finite-deadline socket.

The desktop supplies a random `--launch-id` for each owned Worker launch. The local status receipt echoes this ID, `provider_id`, the interpreter PID and `registered_at` only after an authenticated, same-instance registration succeeds. The launch ID correlates startup; it grants no capability and is not part of registration authority. Windows virtual-environment launchers can have a separate interpreter PID. The desktop may accept that correlated receipt but must stop only its own spawned launcher tree, never a PID nominated by a status file. Concurrent handoffs wait for the same completed registration.

Descriptor schema1 adds `hashi_instance_id` to the desktop generation, transport, endpoint and sibling `agent-bridge.key` reference. Registration `provider_id` defaults legacy workers to `extension` and allows `embedded`. Embedded capability ID/status file is distinct, but device/session identities and write locks remain shared. Optional model argument `browser_target` selects explicitly; the Broker never falls back when an explicit target is missing. Legacy non-broker dispatch also rejects an embedded request.

The existing Tool Registry stamps trusted audit metadata; canonical `hashi_session_id` projects to browser `session_id`. The existing Worker verifies broker identity and leases, stamps instance/Agent/task metadata, then the desktop verifies exact accepted request/Session/tab/handoff/generation. Source webpage text does not acquire system or admin authority. Health and capabilities never expose tokens.

Supported adapter actions: active_tab, get_text, screenshot, get_attribute, wait_for, fill, type_text, select, key, scroll, hover, click. The UI's live page is already open: `browser_session` recreation, raw scripts/HTML, upload, password-vault and extension management are not advertised here. Browser-scope denial does not permit shell/desktop fallback.

## Checks and limits

`python scripts/check_embedded_browser.py` compiles changed Functions and runs the focused broker/worker/JSONL-adapter tests. Tests use fake local endpoints, no real account/browser/desktop.

The paired Workbench branch supplies `tests/acceptance/simple_upgrade.test.mjs`. It launches a real native Windows Electron application, supported Python 3.12 Worker, authenticated Broker and Windows named pipe against disposable pages and a HASHI API contract fixture. The fixture is not a running HASHI/model or a logged-in website. Invoke the gate with `HASHI_SIMPLE_TEST_PYTHON` and `HASHI_SIMPLE_TEST_ROOT` pointing to the supported interpreter and this checkout. It uses isolated profiles/bootstrap/state and verifies exact action denial, human approval, pause/takeover and terminal/connection revocation. It fails on unsupported hosts; missing native acceptance is not a pass.

For repository tests clear unrelated inherited `PYTHONPATH` in the command's environment. Another checkout's package metadata can otherwise alter the parent dependency fingerprint while the isolated child correctly ignores that path. Do not disable runtime fingerprint checks or modify a live environment to hide the mismatch. The focused CI uses Python 3.12.13, matching the runtime policy.

Logged-in website acceptance and installer signing remain release checks. Full remote desktop capability registration without accessible existing device bootstrap is not supplied by this change. Do not advertise it as working or bypass registration. Windows+WSL uses the existing per-user native Worker/shared bootstrap arrangement.

Apply Functions through the supported instance-adoption flow. No protected Core source, user agents.json, credentials, running process, installed service, or browser extension installation is changed by this branch. Keep the other repository at the matching `simple-upgrade` branch for paired acceptance.
