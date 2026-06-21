# HASHI Hermes Agent Transfer Protocol Plan

## Purpose

HASHI already has mature agent movement primitives:

- `scripts/import_openclaw.py` imports OpenClaw agents into HASHI.
- `scripts/move_agent.py` moves or packages HASHI agents between HASHI
  instances.
- HASHI Remote and HChat provide authenticated cross-instance communication.

Hermes currently has a HASHI connector, but that connector is a communication
bridge, not an agent migration system. It exposes Hermes profiles as HASHI
Remote peers and routes HChat into a live Hermes session, but it does not export
or import complete agent definitions.

This plan defines a two-way transfer protocol:

- HASHI agent to Hermes profile.
- Hermes profile to HASHI agent.

The protocol should be implemented as a durable package format plus explicit
import/export tools. It should not rely on an LLM to summarize, infer, or repair
agent state during transfer.

## Current Assets

### HASHI

Relevant files:

- `scripts/import_openclaw.py`
  - One-way OpenClaw to HASHI importer.
  - Imports identity, memory files, Telegram tokens, cron jobs, API credentials,
    scripts, and skills.
- `scripts/move_agent.py`
  - HASHI to HASHI migration tool.
  - Supports direct instance moves, USB/package export, encrypted secrets, and
    workspace transfer.
  - Uses `.hashi-agent` package files.
- `orchestrator/runtime_transfer.py`
  - Runtime handoff payload builder for live session transfer.
- `orchestrator/transfer_store.py`
  - Persistent transfer transaction store.
- `orchestrator/flexible_agent_runtime.py`
  - `/transfer` and remote transfer command handling.
- `orchestrator/runtime_jobs.py` and `orchestrator/skill_manager.py`
  - Job transfer and scheduler integration.

HASHI agent state is usually distributed across:

- `agents.json`
- `secrets.json`
- `tasks.json`
- `workspaces/<agent>/agent.md`
- `workspaces/<agent>/bridge_memory.sqlite`
- `workspaces/<agent>/recent_context.jsonl`
- `workspaces/<agent>/transcript.jsonl`
- `workspaces/<agent>/state.json`
- optional `memory/`, `skills/`, `scripts/`, and other workspace files.

### Hermes Connector

Relevant files in `hashi-connect-hermes`:

- `README.md`
  - Describes the connector as a HASHI Remote/HChat integration.
- `docs/architecture.md`
  - Bridge server, Hermes plugin, and Hermes skill architecture.
- `docs/security.md`
  - HMAC shared-token and runtime-state safety notes.
- `examples/agents.yaml`
  - Hermes bridge agent directory.
- `examples/peers.yaml`
  - HASHI peer routing config.
- `examples/hermes-profile-config.yaml`
  - Hermes profile plugin configuration.
- `src/hashi_connect_hermes/bridge/adapter.py`
  - FastAPI bridge exposing `/health`, `/protocol/handshake`,
    `/protocol/agents`, `/protocol/message`, `/hchat`, and local queue APIs.
- `src/hashi_connect_hermes/bridge/hchat_bridge.py`
  - Queue, reply, and outbound HChat routing.
- `hermes_plugin/platforms/hashi_hchat/adapter.py`
  - Hermes platform plugin that injects incoming HChat into the live session.
- `hermes_skills/hchat/`
  - Hermes skill for model-mediated outbound HChat.

Hermes connector state is usually distributed across:

- `HASHI_CONNECT_HERMES_HOME`
- `agents.yaml`
- `peers.yaml`
- `.shared_token` or `HASHI_REMOTE_SHARED_TOKEN`
- `message_queue.json`
- Hermes profile `config.yaml`
- Hermes profile `platforms/hashi_hchat`
- Hermes profile `skills/hchat`
- Hermes session/profile state, depending on the Hermes runtime installation.

## Non-Goals

- Do not merge HASHI and Hermes runtimes.
- Do not depend on a source or target agent LLM being alive.
- Do not make live HChat equivalent to durable agent migration.
- Do not silently copy secrets in plaintext.
- Do not enable imported agents automatically without explicit operator
  approval.
- Do not assume Hermes internal memory/session schemas are stable unless they
  are confirmed by adapter tests.
- Do not migrate Hermes profile directories wholesale.
- Do not migrate active Hermes sessions, live cron execution state, runtime
  caches, or messaging delivery credentials by default.

## Hermes Runtime Review Findings

