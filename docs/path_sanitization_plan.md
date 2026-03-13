# Path Portability And Home Directory Plan

## Objective
Introduce a portable path model for `bridge-u-f` that supports new installs, relocation, and future packaging without breaking any currently working agent configuration.

This plan is intentionally non-destructive:
- existing absolute paths must continue to work unchanged
- no current `agents.json` entries are rewritten as part of the initial rollout
- portability support is added first as a compatibility layer, then exposed through tools and optional migration flows

## Core Principle
Portability must be additive before it becomes a migration.

That means:
- old configs keep running exactly as they do now
- new configs may use a portable home-directory-based path scheme
- runtime code must understand both formats at the same time
- migration, if any, is explicit and opt-in

## Design Goal
`bridge-u-f` should have a first-class concept of a configurable home directory.

Conceptually:
- `bridge home` is the installation root or chosen runtime root for a deployment
- config paths may be absolute, config-relative, or home-relative
- the home directory is settable and changeable by design
- changing the home directory should not require code edits

## Non-Breaking Requirements
The following are mandatory:

1. Existing agents with absolute paths in `agents.json` must continue to start and run without any path edits.
2. Existing runtime state files must continue to load even if they contain absolute paths.
3. Existing scripts and launchers must keep working from the current repo layout.
4. No rollout step may require rewriting current user config just to keep the system operational.

## Proposed Path Model

### Supported path forms
The runtime should support all of these simultaneously:

- Absolute path
  - Example: `C:\Users\<username>\projects\bridge-u-f\workspaces\sakura\agent.md`
- Config-relative path
  - Resolved relative to the directory containing `agents.json`
  - Example: `workspaces\sakura\agent.md`
- Home-relative path
  - Resolved relative to `BRIDGE_HOME`
  - Example: `@home/workspaces/sakura/agent.md`
- Environment-expanded path
  - Example: `%APPDATA%\npm\gemini.cmd`

### Home directory contract
Introduce a single authoritative home directory resolution order:

1. Explicit CLI or launcher override
2. Environment variable such as `BRIDGE_HOME`
3. Stored installation setting
4. Fallback to the project root containing `main.py`

This makes relocation and packaging possible without forcing immediate config rewrites.

## Implementation Strategy

### Phase 0: Compatibility Layer First
Target:
- `orchestrator/config.py`
- any direct `agents.json` readers

Action:
- Add shared path resolution helpers.
- Resolve path-bearing config fields through one central function.
- Preserve absolute paths exactly as-is.
- Resolve relative paths against `config_path.parent`.
- Resolve `@home/...` paths against the current bridge home directory.
- Expand environment variables where appropriate.

This phase is the safety foundation. No existing config changes are required.

### Phase 1: Introduce Bridge Home
Target:
- config loader
- launcher scripts
- startup path plumbing

Action:
- Define a `bridge home` concept in code and startup scripts.
- Allow `BRIDGE_HOME` to override the default home root.
- Ensure `GlobalConfig.project_root` and related path consumers can distinguish:
  - code/project root
  - configurable bridge home
- Keep default behavior identical to today when no override is supplied.

Expected result:
- current installs behave exactly the same
- future installs can choose a new home directory by configuration

### Phase 2: Path-Aware Config Schema
Target:
- `agents.json`
- `tasks.json`
- future config templates

Action:
- Keep existing absolute path entries valid.
- Allow new entries to use:
  - config-relative paths
  - `@home/...` paths
  - environment-expanded executable paths
- Do not auto-rewrite existing files.
- Add comments or docs that absolute paths are supported legacy form, while home-relative paths are the recommended portable form.

Expected result:
- current agents remain untouched
- new installs gain a portable authoring style

### Phase 3: Tools For Safe Migration
Target:
- new maintenance scripts or commands

Action:
- Add a dry-run migration tool that inspects config and reports:
  - absolute paths currently in use
  - whether each can be safely expressed as config-relative or home-relative
- Add an optional rewrite tool that can generate a migrated copy of config rather than mutating the active file by default.
- Add a “show effective paths” diagnostic command so users can see how runtime resolution behaves.

