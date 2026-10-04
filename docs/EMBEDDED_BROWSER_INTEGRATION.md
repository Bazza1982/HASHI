# Native desktop browser integration

Branch: `simple-upgrade`, based on main `6a4b29b881dd3077c1c2302e4bbc6e41b0277bf9`.

This adds a generic local desktop-browser provider to the existing Browser capability Worker. HASHI remains the authoritative Agent/Session/Run/tool runtime. The desktop owns its pages and final local user consent. No desktop UI, private product code, model routing or second Session store is included here.

## Contract and activation

Run the existing native `tools.device_control_worker` with `--kind browser_control --browser-provider embedded --browser-descriptor <configured path>` and its normal instance source/config/bootstrap options. The desktop integration starts this Worker once; routine actions use authenticated persistent worker HTTP plus local JSONL pipe/socket. The local key is read only beside the configured descriptor and never from model arguments. Windows uses raw overlapped named-pipe I/O, not multiprocessing AF_PIPE framing. Unix uses a finite-deadline socket.

Descriptor schema1 adds `hashi_instance_id` to the desktop generation, transport, endpoint and sibling `agent-bridge.key` reference. Registration `provider_id` defaults legacy workers to `extension` and allows `embedded`. Embedded capability ID/status file is distinct, but device/session identities and write locks remain shared. Optional model argument `browser_target` selects explicitly; the Broker never falls back when an explicit target is missing. Legacy non-broker dispatch also rejects an embedded request.

The existing Tool Registry stamps trusted audit metadata; canonical `hashi_session_id` projects to browser `session_id`. The existing Worker verifies broker identity and leases, stamps instance/Agent/task metadata, then the desktop verifies exact accepted request/Session/tab/handoff/generation. Source webpage text does not acquire system or admin authority. Health and capabilities never expose tokens.

Supported adapter actions: active_tab, get_text, screenshot, get_attribute, wait_for, fill, type_text, select, key, scroll, hover, click. The UI's live page is already open: `browser_session` recreation, raw scripts/HTML, upload, password-vault and extension management are not advertised here. Browser-scope denial does not permit shell/desktop fallback.

## Checks and limits

`python scripts/check_embedded_browser.py` compiles changed Functions and runs the focused broker/worker/JSONL-adapter tests. Tests use fake local endpoints, no real account/browser/desktop.

Native Windows named-pipe and logged-in browser acceptance must run locally. Full remote desktop capability registration without accessible existing device bootstrap is not supplied by this change. Do not advertise it as working or bypass registration. Windows+WSL uses the existing per-user native Worker/shared bootstrap arrangement.

Apply Functions through the supported instance-adoption flow. No protected Core source, user agents.json, credentials, running process, installed service, or browser extension installation is changed by this branch. Keep the other repository at the matching `simple-upgrade` branch for paired acceptance.