These findings come from a live Hermes-side review by `xiaoye@HERMES`. They
should be treated as design constraints until the Hermes runtime adapter proves a
more precise behavior through tests.

### Profile isolation

Hermes profiles are isolated under a profile root such as:

```text
~/.hermes/profiles/<name>/
```

Important subdirectories can include:

- `skills/`
- `plugins/`
- `cron/`
- `memories/`
- session/runtime state such as SQLite session stores

The importer must not copy the whole profile directory. It must use an explicit
profile subdirectory whitelist. Unknown profile files should be preserved only
inside the transfer package as source evidence or skipped with a warning.

### Hermes memory constraints

Hermes memory is profile-scoped and constrained. The current practical limit
reported by the Hermes-side reviewer is approximately 2,200 characters per
memory item, with time-scope and freshness constraints.

Therefore:

- Memory import must validate item size and age.
- Oversized or stale entries must be skipped or converted to portable notes.
- Memory import must report accepted, skipped, and truncated counts.
- Direct writes into Hermes `memories/` require a tested Hermes adapter.

### Session state is not portable memory

Hermes session stores can contain transient conversation state, tool-call
chains, operator instructions, and runtime details. Session SQLite files or
equivalent session stores must not be migrated as durable identity or memory.

Allowed handling:

- Include session metadata in audit only when explicitly requested.
- Never replay active sessions into HASHI or Hermes automatically.
- Use a post-import self-check prompt instead of session replay.

### Cron state is runtime-bound

Hermes cron entries can include runtime-bound fields such as:

- `deliver`
- `profile`
- `context_from`

These fields can point to the wrong target after migration or cause duplicate
execution. All Hermes cron jobs must export as paused review drafts. They must
not be resumed automatically on either runtime.

### Plugins can contain local machine state

Some Hermes plugins can embed local paths, caches, OAuth state, voice models, or
media generation state. Examples called out by the Hermes-side review include
voice/TTS and media generation plugins.

Plugin import policy:

- Copy only explicitly whitelisted plugins.
- Treat plugin config as environment-specific.
- Do not migrate plugin caches.
- Do not migrate OAuth or local credential material.
- Generate review warnings for path-like values.

### Messaging delivery configuration is excluded

All `send_message` delivery configuration is excluded by default, including:

- Telegram tokens
- chat IDs
- webhooks
- gateway delivery targets

Target delivery configuration must be recreated or explicitly approved by the
operator after import.

### Credentials are excluded by default

Hermes-side feedback recommends excluding credential files even when encrypted.
HASHI may still support encrypted secret packaging for local trusted moves, but
the default policy for Hermes transfers is:

- no credentials
- no `.shared_token`
- no Telegram tokens
- no OAuth material
- no voice/media provider keys

Any exception must be represented in `secrets.policy` and require an explicit
CLI flag.

## Terminology

- **Transfer package**: A portable archive containing normalized agent data,
  source-specific files, checksums, and an import manifest.
- **Source runtime**: The runtime from which the agent is exported.
- **Target runtime**: The runtime into which the agent is imported.
- **Copy transfer**: Source remains enabled.
- **Move transfer**: Source is disabled only after target import and validation
  succeed.
- **Review gate**: Imported agent is created disabled or inactive until an
  operator confirms it is safe to enable.
- **Runtime adapter**: Code that maps runtime-specific files to and from the
  normalized transfer model.

## Package Format

Use a new package extension:

```text
.hashi-hermes-agent
```

This avoids ambiguity with the current `.hashi-agent` package, which is
HASHI-to-HASHI focused.

Recommended archive layout:

```text
<agent>_<timestamp>.hashi-hermes-agent
├── manifest.json
├── normalized_agent.json
├── source/
│   ├── runtime.json
│   ├── hashi_agent_config.json
│   ├── hermes_agents_entry.yaml
│   ├── hermes_profile_config.yaml
│   └── raw_paths.json
├── identity/
│   ├── agent.md
│   ├── hermes_instructions.md
│   └── notes.md
├── memory/
│   ├── files/
│   ├── sqlite/
│   ├── jsonl/
│   └── import_notes.json
├── workspace/
├── skills/
├── scripts/
├── schedules/
│   ├── tasks.json
│   └── schedule_notes.json
├── profile_policy.json
├── secrets.policy.json
├── secrets.json
├── secrets.bin
└── audit/
    ├── checksums.json
    ├── dry_run_plan.json
    ├── transfer_report.md
    ├── migration_report.md
    ├── post_migration_self_check.md
    └── warnings.json
```

