# Complete debug-call reconciliation — 2026-10-13

## Main closeout — 2026-10-13

The subsequent user instruction requires HASHI3, HASHI4 and Workbench to use
their existing `main` branches for all continuing work, without a new branch
or worktree. It supersedes the initial `debug-call` construction scope below.

- [HASHI PR #79](https://github.com/Bazza1982/HASHI/pull/79) merged as
  `2028db092ca36b76bb8fd8886fa8b6a7d6f3e24a`. All five candidate CI checks passed,
  including the corrected Linux desktop gate.
- Workbench PR #6 merged as `9e382da31cbfa122905e48526b24864070bb0f26`, with
  both candidate CI checks passed. Its mobile header/menu and reboot-notice
  repairs are included.
- Both original HASHI histories and the reconciliation commits are ancestors
  of `main`. Full tracked-file comparisons passed: 2,066 blobs in each HASHI
  checkout and 1,596 in Workbench, with the recorded vendor gitlink unchanged.
- All three primary checkouts are on `main`, clean, and match their respective
  GitHub `main`. The task's local and remote `debug-call` references, the
  unauthorized camera-fix branch, and the redundant test worktree are removed.
- The closeout audit found all 41 registered worktrees across these three
  repositories clean. Older historical branches/worktrees remain; this is not
  a claim that all historical Git references have been deleted or every old
  experiment belongs in `main`. No branch-owned local or remote commits since
  October 10 remain outside `main`.
- Recent reflog-only backend speech-gap drafts have identical runtime/test
  changes in their amended main ancestors. The four former Workbench
  media/streaming/prefetch drafts are superseded by its consolidated Call
  baseline; their useful behavior and regression coverage are retained.
  Reapplying those drafts would restore obsolete startup/caption assumptions.

These are source and repository facts. No reboot, service restart, renderer
reload, or physical-media acceptance was performed during reconciliation or
closeout. Documentation-only follow-ups descend from the source identities
above without changing their tested runtime files. Local verification receipts
remain in ignored state; approved operational and physical acceptance are
separate.

## Initial scope and source facts

The initial user request required immediate reconciliation of all correct recent
HASHI3 and HASHI4 repairs, with no new branch or worktree. Integration used
HASHI3's existing `debug-call` before the later main-only closeout. This was
source integration and synchronization authority,
not permission to reboot either instance, modify Protected Core, change local
configuration, or claim physical media acceptance.

- GitHub/main baseline: `526241ee92753691fb0d021fa56776622fa9e802`.
- Original HASHI4/debug-call: `2298662e0de639c5fbbbb993d5b506847496ba63`.
- Original HASHI3 root: `d487481560b47693ceb5e3af445dbda41fe1cc1e`, already an ancestor of main.
- Original existing HASHI3/debug-call checkout: `e43ec35a44150b03f24bf527a8191b43821495a4`, already an ancestor of main.

All starting tracked/untracked worktrees involved in integration were clean.
The two endpoint trees differ at exactly 65 paths with rename detection off:
41 Python paths (19 AST differences and 22 formatting-only differences), and
24 documentation paths. Two of the 19 AST differences are unused-import/import
spelling noise. Their Protected Core manifest paths are identical.

A real two-parent merge preserves the original HASHI4 history along with main.
Every tree conflict is resolved using the inventory below; neither endpoint
wholesale replaces the other. All endpoint paths remain represented. No new
branch, worktree, copied environment, or replacement instance is created.

## Ownership and retained behavior

PAO Functions own Call opening persistence, speech ordering, terminal Phone
attempt cleanup and durable error snapshots. PCM owns relative-time guidance
and remote-memory privacy/import/ACK behavior. Existing Frontend Connector
contracts and desktop behavior stay intact. Desktop tests cover both branches'
selected-monitor geometry changes and unrelated-monitor changes.

Historical H4 source-load and physical-device receipts are retained as dated
records. Their selected-file checks never establish equality of the complete
H3/H4 trees, and their operational scopes do not grant authority today.

## Validation

Red: transplant the six missing/changed Call and Phone regression modules onto
the unchanged main implementation. The focused run produced **15 failed,
162 passed** (including seven provider-category subtest failures). It exposed
the absent opening store method/call, missing timestamp guidance, filler TTS
contention, generic provider errors, stale busy state, unsettled attempts and
missing durable snapshot error.

Green: `python -m pytest -q tests/frontend_call tests/test_live_voice.py
 tests/test_live_voice_integration.py tests/test_desktop_session.py
 tests/test_device_control_worker.py tests/test_remote_memory_consolidation.py
 tests/test_skill_manager_standard.py tests/contract/test_agent_creation.py
 --tb=short --disable-warnings` passes **369 tests and 7 subtests**, with no
failures or skips. This exercises both retained security behavior and restored
Call/Phone behavior, not live providers. The pre-existing isolated development
interpreter is used without installing dependencies into a live runtime.

The committed integration passes the core gate (`python -m pytest -q -ra`):
**814 passed, 1 skipped**; the skip is the POSIX file-mode contract on Windows.
The earlier uncommitted attempt passed 811, skipped 1 and rejected three real
qualification checks because Function sources must first be committed. No
qualification guard was bypassed. Call lint/format, protected-Core checks and
whitespace checks pass. The bounded FYI reader passes all 3 checks.

The first Linux desktop CI exposed an inherited test defect: global `os.name`
mutation made pathlib treat a Linux source path as Windows. Red: 1 failed,
87 passed. Both locked/unknown desktop health cases now simulate Windows only
inside the device-worker module, preserving native filesystem behavior and
exercising the Windows branch even on Linux. The revised Windows desktop gate
passes 87 with 1 WSL-only skip. The corrected Linux desktop CI subsequently
passed before PR #79 merged, together with all four other candidate checks.
No runtime implementation changes were made in this follow-up.

Source alignment requires both original heads to be ancestors of the
final commit and matching complete HEAD/tree IDs in HASHI3, HASHI4 and GitHub.
No lifecycle action is part of this repair; running Function adoption and
physical microphone/camera/speaker acceptance remain separate.

## Complete 65-path decisions

| Path | Resolution | Reason |
|---|---|---|
| `docs/AGENT_FYI.md` | Combine | Retain main memory safeguards and H4 Phone policy; add the reconciliation scope. |
| `docs/AGENT_FYI_HISTORY_2026-10-13.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/AGENT_FYI_HISTORY_DESKTOP_20261008.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/AGENT_FYI_HISTORY_HASHI4_PRE_FINAL_DEBUG_CALL_20261011.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/AGENT_FYI_HISTORY_MEDIA_20261009.md` | Main | Retain main's current contract, security/deployment correction, or complementary history. |
| `docs/AGENT_FYI_HISTORY_SYNC_20261009.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/HASHI_CALL_CURRENT_BEHAVIOR.md` | Main | Retain main's current contract, security/deployment correction, or complementary history. |
| `docs/HASHI_CALL_INTERACTION_2026-10-04.md` | Combine | Preserve H4 corrected route receipts and opening contract; add the current reconciliation decision. |
| `docs/HASHI_CALL_INTERACTION_HASHI4_PRE_FINAL_DEBUG_CALL_20261011.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/HASHI_MANUAL_DESKTOP.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/HASHI_PAO_SYSTEM_DESIGN.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/HERV3_UPGRADE.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/LILY_MEMORY_WIKI_INDEPENDENCE_2026-10-02.md` | Main | Retain main's current contract, security/deployment correction, or complementary history. |
| `docs/REMOTE_MEMORY_CONSOLIDATION_PRD.md` | Main | Retain main's current contract, security/deployment correction, or complementary history. |
| `docs/REMOTE_MEMORY_CONSOLIDATION_ROLLOUT.md` | Main | Retain main's current contract, security/deployment correction, or complementary history. |
| `docs/WIKI_REDESIGN_PLAN.md` | Main | Retain main's current contract, security/deployment correction, or complementary history. |
| `docs/repairs/CALL_ENTRY_COST_HASHI4_SOURCE_LOAD_20261011.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/repairs/CALL_LANGUAGE_LATENCY_20261010.md` | Main | Retain main's current contract, security/deployment correction, or complementary history. |
| `docs/repairs/DEBUG_CALL_HASHI4_FINAL_SOURCE_LOAD_20261011.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/repairs/DESKTOP_INPUT_REPAIR_20261008.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/repairs/DESKTOP_VIEW_RECOVERY_20261008.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/repairs/HASHI4_CONFIRMED_FUNCTION_SYNC_20261009.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/repairs/NIGHTLY_20261008_HASHI3.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `docs/repairs/PHONE_FAILURE_PRESENTATION_AND_LATENCY_20261012.md` | H4 | Retain the additional or corrected historical record, with its original acceptance scope. |
| `orchestrator/frontend_call/adapters.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/billing.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/config.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/context.py` | H4 | Preserve relevant spoken-turn date and elapsed-time guidance. |
| `orchestrator/frontend_call/contract.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/defaults.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/diagnostics.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/ports.py` | H4 | Preserve idempotent canonical assistant opening; remove unused json import. |
| `orchestrator/frontend_call/routes.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/service.py` | H4 | Preserve opening persistence and completion of every opening TTS segment before fillers. |
| `orchestrator/frontend_call/settings.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/spoken_context.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/transcription.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/voice_catalog.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_call/voice_previews.py` | Main | AST-equivalent implementation; retain main formatting. |
| `orchestrator/frontend_live_voice/manager.py` | H4 | Preserve terminal-attempt reconciliation and durable safe terminal reason. |
| `orchestrator/frontend_live_voice/openai_live.py` | H4 | Preserve actionable provider rejection categories without raw error text. |
| `scripts/remote_memory_consolidation.py` | Main | Keep independent privacy filtering, validated atomic promotion and bound durable ACKs. |
| `tests/contract/test_agent_creation.py` | Main | Keep environment-independent CLI fixtures. |
| `tests/frontend_call/test_call_activity_port.py` | Main | AST-equivalent implementation; retain main formatting. |
| `tests/frontend_call/test_call_contract.py` | Main | AST-equivalent implementation; retain main formatting. |
| `tests/frontend_call/test_call_correction.py` | Main | Drop only unused H4 imports; retain main formatting and behavior. |
| `tests/frontend_call/test_call_defaults.py` | Main | AST-equivalent implementation; retain main formatting. |
| `tests/frontend_call/test_call_http.py` | Main | AST-equivalent implementation; retain main formatting. |
| `tests/frontend_call/test_call_latency.py` | Main | AST-equivalent implementation; retain main formatting. |
| `tests/frontend_call/test_call_logging.py` | Main | AST-equivalent implementation; retain main formatting. |
| `tests/frontend_call/test_call_openrouter.py` | Main | AST-equivalent implementation; retain main formatting. |
| `tests/frontend_call/test_call_pcm_settings.py` | H4 | Retain H4's behavioral regression coverage. |
| `tests/frontend_call/test_call_ports.py` | H4 | Retain H4's behavioral regression coverage. |
| `tests/frontend_call/test_call_service.py` | H4 | Retain H4's behavioral regression coverage. |
| `tests/frontend_call/test_call_speech_latency.py` | Main | AST-equivalent implementation; retain main formatting. |
| `tests/frontend_call/test_call_streaming_contract.py` | Main | AST-equivalent implementation; retain main formatting. |
| `tests/frontend_call/test_call_streaming_speech.py` | H4 | Retain H4's behavioral regression coverage. |
| `tests/frontend_call/test_call_voice_preview.py` | Main | Equivalent import spelling; keep main formatting. |
| `tests/frontend_call/test_conversation_scope.py` | Main | AST-equivalent implementation; retain main formatting. |
| `tests/test_desktop_session.py` | Combine | Keep both selected-monitor position and size changes, plus H4 unrelated-monitor wake/sleep cases. |
| `tests/test_device_control_worker.py` | Combine | Retain H4 health cases; scope Windows simulation to this module so Linux pathlib stays native. |
| `tests/test_live_voice.py` | H4 | Retain H4's behavioral regression coverage. |
| `tests/test_live_voice_integration.py` | H4 | Retain H4's behavioral regression coverage. |
| `tests/test_remote_memory_consolidation.py` | Main | Keep the complete import, privacy, conflict, path and ACK regression coverage. |
| `tests/test_skill_manager_standard.py` | Main | Keep the current Antigravity skill directory contract. |
