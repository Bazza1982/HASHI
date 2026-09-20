# Working on HASHI

Before editing, read `ARCHITECTURE.md`, `docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`,
and `docs/TESTING_POLICY.md`; for command/UI work also read
`docs/HASHI_COMMAND_UI_STYLE_GUIDE.md`.

- Before editing, state the functional owner (PCM, PAO, HER v2, or Frontend
  Connector), engineering layer, and focused validation.
- Normal features belong in Functions or platform/instance configuration. Use
  the narrowest owner; derive views instead of copying model/effort lists,
  command metadata, ports, or state writers.
- Protected paths come only from `orchestrator.runtime_contract.CORE_SOURCE_PATHS`.
  Run `python scripts/check_protected_core_changes.py` when choosing files and
  before finishing; it covers staged, unstaged, and untracked changes.
- Protected Core is immutable unless the current user explicitly authorizes a
  **Core major-version migration**. Bug fixes, refactors, reboot requests, broad
  approval, and requests to "protect Core" are not that authorization.
- An authorized Core migration raises major, resets minor/patch, carries
  `core-change-approved`, and adds an independent `docs/core-reviews/` review.
  Implementer and reviewer differ; one reviewed PR contains one Core generation.
- `--authorized` and command-scoped `HASHI_CORE_EDIT_AUTHORIZED=1` only record
  authorization already given; neither bypasses version/review gates. Never
  persist Core authorization variables. See
  `docs/HASHI_CORE_PROTECTION_HARDENING_2026-09-13.md`.
- Instance model/effort opt-ins belong in `allowed_backends` and resolve through
  `runtime_effort_options`; never edit the shared catalogue merely to add one.
  Shared compatibility belongs to the qualified Function generation, not Core.
- Core imports no product module, including lazily. Shared services run in a
  replaceable Function process. `/reboot` stays Agent-scoped; separate shared
  replacement needs broad operational scope.
- Never install/upgrade Function dependencies in a running Core interpreter.
  Optional/native dependencies use an isolated, configured sidecar. Normal
  Function changes adopt through hot `/reboot`, never a Core cold restart.
- UI wording belongs in renderers/catalogs. Use shared card/navigation helpers,
  escaped HTML, and the chosen UI locale. Retired Workbench compatibility names
  mean Backend API, not an active HASHI frontend.
- Test observable behavior and persistence/failure boundaries. For a bug, record
  focused red/green evidence. Fake switching or copied prose proves no switch.
- Respect other checkout work. Keep identities, secrets, and machine paths in
  ignored configuration. Never infer identity from a folder name.
- Migrated JSON configuration reuses `orchestrator.config_json` and retains its
  read revision; display fallback is never writable. Do not blindly retry a
  conflict or committed durability error. See `docs/HASHI_CONFIGURATION_PERSISTENCE.md`.
- Reboot/restart is operational, not a test shortcut. Follow current scope. If
  forbidden, finish offline checks and leave adoption explicitly unverified.
  Source, artifacts, and running Workers are separate facts; report what passed.
- "Run essential live test" invokes `docs/HASHI_FRONTEND_LIVE_ACCEPTANCE.md` and
  its versioned manifest through the named external frontend, not automation
  alone. Resolve checkout, instance, and Agent; never widen or carry authority.
  Keep screenshots, receipts, snapshots, and observations distinct. Every
  mandatory item and the final Core/source invariant must pass.
- Update the owning decision and FYI when behavior changes. Record approval,
  implementation and live verification separately, scoped to branch/instance.

Install the local hook with `python scripts/install_engineering_hooks.py`; it
preserves custom hooks and checks staged Core/whitespace. CI remains final.