Only one of `secrets.json` or `secrets.bin` should exist. `secrets.bin` is the
default for transfers that include secrets.

### `manifest.json`

Required fields:

```json
{
  "schema_version": 1,
  "package_type": "hashi-hermes-agent",
  "package_id": "pkg-...",
  "created_at": "2026-06-22T00:00:00Z",
  "created_by": "hashi",
  "source_runtime": "hashi",
  "target_runtime": "hermes",
  "agent_id": "zelda",
  "display_name": "Zelda",
  "transfer_mode": "copy",
  "contains_secrets": false,
  "secrets_encrypted": false,
  "contains_memory": true,
  "contains_workspace": true,
  "source_disable_policy": "never",
  "target_enable_policy": "manual_review",
  "profile_directory_policy": "whitelist_only",
  "cron_import_policy": "paused_review_drafts",
  "memory_import_policy": "validate_size_age",
  "session_import_policy": "never",
  "secrets_policy_file": "secrets.policy.json",
  "checksums_file": "audit/checksums.json"
}
```

Allowed values:

- `source_runtime`: `hashi`, `hermes`
- `target_runtime`: `hashi`, `hermes`
- `transfer_mode`: `copy`, `move`
- `source_disable_policy`: `never`, `after_verified_import`
- `target_enable_policy`: `manual_review`, `enable_after_import`
- `profile_directory_policy`: `whitelist_only`
- `cron_import_policy`: `paused_review_drafts`, `skip`
- `memory_import_policy`: `portable_notes_only`, `validate_size_age`
- `session_import_policy`: `never`

`enable_after_import` must require an explicit CLI flag.

### `profile_policy.json`

This file records which Hermes profile subdirectories are allowed to migrate.
It is mandatory for any transfer involving Hermes.

Example:

```json
{
  "schema_version": 1,
  "runtime": "hermes",
  "allowed_profile_subdirs": [
    "skills",
    "memories"
  ],
  "blocked_profile_subdirs": [
    "sessions",
    "cron/runtime",
    "plugins/cache"
  ],
  "plugin_policy": {
    "mode": "explicit_allowlist",
    "allowed_plugins": [
      "hashi_hchat"
    ],
    "blocked_reason": "plugins may contain local paths, caches, OAuth state, or provider credentials"
  },
  "memory_policy": {
    "max_chars_per_item": 2200,
    "stale_item_action": "skip_with_warning",
    "oversize_item_action": "convert_to_portable_note"
  },
  "cron_policy": {
    "default_state": "paused",
    "blocked_fields": ["deliver", "profile", "context_from"]
  }
}
```

### `secrets.policy.json`

This file is mandatory even when no secrets are included.

Example:

```json
{
  "schema_version": 1,
  "default": "exclude",
  "included": false,
  "encryption": "none",
  "allowed_secret_classes": [],
  "blocked_secret_classes": [
    "telegram_token",
    "telegram_chat_id",
    "webhook",
    "oauth_token",
    "voice_model_key",
    "media_generation_key",
    "hashi_remote_shared_token"
  ],
  "target_decryption_allowed": false,
  "operator_approval_required": true
}
```

If encrypted secrets are explicitly included, `encryption` must describe the
scheme and `target_decryption_allowed` must be explicit. Hermes transfers should
still default to `included: false`.

### `normalized_agent.json`

This is the runtime-independent core record:

```json
{
  "agent_id": "zelda",
  "display_name": "Zelda",
  "description": "",
  "emoji": "",
  "identity_text_path": "identity/agent.md",
  "preferred_backend": {
    "engine": "codex-cli",
    "model": "gpt-5.5"
  },
  "capabilities": {
    "hchat": true,
    "remote": true,
    "workspace_write": true,
    "scheduled_jobs": true
  },
  "memory": {
    "strategy": "portable_files_first",
    "notes_path": "memory/import_notes.json",
    "max_chars_per_item": 2200,
    "session_state_included": false
  },
  "skills": [],
  "schedules": [],
  "secrets": {
    "included": false,
    "encrypted": false,
    "keys": []
  }
}
```

The normalized record should be conservative. It should not claim a capability
unless the exporter observed it directly or the user explicitly requested it.

## Direction A: HASHI to Hermes

