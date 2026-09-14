# HASHI Context Cache (HCC)

## Status and ownership

Approved scope: the user's `feature-hcc` implementation request, September 2026.
Implementation baseline: `79a204a8d5df30bb80511e96ed84df323796acaf` (latest main
checked before implementation). Owner: PCM, Layer 2 Functions. No protected Core
changes, new scheduler, database, provider dependency, or permission grant.
Sandbox verification and live adoption are separate; see the verification section.

## Contract

The canonical Agent workspace `agent.md` optionally contains one `[hcc]` /
`[hcc_end]` block. Absence and an explicitly empty HCC are valid. Persona and
System remain required and non-empty; an included Memory block remains non-empty.

`/hcc` displays status and an approximate content token count. `/hcc on` and
`/hcc off` persist the current Agent's `hcc_enabled` boolean in its existing
`state.json` via `WorkspaceStateStore`. The default is **OFF**. Switching affects
the next PCM assembly, not an already executing model request. It does not
start/stop Cron, clear HCC, change the model, or change other Agents.

When ON and non-empty, **each external turn reads the full latest HCC from disk**,
including incremental and scheduled turns, independently of Memory/Memory+
flags. There is no keyword routing, model classifier, query-specific selection,
TTL eviction, hidden size cap, or live refresh on the answering path. A job's
user-selected scope, output size and schedule control the cache. The model
judges relevance. Internal tool/model steps retain that turn's snapshot; a
mid-turn refresh becomes visible at the next assembly.

The `hcc_usage` section carries trusted instructions at `local_system` authority;
`hcc` is reference data at `runtime_context` authority, not instructions or
permissions. Both have stable section ordering and are protected against
silent truncation. Non-HER over-budget requests report `pcm_hcc_capacity_exceeded`
rather than silently dropping part of the cache. HER retains its own admission
and compaction boundary. HCC does not imply that a provider has infinite capacity.

Every assembly supplies explicit removals for both keys when OFF, absent or empty.
`HERv2Adapter.prepare_fixed_turn_input()` forwards them to the existing durable
HER coordinator. Unchanged HCC need not be retransmitted: the model still sees
it in the materialized active PCM every turn. Changed content replaces that one
section. A removed section must not reappear after coordinator reconstruction.
The general HER rule **omission is not deletion** remains unchanged.

This is not historical erasure: prior assistant replies, raw canonical audit,
provider-retained conversations and backups may contain older observations.
Native provider histories are not rewritten. An older timestamp must not be
represented as a live observation. HCC audit metadata contains only flags,
size estimates and hashes; full authorized raw evidence retains its existing
separate governance. No new body dump is added to normal status or CLI receipts.

## Refreshing through existing HASHI Jobs

Manually maintained HCC may contain free-form text. The supplied refresh helper
uses flat named entries inside the single HCC block:

```text
[hcc]
[weather]
Source: <configured source reference>
Observed: <source time with timezone, or explicitly unknown>
Retrieved: <fetch time with timezone>
<complete current weather summary>
[weather_end]
[news]
<independently refreshed news summary with source/time>
[news_end]
[hcc_end]
```

Create an existing Jobs Cron record for the owning Agent, for example:

```json
{
  "id": "hcc-weather",
  "agent": "YOUR_CONFIGURED_AGENT",
  "enabled": true,
  "schedule": "0 * * * *",
  "timezone": "Australia/Sydney",
  "action": "skill:hcc-refresh",
  "args": "Refresh weather only. Use my configured region and authorized source. Include source observation time and retrieval time with timezone. Keep the summary within my chosen length. Publish using the HCC helper; preserve other entries."
}
```

This is one record to add via existing Jobs controls, not a replacement tasks
file. Change schedule to `*/10 * * * *` or `*/5 * * * *` for ten/five minutes.
Validate scheduler capability first: interval schedules require `croniter`;
its fixed-daily-time fallback does not execute arbitrary interval expressions.
No new live job is created or enabled by installing the feature.

`skills/hcc-refresh/SKILL.md` is a provider-neutral scheduled prompt Skill.
The owning Agent uses its existing authorized source tools; no location, feed,
API credential or weather provider is hard-coded. Source acquisition remains
subject to existing execution and permission boundaries. Typed scheduled jobs
use existing `isolated_per_run` scope; HCC introduces no parallel session model.

### Thin writer CLI

Run with the current permitted Python interpreter, using the skill's installed
absolute script path (shown here relative to the repository):