Expected result:
- portability becomes operationally usable
- no one is forced to migrate blind

### Phase 4: Script And Launcher Support
Target:
- `bridge-u.bat`
- PowerShell scripts
- installer/bootstrap scripts

Action:
- Use script-local resolution for the codebase location.
- Pass or export `BRIDGE_HOME` explicitly when appropriate.
- Avoid embedding user-specific absolute paths in new scripts.
- Preserve existing launcher behavior when `BRIDGE_HOME` is not set.

Expected result:
- scripts become portable
- old entrypoints still behave as before

### Phase 5: Runtime State Compatibility
Target:
- `voice_state.json`
- similar persisted runtime/state artifacts

Action:
- Add read compatibility for both absolute and home-relative paths.
- Prefer writing portable forms only for newly created state where safe.
- Do not bulk-convert old state files in place.
- If a migration is desired later, provide a dedicated one-shot migration tool.

Expected result:
- old state remains readable
- new state can gradually become more portable

### Phase 6: Documentation Update
Target:
- `README.md`
- operational docs
- install/setup docs

Action:
- Document the new home directory concept clearly.
- Explain the three supported path styles:
  - absolute
  - config-relative
  - home-relative
- Show how to relocate an install by changing `BRIDGE_HOME`.
- Avoid rewriting runtime prompt files or workspace docs unless they are actually install documentation.

Expected result:
- documentation reflects the new model without disturbing current agents

## Required Code Changes

### 1. Central path resolver
Add a shared resolver with behavior like:
- `resolve_path(value, *, config_dir, bridge_home, allow_absolute=True)`
- preserves absolute paths
- resolves relative paths against config directory
- resolves `@home/...` against bridge home
- expands environment variables

This should be used for:
- `workspace_dir`
- `system_md`
- path-bearing runtime config values
- optional future fields such as media/model directories

### 2. Separate project root from bridge home
Today the code mostly treats project root as the operational base.

The updated model should distinguish:
- code root: where the repo/app code lives
- bridge home: where configs, workspaces, logs, media, and runtime state are rooted

Default can still be:
- code root == bridge home

But the code should no longer assume they are always identical.

### 3. Config readers outside ConfigManager
Any code reading `agents.json` directly must either:
- reuse the shared resolver, or
- only read raw values for dynamic features and never reinterpret paths independently

This matters for components like:
- orchestrator startup
- workbench API
- WhatsApp transport dynamic config reload

### 4. Portable installer/bootstrap concept
Future install flow should explicitly establish:
- where bridge home lives
- where workspaces live relative to it
- where logs/media/state live relative to it

This should be part of installation design, not an afterthought.

## Validation Criteria
The plan is successful only if all of the following hold:

1. Current `agents.json` with absolute paths still works unchanged.
2. Current launch flow from `bridge-u.bat` still works.
3. Direct `python main.py` from the repo still works.
4. New config entries using `@home/...` resolve correctly.
5. Moving bridge home to a different directory works without code edits.
6. Existing runtime state files remain readable.
7. A dry-run migration tool can explain what would change before any rewrite happens.

## Explicit Non-Goals For Initial Rollout
These should not happen in the first implementation pass:

- mass rewriting of existing `agents.json`
- in-place rewriting of runtime state files
- changing current agent `system_md` or `workspace_dir` values
- forcing all executable commands onto `PATH`
- assuming code root and bridge home must always be the same

## Recommended Rollout Order

1. Build the shared resolver and bridge home abstraction.
2. Make config loading dual-format compatible.
3. Add diagnostics and dry-run migration tooling.
4. Update launchers to support `BRIDGE_HOME`.
5. Document the new model.
6. Only then consider optional config migration for users who want portability.

## Practical Outcome
After this plan is implemented:
- current users can keep their existing absolute paths forever if they want
- new users can install `bridge-u-f` with a proper home-directory model
- future deployments can relocate cleanly
- portability improves without placing existing agent setups at risk