### Export

Input:

- HASHI root.
- Agent ID.
- Optional include/exclude flags for workspace, memory, schedules, secrets, and
  skills.

Read:

- `agents.json`
- `secrets.json`
- `tasks.json`
- `workspaces/<agent>/agent.md`
- `workspaces/<agent>/state.json`
- `workspaces/<agent>/bridge_memory.sqlite`
- `workspaces/<agent>/recent_context.jsonl`
- `workspaces/<agent>/transcript.jsonl`
- `workspaces/<agent>/memory/`
- `workspaces/<agent>/skills/`
- `workspaces/<agent>/scripts/`

Write package:

- `manifest.json`
- `normalized_agent.json`
- `source/hashi_agent_config.json`
- `identity/agent.md`
- selected memory/workspace files
- disabled schedule drafts
- encrypted secrets if requested
- checksums and dry-run plan

Default behavior:

- Dry-run first.
- Do not include secrets unless `--include-secrets`.
- Do not disable the source HASHI agent.
- Mark target Hermes profile as review required.

### Import into Hermes

Input:

- Package.
- Hermes profile directory.
- `HASHI_CONNECT_HERMES_HOME`.
- Optional peer route target.

Write:

- Hermes profile instruction file derived from `identity/agent.md`.
- Hermes profile `config.yaml` merge plan for `platforms/hashi_hchat`.
- `skills/hchat` if absent or explicitly refreshed.
- bridge `agents.yaml` entry for the imported profile.
- optional `peers.yaml` entry for the source HASHI peer.
- memory files into a clearly named import folder, not directly into opaque
  Hermes runtime internals unless a tested adapter exists.

Default target state:

- Added to `agents.yaml` with review metadata.
- Hermes profile not assumed live until the operator restarts Hermes or runs an
  explicit reload command.
- Target profile remains in disabled or review mode until the operator approves
  the import report and post-migration self-check.
- HASHI agent ID to Hermes profile name mapping must be written explicitly in
  the dry-run plan and migration report.

Open issue:

- Hermes internal memory/session schema is not fully represented in the local
  connector repository. First implementation should preserve memory as portable
  files plus import notes. Direct memory injection should be a later adapter.

Hermes-specific import rules:

- Create or update only whitelisted profile subdirectories.
- Do not import session databases.
- Do not import active runtime state.
- Do not import delivery credentials.
- Convert schedules into paused review drafts.
- Validate memory item size and age before writing to Hermes memory.
- Generate review warnings for plugin config values that look like local paths,
  caches, OAuth state, or provider credentials.

## Direction B: Hermes to HASHI

### Export

Input:

- Hermes profile directory.
- `HASHI_CONNECT_HERMES_HOME`.
- Hermes agent/profile name.

Read:

- Hermes profile `config.yaml`.
- Hermes profile `skills/`.
- Hermes profile `platforms/`.
- Hermes profile `memories/`, subject to item size and age validation.
- Hermes profile `cron/`, exported as paused review drafts only.
- bridge `agents.yaml`.
- bridge `peers.yaml`.
- connector queue state only for audit, not as durable memory.
- any confirmed Hermes profile instruction or memory files.

Write package:

- `manifest.json`
- `normalized_agent.json`
- `source/hermes_profile_config.yaml`
- `source/hermes_agents_entry.yaml`
- `identity/hermes_instructions.md`
- skills and profile files that are safe to copy
- memory import notes
- profile whitelist policy
- secrets policy
- paused schedule drafts
- checksums and dry-run plan

Default behavior:

- Do not include `.shared_token` unless explicitly requested.
- Do not export transient `message_queue.json` as memory.
- Do not claim full memory migration unless Hermes memory files are identified.
- Do not export `sessions.db` or equivalent active session state.
- Do not export `send_message` delivery config, Telegram tokens, chat IDs, or
  webhooks.
- Do not export plugin caches, OAuth state, voice model files, or media provider
  credentials.
- Source Hermes profile must enter a disabled state for `move` mode after the
  HASHI import is verified. A marker alone is not sufficient.

### Import into HASHI

Input:

- Package.
- HASHI root.
- Target agent ID.

Write:

- `workspaces/<agent>/agent.md`
- `workspaces/<agent>/hermes_import/`
- `workspaces/<agent>/skills/` where compatible
- `agents.json` entry with `is_active: false` by default
- optional `secrets.json` entries only after decrypting package secrets
- optional `tasks.json` drafts disabled by default
- `workspaces/<agent>/hermes_import/migration_report.md`
- `workspaces/<agent>/hermes_import/post_migration_self_check.md`