```bash
python skills/hcc-refresh/scripts/hcc_update.py --workspace "<agent-workspace>" inspect weather
python skills/hcc-refresh/scripts/hcc_update.py --workspace "<agent-workspace>" replace weather --expected "<digest-or-absent>" --file "<utf8-body-file>"
```

Inspect **before fetching**, then retain the returned digest. `absent` explicitly
means that the entry did not exist; it is not an unconditional overwrite flag.
The replacement body can alternatively arrive on stdin. The file contains only
the body, not `[weather]` boundaries. Standalone `[name]` / `[name_end]` lines
are reserved markers and rejected inside supplied data. Names use 1-64 ASCII letters/digits plus dot, dash or underscore, excluding
PCM block names and the `_end` suffix. The initial feature-branch preview
HTML entry markers (`<!-- hcc:name -->` / `<!-- hcc:name_end -->`) are also
recognized and replaced in place; existing snapshots are not duplicated.

`--workspace` identifies the configured Agent workspace, never a session Workzone
or cwd. `BRIDGE_WORKSPACE_DIR` may supply that same binding; when present it must
match an explicit argument. A missing binding fails rather than guessing.
The helper is a write boundary, not a retrieval tool used on the answering path.

### Publication and failure boundaries

`inspect_hcc_entry()` reads only the selected entry's revision for a job.
`replace_hcc_entry()` validates a complete non-empty result, then uses
`update_pcm_text()` for a short read/modify/write transaction. The existing
cross-process lock is exposed through `config_json.file_write_lock()` for
format-neutral use. Fetching and summarization happen **outside** the lock.

Under the lock the helper reads and validates the latest complete document,
compares the selected entry's digest, replaces only its body, revalidates the
candidate, and publishes atomically. All other PCM bytes (including CRLF) remain
unchanged. Different-entry jobs serialize without losing each other's changes;
a same-entry stale job fails with `HCCConflictError`. Do not fetch a new digest
and blindly republish an old observation after conflict.

Validation, lock timeout, pre-publication permission and rename failures retain
the prior file. Fallible permission changes occur on the temporary file before
`os.replace`. Temporary files are cleaned after a failed publication. The design
promises atomic replacement, not a new filesystem power-loss durability contract.
A source failure must not clear the old observation or advance its timestamps.

Unknown external editors do not honor the lock. A before-publication comparison
catches detected external edits, but it is not a universal compare-and-swap
against non-cooperating editors. Do not run arbitrary whole-file rewrites in
parallel with refresh jobs. Malformed canonical PCM remains a validation error;
the helper does not silently repair it or fall back to another filename.

## Changed ownership boundaries

| File | Change |
|---|---|
| `orchestrator/pcm.py` | Optional HCC parsing/rendering/conversion; exact body spans; serialized validated mutation; permission work before publication. |
| `orchestrator/hcc.py` | Opt-in flag access, trusted usage instruction, entry inspection and conflict-aware replacement. |
| `orchestrator/config_json.py` | Reuse the existing process/OS lock as a format-neutral context manager. |
| `orchestrator/bridge_memory.py` | Whole HCC each assembly; typed authority, stable keys, removals, size/hash audit; no silent truncation. |
| `adapters/her_v2.py` | Validate and forward explicit removed-section keys to the existing Fixed coordinator. |
| `orchestrator/runtime_hcc.py` | Current-Agent status/on/off, persisted state, authorization, failure reporting and localized UI. |
| `orchestrator/flexible_agent_runtime.py` | Thin native command delegate only. |
| `orchestrator/command_specs.py` and runtime locales | One command definition shared by frontends; English/Chinese catalog keys. |
| `skills/hcc-refresh/` | Standard scheduled Skill plus a thin writer CLI using the PCM-owned implementation. |

Existing writer audit: ConfigAdmin scaffolding and onboarding create new/missing
PCM; import/move uses staging; startup legacy conversion now preserves HCC.
No new whole-file live writer was introduced. Existing portable inventory already
includes `agent.md` and `state.json`, so neither transfer storage nor schema needs
an HCC-specific addition. Package extraction is tested with HCC and its preference.

## Test assertions retained and retired

Replace the **three-block-only format description** and three-block conversion
assumption with optional HCC round-trip support. Only HCC can be explicitly empty.
The former 24-function historical acceptance count is not a completeness claim
for this added state/removal/concurrency contract.

Do **not** delete the old empty-Persona test, canonical-path/symlink/UTF-8 guards,
existing Memory behavior, Fixed unchanged-delta tests, attachment revocations,
current-message authorization resets, or raw-audit retention. An old fixture with
two unchanged sections still correctly asserts two; do not mechanically change
that expected count. No existing behavioral test was removed for this feature.