Recommended HASHI agent config:

- `type`: `flex`
- `active_backend`: selected by operator or default HASHI backend.
- `system_md`: `workspaces/<agent>/agent.md`
- `workspace_dir`: `workspaces/<agent>`
- `is_active`: `false`
- `background_mode`: `true`
- `access_scope`: conservative default, probably `project`.

Default target state:

- Imported but disabled.
- Operator must run review command or pass `--enable` to activate.

## CLI Design

Use a new script first:

```bash
python scripts/transfer_hermes_agent.py plan \
  --from hashi \
  --agent zelda \
  --target-runtime hermes \
  --target-profile /path/to/hermes/profile

python scripts/transfer_hermes_agent.py export \
  --from hashi \
  --root /home/lily/projects/hashi \
  --agent zelda \
  --out ./packages

python scripts/transfer_hermes_agent.py import \
  --to hermes \
  --package ./packages/zelda.hashi-hermes-agent \
  --profile /path/to/hermes/profile \
  --bridge-home ~/.hashi-connect-hermes

python scripts/transfer_hermes_agent.py export \
  --from hermes \
  --profile /path/to/hermes/profile \
  --bridge-home ~/.hashi-connect-hermes \
  --agent assistant \
  --out ./packages

python scripts/transfer_hermes_agent.py import \
  --to hashi \
  --package ./packages/assistant.hashi-hermes-agent \
  --root /home/lily/projects/hashi
```

Later, the same functionality can be exposed through:

```text
/agent transfer
/agent export
/agent import
```

or integrated into `/move` after the CLI and schema are stable.

## Safety Rules

1. Dry-run is the default for any operation that writes outside a temporary
   package directory.
2. Existing target agents or profiles are never overwritten unless
   `--overwrite` is provided.
3. Imported agents are disabled by default.
4. Source agents are disabled only in `move` mode and only after target import
   passes validation.
5. Secrets are excluded by default.
6. Secret inclusion requires either encrypted package output or an explicit
   `--plain-secrets` flag.
7. Runtime queue files are not treated as durable memory.
8. Path rewrites must be explicit and audited.
9. Owner/resource mismatch checks must run before enabling imported jobs.
10. Package checksums must be validated before import.
11. Import reports must list every file that will be written.
12. Rollback information must be generated before modifying target config files.
13. Hermes profile imports must use a subdirectory whitelist.
14. Hermes memory imports must enforce size and age checks.
15. Hermes cron imports must create paused review drafts only.
16. Active sessions and runtime state must never be imported.
17. Messaging delivery configuration is excluded unless an explicit adapter and
    operator approval exist.
18. Every successful import must produce a migration report and a
    post-migration self-check prompt.

## Validation Gates

### Export validation

- Source root/profile exists.
- Source agent exists.
- Required identity material exists or a placeholder is generated with a
  warning.
- Package manifest validates against schema.
- Checksums match package content.
- Secret mode is explicit.
- For Hermes export, profile whitelist policy exists.
- For Hermes export, memory entries are counted, size-checked, and age-checked.
- For Hermes export, cron entries are marked paused.
- For Hermes export, session stores and delivery credentials are excluded.

### Import validation

- Target root/profile exists.
- Target config files are parseable.
- Target agent conflict handling is explicit.
- Package source and target runtimes are compatible with the requested command.
- Checksums pass.
- Planned writes are inside the approved target root/profile/bridge home.
- Imported schedules are disabled unless explicitly enabled.
- Hermes target profile name is explicitly mapped from the source agent ID.
- Migration report is generated before enabling the target.
- Post-migration self-check instructions are written before enabling the target.

### Post-migration verification

After import, the target runtime must perform a self-check before the operator
enables the transferred agent/profile.

Minimum self-check:

- Confirm target runtime and agent/profile name.
- Confirm imported identity file path.
- Confirm skills copied or skipped.
- Confirm memory accepted/skipped counts.
- Confirm schedules are disabled or paused.
- Confirm no secrets were imported unless explicitly approved.
- Confirm HChat/Remote bridge status if enabled.

For Hermes targets, the self-check should run inside the target Hermes profile
after restart or reload. For HASHI targets, it can be run as a disabled-agent
review command or a one-shot validation prompt before activation.

## Test Plan

Initial tests:

- HASHI exporter builds a valid package from a fixture `agents.json`,
  `secrets.json`, `tasks.json`, and workspace.
- HASHI importer creates a disabled agent and does not touch unrelated agents.
- Hermes exporter reads fixture `config.yaml`, `agents.yaml`, and `skills/`.
- Hermes importer updates fixture `agents.yaml` and profile config through a
  merge plan.
- Hermes importer refuses non-whitelisted profile subdirectories.
- Hermes memory importer skips or converts entries above the configured
  character limit.
- Hermes cron importer marks every imported cron entry paused.
- Hermes exporter excludes sessions and delivery credentials.
- `secrets.policy.json` is required and defaults to excluding all secrets.
- Existing target conflict defaults to failure or skip.
- Secret package encryption/decryption round trips.
- Plain secret export requires explicit flag.
- Checksums catch corrupted package entries.
- Dry-run performs no writes.
- Migration report includes source/target profile, skill list, cron states,
  memory counts, and secrets status.
- Post-migration self-check file is generated.
- Round trip:
  - HASHI fixture to package.
  - package to Hermes fixture.
  - Hermes fixture to package.
  - package to HASHI fixture.
  - identity and capability fields survive the round trip.

Focused integration tests:

- Imported Hermes profile appears in `/protocol/agents`.
- Imported HASHI agent appears in HASHI agent directory after reload.
- HChat smoke works after operator enables the imported profile/agent.

## Implementation Phases

### Phase 1: Schema and package utilities

Deliverables:

- `orchestrator/hermes_transfer/schema.py`
- package writer/reader
- checksum validator
- dry-run report model
- tests for manifest validation and checksum failure

No runtime integration yet.

### Phase 2: HASHI exporter

Deliverables:

- Read HASHI agent config, workspace, optional memory, schedules, and optional
  secrets.
- Write `.hashi-hermes-agent` packages.
- Generate `audit/dry_run_plan.json` and `audit/transfer_report.md`.

### Phase 3: HASHI importer

Deliverables:

- Import package into HASHI as disabled agent.
- Write workspace files and `agent.md`.
- Update `agents.json` safely.
- Add disabled schedule drafts where requested.
- Include rollback snapshots for modified files.

### Phase 4: Hermes exporter

Deliverables:

- Read Hermes profile config and bridge `agents.yaml`.
- Export Hermes profile instructions, skills, bridge metadata, and safe profile
  files.
- Preserve unknown Hermes state as portable files with import notes instead of
  modifying it blindly.

### Phase 5: Hermes importer

Deliverables:

- Generate or update Hermes profile files.
- Merge `platforms/hashi_hchat` config.
- Install `skills/hchat` when needed.
- Update bridge `agents.yaml`.
- Produce restart/reload instructions.

### Phase 6: Move semantics

Deliverables:

- `copy` mode stable.
- `move` mode disables source only after target import validation.
- rollback command for failed post-import validation.

### Phase 7: Operator UI

Deliverables:

- Workbench or Telegram menu for:
  - plan
  - export
  - import
  - enable target
  - rollback
- Clear review gates before enabling.

### Phase 8: Live smoke and documentation

Deliverables:

- HASHI to Hermes smoke.
- Hermes to HASHI smoke.
- HChat verification after import.
- Final user guide.

## Open Questions

1. Where is the canonical Hermes profile root on the operator's current
   machines?
2. Which Hermes files are durable memory versus runtime cache?
3. Should HASHI imported from Hermes default to `flex` or a specific fixed
   backend?
4. Should Hermes imported from HASHI create one profile per HASHI agent or one
   shared Hermes profile with multiple bridge `agents.yaml` entries?
5. Should `tasks.json` schedules become Hermes-native schedules, disabled notes,
   or HASHI-side remote jobs?
6. What is the acceptable secret policy for local-only moves between trusted
   machines?

## Recommended First Checkpoint

Implement Phase 1 through Phase 3 first:

1. Define the package schema.
2. Export a HASHI agent into `.hashi-hermes-agent`.
3. Import that package back into HASHI as a disabled test agent.

This validates the packaging, checksums, dry-run reports, conflict handling, and
safe import mechanics before touching Hermes profile internals.

After that, implement Hermes exporter/importer against fixture profiles from
`hashi-connect-hermes/examples/`.