Add observable tests rather than prompt-text mirrors. New suites exercise real
filesystem writes, independent OS processes, CLI subprocesses, the real command
delegate, persisted state, actual PCM/adapter/durable HER coordination, and the
existing scheduler/SkillManager path. The runtime-pipeline test confirms that
scheduled Memory isolation does not suppress HCC. Agent-package extraction
confirms HCC and the flag survive existing transfer mechanics.

## Verification record

Local isolated Linux sandbox: project CPython **3.12.13**, with dependencies
installed from `constraints/standard-py312.lock` and `.[test]` in a disposable
interpreter, not in a running HASHI Core. Source baseline as recorded above.

- HCC acceptance: **57 parameterized cases passed**, including pipeline/package
  nodes. No live model, weather, news, traffic, email or production Agent used.
- Component regression: **446 cases passed** with the command below.
- Reversible fault checks: empty-HCC rejection, Memory-gating HCC, dropped Fixed
  removals, missing revision checks, missing process lock, ignored flag writes,
  and permission failure after publication all made their focused test fail.
  All mutations were restored; the acceptance tests then passed again.
- Bare core gate was attempted, not declared green. It exposed
  `test_adapter_reconciles_old_inflight_ledger_without_resuming_it`; the identical
  failure was reproduced on an untouched baseline worktree. The full run later
  exceeded the sandbox bound at `test_default_hot_probe_does_not_seed_from_core_loaded_modules`;
  a baseline-only probe also remained in source-manifest construction at its
  smaller time bound. Neither test was deleted, reclassified or weakened.
- Protected-Core guard, executable lint and whitespace checks passed in sandbox.
  CI runner results and exact final counts belong in the branch's verification
  report, not an assertion that sandbox success proves deployment success.

Focused component command:

```bash
python -m pytest -q tests/test_hcc.py tests/test_hcc_integration.py tests/test_pcm_upgrade_contract.py tests/test_pcm_fixed_gateway.py tests/test_pcm_transfer.py tests/test_her_v2_fixed_backend.py tests/test_config_json.py tests/test_workspace_state.py tests/test_command_registry.py tests/test_ui_language.py tests/test_runtime_command_binding.py tests/test_runtime_pipeline.py tests/test_scheduler_backend_inheritance.py tests/test_scheduler_persistence.py tests/test_skill_manager_standard.py tests/test_runtime_skill_commands.py tests/test_runtime_skill_callbacks.py tests/test_agent_move_package.py
python scripts/check_protected_core_changes.py
```

## Adoption and rollback

Pull the feature branch, qualify the Functions generation using the existing
workflow, and use the normal scoped hot `/reboot` for adoption. Do not cold
restart Core. **Adopt the parser before adding HCC to live `agent.md`.** Configure
only desired sources and Jobs; opt in with `/hcc on` and verify current-Agent
status. Test V1 -> V2 refresh, next-turn visibility, then OFF in a disposable
local Agent before using personal data. Source changes, qualified artifact and
running Worker generation are separate facts; live adoption is still unverified.

For an old-version rollback, stop the associated refresh jobs, preserve a private
backup, remove the whole HCC block from live PCM and validate the remaining
Persona/System/Memory document before adopting the old Functions generation.
`/hcc off` alone does **not** make an old parser understand the new block. Never
roll back by deleting unrelated PCM or the user's other state.

## Reconciliation of the initial feature-branch preview

While this implementation was being verified, `feature-hcc` advanced independently
to `23aebe28e8dc82d502a4af3a6218abed70c705a6` (343 insertions, 12 HCC-only files).
That commit is retained as an ancestor, not force-reset. Its HCC changes were
reviewed and completed by this implementation; unrelated files remain unchanged.
The `[hcc]` format, `hcc_enabled` preference, boolean setter return, `/hcc status`
alias and existing comment-style entry data are preserved.

Preview-only assertions superseded deliberately: missing HCC is represented by
`None` rather than an empty string so absence round-trips; trusted HCC guidance
has its own system-authority section, so revocation removes both section keys.
The internal writer now requires `expected_entry_sha256` from `inspect` (`sha256`
field); callers must not use the preview's optional `expected_digest` blind-write
path. The CLI likewise requires `--expected <digest-or-absent>` and explicit
Agent binding. Update any manually created preview cron invocation accordingly.
The replacement tests retain all eight preview behavioral contracts (optional/empty,
state persistence, independent entries, stale rejection, failure preservation,
per-turn freshness, off and empty revocation) and strengthen their observable
boundaries; no pre-HCC main-branch behavioral test was removed.
