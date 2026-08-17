# HER Bug Journal

Status: active permanent regression record
Applies to: HASHI ↔ HER ↔ provider integration
Test plan: [HER_COMPREHENSIVE_TEST_PLAN.md](HER_COMPREHENSIVE_TEST_PLAN.md)

## Rules

1. Record a defect before repairing it.
2. Never delete or renumber an entry. Reopen the original entry when a symptom recurs.
3. One entry describes one root-cause class. Split unrelated causes even when one user
   run exposed them together.
4. Redact credentials, private prompts, and unnecessary reasoning from evidence.
5. `Fixed` means code changed; `Verified` means the regression failed on the known-bad
   build, passed on the fixed build, and the required live retest passed.
6. Every confirmed defect must add an automated regression test. A manual-only fix
   remains `Fixed, verification pending`.
7. Link the exact package SHA, HASHI commit, HER source commit, cell ID, run ID, and
   evidence bundle. Branch names alone are not sufficient provenance.
8. Model-quality failures are not logged as HER bugs unless HER misreports, corrupts,
   duplicates, loses, or fails to terminate the run correctly.

## Status and severity

Statuses: `New`, `Reproduced`, `Root caused`, `Fixed`, `Verified`, `Reopened`, `Deferred`,
`Not a HER bug`.

| Severity | Meaning |
| --- | --- |
| `P0` | secret leak, unsafe write, cross-agent/session contamination, or destructive duplicate side effect |
| `P1` | lost task/context, corrupt protocol, missing terminal event, false success/error, runaway loop, or unusable output |
| `P2` | degraded display, wrong accounting, recoverable duplicate/missing progress, or route-specific reliability defect |
| `P3` | minor diagnostic, documentation, or evidence-quality defect without behavioral loss |

## Index

| ID | Status | Severity | Summary | Regression anchor |
| --- | --- | --- | --- | --- |
| `HER-20260811-001` | Verified | P1 | reasoning fragments lost exact whitespace and joined/split words | `test_thinking_deltas_preserve_exact_provider_spacing` |
| `HER-20260811-002` | Verified | P1 | bare continuation after `/stop` could lose the original flex-mode task | `test_build_turn_prompt_binds_bare_continue_to_persisted_stopped_task` |
| `HER-20260811-003` | Fixed — live verification pending | P1 | interactive permission text could corrupt stream JSONL | `stream_json_permission_prompt_preserves_the_jsonl_contract` |
| `HER-20260811-004` | Fixed — live verification pending | P1 | reasoning-only assistant history could make DeepSeek visible finalization fail without `run_finished` | `deepseek_v4_drops_reasoning_only_assistant_history` |
| `HER-20260811-005` | Fixed — live verification pending | P1 | parser errors could expose the original prompt/command while diagnosing a missing final event | `test_stream_json_parser_missing_final_is_safe_and_fail_closed` |
| `HER-20260811-006` | Fixed — live verification pending | P1 | HER Tool Gateway could not start because its JSON Schema dependency was undeclared and absent | `test_lab_self_test_proves_isolation_and_cleanup_guard` |
| `HER-20260811-007` | Fixed — live verification pending | P1 | stream-json provider failures exited without the required `run_finished` event | `stream_json_provider_error_emits_terminal_run_finished` |
| `HER-20260811-008` | Verified | P2 | the required Rust 1.95 all-target Clippy gate failed on accumulated lint drift | `cargo clippy --workspace --all-targets -- -D warnings` |
| `HER-20260811-009` | Verified | P3 | the new sequential-step evidence check read a nonexistent receipt field and reported a false failure | `test_packaged_candidate_sequential_steps_are_exactly_once` |
| `HER-20260811-010` | Fixed — live verification pending | P2 | malformed or truncated provider streams terminated safely but were classified as unknown without the last safe event | `stream_json_malformed_provider_stream_names_protocol_failure` |
| `HER-20260811-011` | Fixed — live verification pending | P2 | provider HTTP 403 was classified as a generic HTTP error instead of an authentication failure | `classify_error_kind_returns_correct_discriminants` |
| `HER-20260811-012` | Verified | P3 | the delayed-response fixture printed a BrokenPipe traceback after the expected client timeout | `test_scripted_provider_tolerates_timed_out_client_disconnect` |
| `HER-20260811-013` | Verified | P3 | the native-ceiling checker confused provider iterations with executed tool iterations | `test_packaged_candidate_hits_native_iteration_ceiling_exactly` |
| `HER-20260812-014` | Verified | P1 | configured stdio MCP child logs leaked into HER structured CLI stderr | `allowed_tools_json_errors_isolate_configured_mcp_stderr` |
| `HER-20260812-015` | Fixed in source — rebuild/live verification pending | P1 | initial planning/validation deviations could abort valid tasks before primary execution | `medium_noncanonical_planned_tool_prose_reports_and_executes_real_tools`; `initial_planner_provider_error_is_visible_but_does_not_block_agent` |
| `HER-20260812-016` | Verified | P2 | request activity timestamps could move backwards when wall-clock time regressed | `test_request_activity_clamps_regressing_timestamps_to_sequence_order` |
| `HER-20260812-017` | Fixed — blast-radius verification pending | P1 | AskUserQuestion terminal UI corrupted structured JSONL and hid its correlated tool_end | `stream_json_ask_user_question_preserves_correlated_tool_events` |
| `HER-20260812-018` | Verified | P1 | controller nudge misclassified an in-progress task's next packet as a pending-task start and livelocked | `test_in_progress_packet_continuation_cannot_require_new_start_authority` |
| `HER-20260812-019` | Verified | P1 | live harness accepted the pre-restart runtime as the `/reboot min` completion receipt and lost the first request | `test_restart_receipt_rejects_pre_restart_online_idle_runtime` |
| `HER-20260813-020` | Fixed — live verification pending | P1 | MCP image results were flattened into text before the provider could see them | `test_packaged_her_bridges_media_read_image_into_provider_vision_input` |
| `HER-20260813-021` | Fixed — live verification pending | P1 | legacy screenshot results reached HER as strings instead of validated MCP image content | `test_gateway_bridges_legacy_browser_screenshot_string_to_image` |
| `HER-20260813-022` | Fixed — live verification pending | P1 | Flex/composed full-context HER turns can also resume the persisted HER session | `test_her_full_context_turn_never_resumes_or_checkpoints_session` |
| `HER-20260813-023` | Fixed — live verification pending | P2 | runtime and adapter HER Habit pipelines can both process one foreground run | `test_her_adapter_declares_habit_pipeline_ownership`; `test_runtime_intake_ineligibility_disables_adapter_habit_pipeline` |
| `HER-20260813-024` | Fixed — live verification pending | P1 | post-multimedia adapter runner rejected Meditation isolation overrides before inference | `test_her_task_runner_applies_meditation_safety_overrides` |
| `HER-20260813-025` | Verified | P2 | HER Debug Lab failed in a clean clone without machine-local Ajiao state | `test_optional_operator_baseline_is_clone_portable_and_content_free`; `tests/test_her_debug_lab.py` |
| `HER-20260813-026` | Fixed — live verification pending | P1 | MAX+ critic gates could repeatedly delay and then suppress otherwise usable completed work | `max_effort_returns_agent_owned_final_after_execution_review_exhaustion`; `max_plus_trivial_plan_skips_independent_review_gates` |
| `HER-20260813-027` | Fixed — live verification pending | P1 | Habit Meditation model work occupied the foreground execution queue and process slot | `test_habit_meditation_model_work_does_not_block_foreground_task`; `test_habit_meditation_uses_low_effort_tool_free_snapshot` |
| `HER-20260813-028` | Fixed — live verification pending | P1 | max-iteration handling could replace a usable primary-agent final answer with a mechanical incomplete report | `test_claw_incomplete_max_iterations_preserves_normal_final`; `test_claw_incomplete_dangling_tool_markup_uses_deterministic_report` |
| `HER-20260814-029` | Fixed — live verification pending | P2 | HIGH/XHIGH shared checkpoints reviewed stale task frames without current tool evidence and repeated the same divergence review | `high_effort_reserves_turns_for_review_and_validation`; `high_effort_deduplicates_repeated_unplanned_tool_reviews` |
| `HER-20260814-030` | Verified | P1 | fixed-mode planner saw only the incremental current prompt, not the persistent session view used by the primary agent | `fixed_session_planner_sees_resumed_options_at_every_planning_effort`; `medium_plus_plans_replans_and_reports_non_blocking_tool_divergence` |
| `HER-20260814-031` | Verified | P1 | long fixed sessions could make the planner answer as the conversational agent instead of returning TaskFrame JSON | `planner_request_preserves_session_prefix_and_appends_nonpersistent_control`; fixed-session direct-response effort matrix |
| `HER-20260815-032` | Fixed — live verification pending | P0 | isolated scheduler choices and CONTINUE checkpoints were absent from the primary conversation context | `test_build_turn_prompt_prefers_newer_scheduler_receipt_over_stopped_task`; `test_her_isolated_continuation_resumes_exact_checkpoint_without_replacing_primary` |
| `HER-20260816-033` | Fixed in source — rebuild/live verification pending | P1 | TaskFrame again saw only `A` while the primary executor saw and completed the previous option | `task_checkpoint_receives_immediate_previous_dialogue_context`; `tests/test_runtime_turn_context.py` |
| `HER-20260816-034` | Verified | P1 | offline `--status` constructed a second rebuild manager and falsely failed an active build as a kernel restart | `test_offline_status_is_strictly_read_only_during_active_build`; `test_manager_ownership_lock_excludes_a_second_process` |
| `HER-20260817-035` | Deployed to Arale — live behavior verification pending | P1 | source-integrated `.22` lost native direct-response termination, so completed TaskFrame answers entered primary execution and MAX/MAX+ review loops | `direct_response_finishes_after_one_planning_call_at_every_native_effort`; `invalid_direct_response_falls_back_to_primary_execution`; `test_claw_direct_response_acknowledgement_is_final_only` |
| `HER-20260817-036` | Deployed to Arale — live behavior verification pending | P2 | pending model-authored Persona commentary shared technical cadence and vanished silently at turn finalization | `test_claw_cadence_technical_activity_does_not_delay_persona_commentary`; `test_claw_cadence_finish_supersedes_only_latest_pending_commentary`; `test_her_effort_commentary_matrix_reaches_transport_receipt` |
| `HER-20260817-037` | Verified; delivery follow-up tracked by `HER-20260817-039` | P2 | primary tool-turn Persona text remained internal `assistant_delta`, so the repaired commentary cadence had no native events to deliver | `complete_tool_bound_text_emits_one_commentary_before_tool_execution`; `test_claw_tool_bound_assistant_commentary_is_user_visible_primary_model_text`; effort transport matrix |
| `HER-20260817-038` | Deployed to Arale — live behavior verification pending | P1 | StreamLake's 504 inside an established SSE stream was hard-coded non-retryable and aborted a resumable Arale turn | `stream_message_marks_embedded_gateway_timeout_as_retryable`; `provider_stream_retries_embedded_504_once_and_returns_only_complete_attempt` |
| `HER-20260817-039` | Deployed to Arale — live behavior verification pending | P1 | a legacy effort gate silently demoted every native Persona commentary at `low` and `medium` to internal despite `/commentary on` | `test_medium_adapter_delivers_native_tool_turn_commentary`; corrected low-through-ultra transport matrix |

## Historical entries

### HER-20260817-039 — low/medium effort silently suppressed Persona commentary

- **Status:** Deployed to Arale — live behavior verification pending
- **Severity:** P1
- **Discovered:** 2026-08-17 AEST in Arale request `req-0001`, a 835-second
  `medium` turn with 17 successful tool calls.
- **Expected:** execution effort changes planning/execution depth, not message
  ownership. When `/commentary on`, any native model-authored Persona update at
  `low` through `ultra` follows the common commentary cadence to Telegram.
- **Actual:** native HER generated at least thirteen complete
  `assistant_commentary` events. The adapter then converted every one to
  `delivery_class=internal` with
  `suppressed_reason=effort_progress_disabled`; Telegram therefore received
  only the initial acknowledgement and final answer.
- **Root cause:** `HER_COMMENTARY_EFFORTS` retained an obsolete
  `{high, xhigh, max, max+, ultra}` presentation allowlist. The previous
  low-through-ultra test encoded that obsolete policy as its expected output,
  so it proved routing only at allowed efforts and falsely passed the overall
  contract.
- **Repair:** remove effort from the presentation decision; always run the
  native commentary cadence when a stream callback exists; leave `/commentary`
  as the sole optional user-visibility gate. Low may have no TaskFrame
  acknowledgement, but a native tool-turn commentary remains deliverable.
- **Regression tests:** a real fake-CLI `medium` adapter stream proving native
  commentary crosses the adapter boundary, plus corrected `low` through
  `ultra` events reaching Telegram transport receipts.
- **Secrets/redaction checked:** yes; the forensic audit contained task text,
  but no task payload or private path has been copied into tests.
- **Managed adoption:** Python-only commit `93ed8143`; a targeted `/reboot min`
  was requested only after Arale's foreground task and Habit Meditation had
  both exited. Arale stopped at 18:23:00, reloaded the current adapter/runtime
  pipeline, validated the existing 69-tool HER gateway, and returned online
  with Telegram at 18:23:05; the complete hot-reload postcheck finished at
  18:23:13 AEST. The unchanged Rust candidate remains commit `99dc906d`, as
  expected for a presentation-layer-only repair.
- **Remaining risk:** the next real `medium` multi-tool turn must prove that at
  least one generated commentary receives a Telegram transport receipt instead
  of an effort suppression record.

### HER-20260817-038 — embedded SSE 504 aborted a resumable turn

- **Status:** Deployed to Arale — live behavior verification pending
- **Severity:** P1
- **Discovered:** 2026-08-17 AEST in Arale request `req-0002`, fixed-session
  HER, `deepseek/deepseek-v4-pro` through StreamLake.
- **Expected:** a transient gateway/idle timeout received after an SSE handshake
  is classified with the same retryability as an HTTP 504. HER may replay one
  incomplete model attempt, while tools execute only from a complete response.
- **Actual:** the provider returned `code=504`, `error_type=timeout`, and
  `Upstream idle timeout exceeded` inside a `data:` frame after the preceding
  tool result. `parse_sse_frame` constructed `ApiError::Api` with
  `retryable=false`; the CLI converted it to an opaque `RuntimeError` and
  immediately ended the task as a backend error after 618.63 seconds.
- **User-visible impact:** verified work and session state were retained, but
  Arale could not wrap up or continue the current turn after one transient
  provider timeout.
- **Root cause:** SSE-embedded errors bypassed normal HTTP status
  classification, and the CLI erased structured retryability before its
  post-handshake continuation policy could inspect it.
- **Repair:** extract numeric or string status, provider timeout metadata, and
  request ID from embedded errors; preserve structured failure stage and
  retryability through stream consumption; replay one incomplete retryable
  stream under the original deadline; emit technical `provider_retry`
  telemetry; never retry handshake-exhausted or local failures at this layer.
- **Exactly-once boundary:** the conversation runtime receives events only
  after one provider attempt completes, and no tool is executed before that
  return. A discarded incomplete attempt therefore cannot replay a tool side
  effect.
- **Regression tests:** exact StreamLake-shaped SSE 504 classification plus a
  real local two-response SSE server proving one retry and a complete recovered
  result.
- **Secrets/redaction checked:** yes; tests and documentation use synthetic
  credentials and provider payloads only.
- **Managed adoption:** commit `99dc906d`; job
  `rebuild-20260817-075341-15872f6e`; candidate
  `dev-f08531a74d37c890-d79f38effe44`; binary SHA-256
  `d79f38effe44bfa417ce81749d528711c73b7baf6dbe5f6b1f7dedbb7493ef9b`.
  Candidate version/CLI/stream-json verification passed, Arale selected the
  exact immutable binary, targeted hot restart completed, the 69-tool gateway
  validated, Telegram reconnected, and the managed postcheck passed.
- **Remaining risk:** one live Arale provider-timeout recovery retest remains.

### HER-20260817-037 — native tool-turn commentary was never produced

- **Status:** Verified; delivery follow-up tracked by `HER-20260817-039`
- **Severity:** P2
- **Recurrence of:** distinct producer-side defect exposed after
  `HER-20260817-036`; the cadence/terminal repair itself remained loaded.
- **Discovered:** 2026-08-17 AEST while tracing Arale's first live test after the
  commentary deployment.
- **Expected:** Persona text authored by the primary model before a tool call is
  assembled into one commentary event, then follows the independent commentary
  cadence to Telegram. Raw token deltas stay internal and final answers stay on
  the final lane.
- **Actual:** ten tool turns contained clear Persona preambles, but native HER
  emitted them only as `assistant_delta`; the adapter correctly classifies that
  type as internal. One replan also authored `task_commentary`, but harmless
  aliases resolved to the same planned capability and caused the whole replan
  to be rejected, discarding its optional commentary.
- **User-visible impact:** only the initial acknowledgement appeared during a
  long Arale task even though `/commentary on` and the repaired cadence were
  active.
- **Root cause:** previous end-to-end tests injected synthetic
  `task_commentary` and proved routing/cadence, but never proved that a real
  primary tool turn produced a commentary event. TaskFrame validation also
  treated duplicate canonical aliases as a fatal planning error.
- **Repair:** after a provider turn completes successfully, assemble its visible
  text and emit one `assistant_commentary` immediately before ToolStart when a
  non-interactive tool will run; exclude final and AskUserQuestion turns;
  preserve raw deltas as internal; deduplicate same-capability TaskFrame aliases
  in first-seen order.
- **Regression tests:** native producer ordering and non-duplication, native
  JSON serialization, adapter ownership/provenance, and low-through-ultra
  transport receipt coverage using the new native event type.
- **Secrets/redaction checked:** yes; no private model text is stored in tests.
- **Managed adoption:** the same commit `99dc906d`, rebuild job
  `rebuild-20260817-075341-15872f6e`, and immutable candidate
  `dev-f08531a74d37c890-d79f38effe44` passed candidate verification, targeted
  Arale restart, exact binary selection, Tool Gateway validation, Telegram
  reconnection, and managed postcheck.
- **Remaining risk:** one live multi-tool Arale retest remains.
  That retest produced native `assistant-commentary:*` audit records, verifying
  this producer fix; their later presentation suppression is the distinct
  adapter defect tracked by `HER-20260817-039`.

### HER-20260817-036 — pending Persona commentary vanished at finalization

- **Status:** Deployed to Arale — live behavior verification pending
- **Severity:** P2
- **Discovered:** 2026-08-17 AEST while tracing a MAX+ Arale turn whose native
  event ledger contained a model-authored `task_commentary` but whose Telegram
  transcript contained no commentary delivery.
- **Expected:** user-facing Persona commentary follows its own effort-independent
  cadence and `/commentary` switch. Technical updates belong to `/verbose`,
  provider reasoning belongs to `/think`, and activity in either channel cannot
  reset the Persona clock. Every generated commentary event is eventually
  delivered once or receives an explicit audited suppression outcome.
- **Actual:** the adapter placed material commentary into a pending slot, but
  technical events updated the same visible/activity cadence. At normal turn
  finalization the adapter closed and cancelled the cadence task without
  resolving the slot, so the generated Persona update reached neither Telegram
  nor the message audit.
- **User-visible impact:** Arale could work for several minutes without any of
  her generated Persona progress updates appearing, even with `/commentary on`.
  The missing audit trail also made the silence look like a model-generation
  failure rather than a presentation-layer regression.
- **Root cause:** Persona commentary and neutral technical leases shared one
  clock, while the adapter's terminal path treated the pending commentary slot
  as disposable task-local state.
- **Repair:** give commentary and technical leases independent clocks; retain
  only the newest material pending commentary; explicitly audit displaced
  updates; and resolve the one terminal remainder as `superseded_by_final`
  without sending a last-second burst. Cancellation receives its own explicit
  suppression reason. The runtime audit accepts these internally owned terminal
  outcomes without routing them to Telegram.
- **Regression tests:** cadence isolation under technical activity; one-slot
  coalescing; terminal supersession; real adapter stream finalization; and a
  `low` through `ultra` native-event-to-transport-receipt matrix.
- **Managed adoption:** job `rebuild-20260817-070110-3c9bf6f7` reused candidate
  `dev-acbe61534cf4a5eb-20d2d521fa40`, completed Arale's targeted hot restart,
  verified the current HER adapter/runtime-pipeline reload contract, restored
  Telegram connectivity, and passed the managed postcheck on 2026-08-17 AEST.
  This proves deployment and transport health; a real long-running turn is
  still required before changing the issue status to `Verified`.

### HER-20260817-035 — TaskFrame direct answers no longer terminated the turn

- **Status:** Deployed to Arale — live behavior verification pending
- **Severity:** P1
- **Discovered:** 2026-08-17 AEST during a MAX+ `/handoff` latency diagnosis
- **Expected:** when TaskFrame determines that its Persona-authored
  acknowledgement completely answers a no-tool, no-state-change turn, HER
  persists and delivers that answer once with `completed/end_turn`, without a
  second primary generation or any assurance review. Non-direct work continues
  through the primary Agent under the TaskFrame goal and boundaries.
- **Actual:** the source-integrated `.22` `TaskFrame` had no `direct_response`
  field and `run_turn_observed` unconditionally entered the primary execution
  loop. The Python adapter still recognized synthetic `direct_response=true`
  events, but native HER could never produce one. At MAX+ this amplified a
  completed acknowledgement into a redundant primary answer and repeated
  verification revisions.
- **User-visible impact:** a simple handoff acknowledgement took several
  minutes, consumed repeated model/reviewer calls, and risked a worse duplicate
  final answer even though planning had already produced the complete response.
- **Root cause:** the supervised source import from the certified `.22` line did
  not preserve the native direct-response profile and early-return path that
  remained documented and present in the `.19` binary. Adapter-only synthetic
  coverage proved final-lane routing but not native production or termination,
  so the producer/consumer contract split was not detected.
- **Repair:** restore an explicit native `TaskFrame.direct_response`; require a
  complete Persona answer with no planned actions, tools, assurance, completed
  execution claims, failures, remaining work, or next action; bypass independent
  planning and completion gates; persist the answer in the normal session; emit
  the initial TaskPlan for final-lane classification; and return zero-iteration
  `completed/end_turn`. Invalid direct frames retain a visible planning error and
  fall through the existing authorization-preserving primary path.
- **Regression tests:** native effort matrix for `medium`, `high`, `xhigh`,
  `max`, and `max+`; invalid-direct non-blocking fallback; non-direct TaskFrame
  handoff to the primary Agent; existing adapter final-lane delivery test.
- **Managed rebuild:** job `rebuild-20260817-061803-bfc0b34d` built and verified
  immutable candidate `dev-acbe61534cf4a5eb-20d2d521fa40` with binary SHA-256
  `20d2d521fa4065b53a5e7615f8da920bb503e7b7494b7a7a1b2b68d18df14d9f`.
  Adoption was safely deferred because Arale remained busy with an active user
  task throughout the 120-second idle window; the previously selected HER was
  left unchanged. A later `/rebuild` while Arale is idle can reuse this verified
  candidate and complete targeted adoption without recompilation.
- **Managed adoption:** follow-up job `rebuild-20260817-070110-3c9bf6f7`
  reused that immutable candidate, completed Arale's targeted hot restart, and
  passed the managed postcheck on 2026-08-17 AEST. A real direct-response turn
  remains the final user-visible verification step.

### HER-20260816-034 — rebuild status observer falsely became a restart owner

- **Status:** Verified
- **Severity:** P1
- **Discovered:** 2026-08-16 AEST during post-commit offline verification
- **Expected:** every status path is side-effect-free; one live kernel-owned
  manager exclusively owns recovery and mutation for its rebuild state root.
- **Actual:** `scripts/her_rebuild_dev.py --status` constructed a new
  `HERRebuildManager`. Manager construction automatically ran
  `recover_nonterminal()`, so the observer rewrote another process's active
  `building` job to `failed` with `kernel_restarted_during_rebuild`.
- **User-visible impact:** an operator checking status could receive a false
  failure, terminate the authoritative job record and force a redundant rebuild.
  The selected HER and certified package were not changed.
- **Root cause:** startup recovery used manager construction itself as evidence
  of a cold kernel restart. There was no process-lifetime coordinator ownership
  lock, and the offline status helper unnecessarily constructed the mutating
  coordinator before reading the job store.
- **Fix:** one OS-held `HERManagerLock` now owns each rebuild state root for the
  coordinator's process lifetime. A second live process is rejected before
  recovery. After the real owner exits, the OS releases the lock and the next
  owner retains the intended interrupted-job recovery behavior. Offline
  `--status` now reads `HERRebuildJobStore` directly with directory creation
  disabled; it never constructs a manager or writes state. During `/reboot`,
  the stable Manager is upgraded in place to the reloaded class: its jobs,
  candidates and active tasks remain intact while an older instance acquires
  the new ownership lock. No full process restart is required.
- **Regression tests:** byte-identical active job across concurrent status;
  absent-state status creates no directory; second manager rejected in-process;
  second Python process rejected by the OS lock; replacement manager after
  owner exit still performs cold-start recovery; existing build, verification,
  adoption, rollback and notification tests retained; manager-registry hot
  migration preserves the same instance and an active task by identity.
- **Automated verification:** rebuild/registry/reboot-focused suite passed
  `61/61`; complete HASHI suite passed `2302 passed, 2 skipped` after the final
  hot-migration implementation. Ruff, Python compilation and
  `git diff --check` passed for the changed surfaces.
- **Platform boundary:** Linux/WSL OS-lock behavior was exercised locally. The
  same helper uses the existing Windows `msvcrt` byte-range lock path already
  covered by the cross-platform rebuild-lock contract; Windows CI remains part
  of the release matrix.
- **Secrets/redaction checked:** yes; lock metadata contains only schema,
  manager ID, PID and timestamps.
- **Recurrence count:** 0

### HER-20260816-033 — TaskFrame and executor again resolved different turns

- **Status:** Fixed in source — rebuild/live verification pending
- **Severity:** P1
- **Recurrence of:** `HER-20260814-030`
- **Discovered:** 2026-08-16 AEST during a deliberate HER model-switch test
- **Provider / model / mode / effort:** direct DeepSeek; persistent HER session;
  `deepseek-v4-pro` changed to `deepseek-v4-flash`; `xhigh`
- **Expected:** one user turn has one enqueue-time reply target and one resolved
  goal shared by TaskFrame and primary execution.
- **Actual:** TaskFrame received only current `A`, reported no clear task and
  planned no tools. The primary executor received the persistent session,
  resolved `A` as the complete Wiki pipeline, and completed it successfully.
- **User-visible impact:** the Agent first appeared to lose continuity while an
  internal second line still understood and completed the task. Planning and
  execution audit records contradicted each other.
- **Root cause:** the integrated Rust source at `6c7fd961` reconstructed every
  task checkpoint from `active_goal` alone and explicitly delegated semantic
  resolution to the primary executor. That superseded the previously verified
  `.18/.19` session-sharing fix. HASHI also did not provide TaskFrame with its
  transport-owned visible delivery order or model/effort transition.
- **Fix:** HASHI now freezes a bounded `hashi-turn-context-v1` envelope when a
  direct request enters the queue. It contains the latest final dialogue
  actually delivered before enqueue, the frozen cross-session target when
  present, and current/previous model and effort. Native HER supplies the
  bounded previous user/assistant pair plus current request to TaskFrame and
  supplies the same canonical metadata to execution. Cold HASHI restarts fall
  back to the immediate completed pair in the persistent HER session; a known
  pending earlier direct turn cannot be captured retroactively. Obvious
  unresolved short-choice frames stop before tools, including at Max/Max+.
- **Regression tests:** `tests/test_runtime_turn_context.py`;
  `task_checkpoint_receives_immediate_previous_dialogue_context`;
  `hashi_enqueue_context_overrides_newer_session_history_for_referent_resolution`;
  `hashi_cold_start_context_uses_persistent_session_fallback`;
  `short_choice_frame_must_resolve_against_supplied_previous_dialogue`.
- **Automated verification:** complete HASHI Python suite passed with `2297
  passed, 2 skipped`; all Rust workspace/all-target tests passed; runtime
  library Clippy with warnings denied, Ruff, Python compilation and
  `git diff --check` passed. Workspace-wide all-target Clippy still reaches
  pre-existing diagnostics outside this change and is not claimed as a clean
  certification result. Offline `/rebuild` and live provider evidence remain
  pending.
- **Required live retest:** on an idle test Agent, `/rebuild`; establish one
  persistent A/B turn; deliberately switch Pro to Flash and effort; send only
  `A`; prove the first TaskFrame, acknowledgement, tool plan and final execution
  resolve the same option. Repeat with a cron result delivered both before and
  after enqueue.
- **Remaining risk:** the current source fix is not present in certified `.22`;
  production packaging and cross-platform certification remain pending.
- **Secrets/redaction checked:** yes; the envelope is bounded, process-local on
  the HASHI side, and the documented examples contain no private task content or
  credentials.
- **Recurrence count:** 1

### HER-20260815-032 — isolated scheduler replies were absent from primary context

- **Status:** Fixed — live verification pending
- **Severity:** P0
- **Expected:** every scheduler-owned turn leaves a durable, no-op receipt that is
  injected into the next primary turn in fixed and flex modes. A short reply such as
  `CONTINUE`, `A`, or `comment A, C` binds to the newest delivered unresolved prompt,
  and an isolated HER checkpoint resumes without replacing the primary session.
- **Actual:** scheduler turns intentionally ran in `isolated_per_run`, but their
  completion report existed only in Telegram and the isolated HER session. The main
  session could therefore resolve the user's short reply against an older question or
  A/B/C choice from its own history.
- **User-visible impact:** the agent could continue the wrong task or apply a selected
  option to the wrong proposal, including an unintended externally visible action.
- **Root cause:** scheduler session isolation was introduced without a corresponding
  cross-session completion receipt, context injection path, or deterministic reply
  binding. Existing continuation recovery covered `/stop` only.
- **Fix:** HASHI now journals bounded scheduler/isolated completion receipts atomically,
  injects them as read-only context in both modes, tracks pending continuation/choice/
  question ownership, and supersedes stale prompts when a newer scheduler or primary
  interaction is delivered. Matching HER replies resume the exact isolated session;
  incompatible-model fallbacks remain isolated rather than entering the primary
  session. Failed continuation attempts retain the last retryable checkpoint.
- **Known-bad HASHI checkpoint:** `cdf1036d`.
- **Fix commits:** `d99dc1ad`, `98009ea1` (a primary question ending in an
  emoji now also retires an older scheduler prompt).
- **Regression tests:** `tests/test_runtime_cross_session.py`; fixed/flex pipeline
  precedence, foreground/background delivery persistence, and exact HER checkpoint
  resume tests in the affected runtime and adapter suites.
- **Automated verification:** targeted affected suites passed `246/246`; the complete
  HASHI test directory reported `2146 passed, 6 failed, 3 skipped`. The six failures
  match the pre-existing five packaged-HER debug-lab contracts and one legacy media
  payload assertion. Ruff, Python compilation, diff checks, and the 8059-line active
  runtime architecture ratchet passed (`8058` lines).
- **Required live retest:** reboot one test agent, run an isolated scheduled turn that
  ends with `CONTINUE`, resume it from the primary chat, then repeat with overlapping
  primary and scheduler A/B/C prompts and prove the newest delivered prompt owns the
  reply without changing the primary HER session ID.
- **Activation check:** Momo loaded both fixes through `/reboot min` at 17:56 AEST,
  restored its existing primary HER checkpoint, started its queue processor, and
  returned online with Telegram connected. The 17:00 scheduler result was backfilled
  as one mode-`0600` receipt; the newer 17:02 primary question correctly made it
  inactive while retaining it for context injection.
- **Remaining risk:** no fresh post-fix scheduled turn and real short-reply canary has
  run yet; live provider and Telegram binding evidence is pending.
- **Secrets/redaction checked:** yes; the entry contains no private task text or
  credentials.
- **Recurrence count:** 0

### HER-20260814-031 — long fixed sessions displaced the planner protocol

- **Status:** Verified
- **Severity:** P1
- **Expected:** the planner sees the same effective fixed-session context as the
  primary agent and returns one schema-valid TaskFrame without tools or side effects,
  leaving enough time for the user-visible turn to complete inside Workbench's
  280-second wait boundary.
- **Actual:** the planner correctly resolved the current `A` against the preceding
  choices but replied as the conversational agent: first as prose, then as tool
  markup, then as prose again. Three rejected frames consumed roughly 501 seconds,
  Workbench returned HTTP 504, and conservative fallback let the primary agent resume
  an unrelated historical task from the same production session.
- **User-visible impact:** a continuity answer timed out while backend work continued;
  fallback could perform stale in-scope-looking work after the caller had already
  received 504.
- **Root cause:** the full persistent conversation correctly preceded the planner's
  system instructions, but a very long chat made the provider continue the normal
  assistant role instead of obeying the earlier TaskFrame output contract. The first
  live canary also violated its own fresh-session requirement by reusing a production
  session, so it did not isolate the protocol regression.
- **Fix:** retain the persistent session as an exact request prefix, append a final
  request-local task-control envelope that explicitly forbids prose, tools, and task
  execution, and request provider-native JSON output for TaskFrame and other
  schema-bound runtime calls. The envelope is not persisted and grants no user
  authority. The corrected canary explicitly requires restatement only, with no tools
  or writes.
- **HER source:** `79be4613e37d03781713253a04aa64aedf3f1902`.
- **Fixed HER:** `0.1.0-hashi.19` Linux x86-64 / SHA-256
  `3cd9dbee8617b7fb23a7df7893cc2a3bd17a70b0d0c3fa5945f41ab88f674538`;
  Windows x86-64 / SHA-256
  `f483723f249e89b08eec2f091553e1dc2e207dbe9565a819a41c264b9e3f00f5`.
- **HASHI package commit:** `0d27419a8916b9ca15bd83743eff729e634d06c7`.
- **Regression tests:** the planner request preserves all normalized session messages
  as an exact prefix, appends exactly one non-persistent control envelope, maps JSON
  mode only onto schema-bound calls, and verifies direct responses at `medium`,
  `high`, `xhigh`, `max`, and `max+`; `low` remains the planning-disabled negative
  control.
- **Automated verification:** format check, Rust runtime `655/655`, two conformance
  tests, 12 integration tests, full workspace/all-target tests, and workspace/all-target
  Clippy with warnings denied passed from the pinned clean source. The native Windows
  release build embedded the exact source SHA and passed `version`, `doctor`, `status`,
  target/provenance, and stdin-capability smokes.
- **Live provider verification:** isolated two-round fixed sessions passed on direct
  DeepSeek at all six effort levels. Every round completed under 29 seconds, every
  planning-enabled level emitted one parsed control frame, `low` emitted none, all
  second turns recovered the exact effort-specific marker, and no tools or workspace
  writes occurred.
- **Workbench verification:** HASHI1 `temp`, fixed HER, direct DeepSeek
  `deepseek-v4-pro`, and `/effort max` used a previously empty HER checkpoint and
  `/api/browser/chat/send` with `timeout_s=280`. Request `req-0001` returned HTTP 200
  in 48.197496 seconds with all three options. Its first schema-valid JSON frame was
  rejected for retaining future work in a `direct_response`; the bounded retry parsed
  normally, with no prose or tool markup. Request `req-0002` contained only `A`,
  parsed its TaskFrame on attempt one, restored blue `ORCHID-WB-MAX-731`, and returned
  HTTP 200 in 37.865036 seconds. Both runs finished in one iteration with
  `tool_calls=0`; the tool-action audit gained no rows; the five-line persistent
  session contains no task-control envelope. No `/new`, reset, or wipe flow was used.
- **Recurrence count:** 0

### HER-20260814-030 — fixed-mode planner was detached from the persistent session

- **Status:** Verified
- **Severity:** P1
- **Expected:** every planning-enabled fixed-mode checkpoint sees the same effective
  normalized session messages as the primary task agent, including the immediately
  preceding assistant answer and the current incremental user turn.
- **Actual:** Sakura offered A/B/C choices in her persistent session, but the next
  planner request contained only the new `A` turn. The planner treated it as an
  unexplained letter and finalized that misunderstanding without invoking the
  session-backed primary generation.
- **User-visible impact:** immediate conversational references could lose continuity
  at `medium`, `high`, `xhigh`, `max`, or `max+`, and a planner-owned direct response
  could make the loss visible even though Claw had correctly persisted the history.
- **Root cause:** `run_task_checkpoint` rebuilt every planning request as a one-message
  vector from `turn_payload`; primary execution independently used
  `self.session.messages`. HASHI fixed mode correctly sent incremental turns and was
  not the owner of the missing context.
- **Fix:** all task checkpoints now clone the same already-normalized session message
  view used for execution. The prompt contract explicitly treats earlier messages as
  context while keeping the newest authoritative request as the sole active task. No
  lexical trigger or duplicate HASHI history injection is used.
- **HER source:** `e6fd0349ff53ed731fd4e34e7ddcb8a7946ddaaf`.
- **Fixed HER:** `0.1.0-hashi.18` Linux x86-64 / SHA-256
  `86cc892a23448c8bab045467dc6a72eccd8cea77fdfe1ea059a63cce5de4cc8c`.
- **HASHI package commit:** `fb01d2801a9f5cca2c029a6a94e9ab68e6409bb4`.
- **Regression tests:**
  `fixed_session_planner_sees_resumed_options_at_every_planning_effort` recreates an
  A/B/C assistant turn followed by an incremental `A` across `medium`, `high`,
  `xhigh`, `max`, and `max+`; `medium_plus_plans_replans_and_reports_non_blocking_tool_divergence`
  proves a later replan sees the primary agent's complete tool-bearing session.
- **Automated verification:** Rust runtime `655/655`, full Rust workspace tests, and
  workspace/all-target Clippy with warnings denied passed from the pinned clean source.
- **Live verification:** the `.19` Workbench canary recorded under
  `HER-20260814-031` established a new fixed HER session, then sent only `A` on the
  second turn. The first-attempt planner and final response both resolved the preserved
  option as blue `ORCHID-WB-MAX-731` without a duplicated HASHI history prompt, tool
  call, write, reset, or timeout.
- **Recurrence count:** 1 (`HER-20260816-033`)

### HER-20260814-029 — assurance checkpoints could not see current execution evidence

- **Status:** Fixed — live verification pending
- **Severity:** P2
- **Expected:** HIGH self-review and shared XHIGH/MAX/MAX+ mid-task replanning see
  the current turn's real tool inputs and results, then provide advisory feedback that
  may make the primary agent think again or revise without suppressing its answer.
- **Actual:** checkpoint requests contained the canonical turn payload and previous
  task frame but no current tool results. A long Sakura run therefore retained empty
  progress/evidence fields and eventually claimed that no tools had run. Repeated use
  of the same unplanned shell capability also triggered a new blind review each time.
- **Root cause:** `run_task_checkpoint` was not connected to the immutable evidence
  store used by independent completion review, and divergence triggers had no
  per-capability turn-level deduplication.
- **Fix:** pass current assistant tool calls and tool results into every mid-task
  checkpoint, render a bounded inline evidence ledger, keep the review explicitly
  advisory/fail-open, and trigger only one immediate review per unplanned canonical
  tool capability before returning to the configured periodic cadence.
- **HER source:** `781e39db266f33164245825d006d91cfc054fcf7`.
- **Fixed HER:** `0.1.0-hashi.17` Linux x86-64 / SHA-256
  `6e7ea72f5c50fb6af1d3adf67478ee79f8a55741f78ec2c4a775a3e43039af57`.
- **HASHI package commit:** `768aa0fe`.
- **Regression tests:** `high_effort_reserves_turns_for_review_and_validation`,
  `high_effort_adds_a_critical_review_after_tool_failure`,
  `high_effort_deduplicates_repeated_unplanned_tool_reviews`, and
  `inline_catalog_exposes_results_without_advertising_unavailable_tools`.
- **Live retest required:** one HIGH multi-tool task and one XHIGH task must show
  non-empty execution evidence in the checkpoint while the primary answer is still
  delivered normally.
- **Recurrence count:** 0

### HER-20260813-026 — MAX+ assurance became a hard completion gate

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **Expected:** planning chooses task-matched success, validation, and review work;
  critic output helps the primary agent improve but cannot confiscate its result.
- **Actual:** exact tool-name comparison produced false divergence triggers, fixed
  review limits repeated work, and a private time budget competed with `/timeout`.
- **Root cause:** advisory assurance was implemented as runtime-owned gating with
  mechanical matching and fixed ceilings instead of plan-directed feedback.
- **Fix:** HER `.11` canonicalizes tool capabilities, skips heavy review for trivial
  plans, derives revision allowance from the plan, deduplicates replan failures,
  removes the private MAX+ wall-clock budget, and returns final ownership to the
  primary agent after feedback.
- **Fixed HER:** `0.1.0-hashi.11` / source `f524b47054e5964b9ddfc61ab28cbfd990dc09af`
  / SHA-256 `93229c2b3aae40eabe5ed4582429a5247a4520ba45b6f1c99eecadecefaa1232`.
- **Live retest required:** one trivial handoff and one multi-tool MAX+ task on the
  upgraded packaged runtime.
- **Recurrence count:** 0

### HER-20260813-027 — Meditation blocked the primary queue

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **Expected:** Meditation uses the same agent's immutable context snapshot in a
  separate queue as one low-effort, tool-free reflection round.
- **Actual:** the model reflection shared the foreground execution lock and process
  identity, delaying the next user task and risking foreground cancellation state.
- **Root cause:** a broad lock covered both long model work and short durable Habit
  state transitions.
- **Fix:** separate Meditation execution lock, snapshot-only prompt, low effort,
  tools disabled, no resumed session, and narrow serialization only around store
  mutations. Existing turn-based scheduling remains unchanged.
- **HASHI fix:** `4f66ca86`.
- **Live retest required:** enqueue a foreground request while Meditation is running
  and verify independent progress plus correct `/stop` ownership.
- **Recurrence count:** 0

### HER-20260813-028 — incomplete handling discarded a usable final answer

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **Expected:** iteration exhaustion preserves a normal primary-agent final answer;
  only dangling tool/protocol output uses a deterministic recovery report.
- **Actual:** HASHI discarded HER's final text whenever HER classified the run as
  incomplete, even when all requested operations had succeeded.
- **Root cause:** completion classification was treated as higher authority than the
  primary agent's usable final payload.
- **Fix:** preserve normal final text and append resumable state; retain deterministic
  fallback only for malformed/dangling tool markup. Continue is preferred after
  successful uncertain side effects unless evidence proves failure or repetition.
- **HASHI fix:** `6d69ded6`.
- **Live retest required:** iteration-ceiling task with successful tools and a usable
  final, plus a dangling-tool negative control.
- **Recurrence count:** 0

### HER-20260811-001 — exact reasoning whitespace was corrupted

- **Status:** Verified
- **Severity:** P1
- **First observed:** live HER reasoning display showed both joined words and invented
  spaces inside words.
- **Affected area:** HER provider stream → HER JSONL → HASHI thinking display.
- **Expected:** raw reasoning deltas concatenate byte-for-byte, including leading,
  trailing, repeated, and whitespace-only fragments.
- **Actual:** fragment trimming/reconstruction removed or invented spaces.
- **Root cause:** delta handling treated streamed fragments as words/lines rather than
  exact fragments at one or more boundaries.
- **HASHI fix:** `9c4bbd5` and packaged follow-up in `ed9ce45`.
- **Regression tests:**
  - `tests/test_telegram_stream_policy.py::test_thinking_deltas_preserve_exact_provider_spacing`
  - `tests/test_her_adapter.py::test_claw_adapter_stream_json_emits_actual_thinking_delta`
- **Permanent retest:** stream exactness fixture in every provider/model/mode route;
  presentation-policy matrix at all efforts.
- **Recurrence count:** 0

### HER-20260811-002 — `/stop` continuation lost the original task

- **Status:** Verified
- **Severity:** P1
- **First observed:** after stopping a flex-mode job, a later bare “continue” request did
  not include enough information for Ajiao to identify the unfinished job.
- **Affected area:** HASHI interrupted-task state and HER continuation prompt assembly.
- **Expected:** HASHI persists the authoritative original prompt and rebinds it to a bare
  continuation while retaining verified workspace state.
- **Actual:** only the short continuation text could reach the backend.
- **Root cause:** interrupted task identity was not durably rebound at turn construction.
- **HASHI fix:** `ed9ce45`.
- **Regression test:**
  `tests/test_runtime_pipeline.py::test_build_turn_prompt_binds_bare_continue_to_persisted_stopped_task`
- **Permanent retest:** `C07` in all 48 cells, with cancellation after `run_started`, after
  a successful tool, and during finalization.
- **Recurrence count:** 0

### HER-20260811-003 — permission prompt broke the JSONL stream

- **Status:** Verified
- **Severity:** P1
- **First observed:** a stream-mode permission path emitted interactive text adjacent to
  structured events and could leave HASHI without a valid terminal record.
- **Affected area:** packaged HER CLI stream-json permission path.
- **Expected:** permission-required/decision events remain complete JSONL records; no
  terminal prompt is printed into stdout.
- **Actual:** interactive prompt text could contaminate the machine-readable stream.
- **HER source fix:** `a439c6eeef9f1d02a90e80a59d92940c519d2a84`.
- **Packaged HASHI fix:** HER `0.1.0-hashi.3`, HASHI commit `46d4ad0`.
- **Regression tests:**
  - HER source `stream_json_permission_prompt_preserves_the_jsonl_contract`
  - `tests/test_her_adapter.py::test_stream_json_parser_accepts_legacy_diagnostics_when_run_finished_exists`
- **Permanent retest:** structured-permission offline test, malformed-stream fault test,
  and full-permission live runs that assert no prompt path is reached.
- **Verification still required:** fixed/flex live finalization on both provider routes
  with the packaged `0.1.0-hashi.3` binary.
- **Recurrence count:** 0

### HER-20260811-004 — DeepSeek visible finalization ended in an error

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **First observed:** Ajiao completed useful work and displayed normal thinking, but the
  run ended as an error instead of producing a valid final terminal outcome.
- **Affected area:** OpenAI-compatible DeepSeek history translation during tool-free
  visible-finalization recovery.
- **Expected:** a thinking-only response gets one visible-finalization retry and always
  reaches `run_finished`; repeated thinking-only responses become
  `incomplete/no_final_text`.
- **Actual:** a reasoning-only assistant history item could be sent back to DeepSeek as
  an invalid assistant message, causing an HTTP error and no terminal event.
- **Root cause:** reasoning-only assistant history was retained even though DeepSeek
  requires user-visible assistant content for that history shape.
- **HER source fix:** `a439c6eeef9f1d02a90e80a59d92940c519d2a84`.
- **Packaged HASHI fix:** HER `0.1.0-hashi.3`, HASHI commit `46d4ad0`.
- **Regression tests:**
  - HER source `deepseek_v4_drops_reasoning_only_assistant_history`
  - HER source `thinking_only_response_gets_one_tool_free_visible_finalization_retry`
  - HER source `repeated_thinking_only_response_is_incomplete_with_deterministic_report`
- **Permanent retest:** thinking-only and no-final-text fault fixtures on both providers,
  both models, both modes, and all efforts.
- **Verification still required:** reproduce the original finalization shape through the
  live Official DeepSeek and OpenRouter routes with the packaged binary.
- **Recurrence count:** 0

### HER-20260811-005 — finalization diagnostics could leak prompt content

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **First observed:** investigation of a missing `run_finished` showed that raw command or
  output text could be included in parser error diagnostics.
- **Affected area:** HASHI HER adapter error construction and log delivery.
- **Expected:** errors identify the binary, last structured error kind, and protocol
  state without echoing the private prompt, secret, raw command, or full stream.
- **Actual:** unsafe diagnostic construction could expose sensitive request content.
- **HASHI fix:** `46d4ad0`.
- **Regression tests:**
  - `tests/test_her_adapter.py::test_stream_json_parser_missing_final_is_safe_and_fail_closed`
  - `tests/test_her_adapter.py::test_json_parser_error_does_not_echo_command_or_output`
- **Permanent retest:** secret-canary scan for all error and fault-injection runs.
- **Verification still required:** live fault-proxy run proving Telegram/user diagnostics
  and retained logs contain no prompt canary.
- **Recurrence count:** 0

### HER-20260811-006 — HER Tool Gateway dependency was undeclared

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **Discovered:** 2026-08-11 22:38 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / Layer A lab preflight /
  `her-20260811T123832Z-e3aa45`
- **Provider / model / mode / effort:** local scripted provider fixture; no live route or
  paid model request
- **HASHI commit and dirty state:** `46d4ad0188fd9ec8ef8ec9c808e55bd6880a1003`;
  pre-existing HER campaign documentation/template changes plus the isolated harness
- **HER package version / SHA-256:** `0.1.0-hashi.3` /
  `a201d8952c441c856af4b8304a87840e7c9916e6473f6cf5ed383e137f1d48ee`
- **HER source commit:** `a439c6eeef9f1d02a90e80a59d92940c519d2a84`
- **Expected:** the owner-only Tool Gateway context starts through Ajiao's configured
  Python environment, completes MCP initialization, and lists the isolated file and
  shell tools.
- **Actual:** `tools.gateway.mcp_stdio` exited before MCP initialization with
  `ModuleNotFoundError: No module named 'jsonschema'`; the scripted sequential-step MCP
  fixture started normally in the same lab.
- **User-visible impact:** a HER turn can continue without the required HASHI tools or
  repeatedly attempt to start the failed MCP server, making tool-bearing cells invalid
  and potentially leaving requested work incomplete.
- **First divergent event:** the `hashi-tools` MCP child process exited with status 1
  before returning its `initialize` response.
- **Evidence bundle:**
  `workspaces/ajiao/her_test_lab/runs/her-20260811T123832Z-e3aa45/evidence/`
- **Secrets/redaction checked:** yes; the evidence scan reported zero findings
- **Owning layer:** HASHI packaging/dependency declaration for the HER Tool Gateway
- **Root cause:** `tools/gateway/mcp_stdio.py` imports `jsonschema.Draft7Validator`, but
  neither `requirements.txt` nor `pyproject.toml` declares `jsonschema`, and the active
  HASHI2 virtual environment does not contain it.
- **Known-bad commit/package:** HASHI `46d4ad0188fd9ec8ef8ec9c808e55bd6880a1003` /
  HER `0.1.0-hashi.3`
- **Repair:** declared `jsonschema>=4.20.0,<5.0.0` in both install surfaces, installed it
  into the current HASHI2 environment, and repeated the exact two-server MCP handshake.
- **Fixed-build result:** isolated run `her-20260811T123936Z-b60dae` initialized both
  MCP servers, listed the HASHI tools and `her_step`, exited both servers cleanly, and
  reported zero evidence-scan findings; 18 targeted HASHI tests passed.
- **Final offline candidate:** HASHI `56c5069781ae2f9e6da155eb00c78d04b6dc18ae`,
  HER source `228442af944868e4c2ce8992e5343dc75a60e2ab`, package
  `0.1.0-hashi.6` / SHA-256
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794`.
- **Regression test:**
  `tests/test_her_debug_lab.py::test_lab_self_test_proves_isolation_and_cleanup_guard`
- **Required retest:** exact lab preflight, packaged HER sequential-step tool roundtrip,
  then full deterministic Layer A before any live Flash cell.
- **Recurrence count:** 0

### HER-20260811-007 — provider failure exited stream JSON without a terminal event

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **Discovered:** 2026-08-11 22:40 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / Layer A provider-fault preflight /
  `her-20260811T124319Z-406060`
- **Provider / model / mode / effort:** local scripted OpenAI-compatible provider /
  synthetic `openai/deepseek-v4-flash` routing fixture / one-shot / low
- **HASHI commit and dirty state:** `46d4ad0188fd9ec8ef8ec9c808e55bd6880a1003`;
  active isolated campaign changes retained
- **HER package version / SHA-256:** `0.1.0-hashi.3` /
  `a201d8952c441c856af4b8304a87840e7c9916e6473f6cf5ed383e137f1d48ee`
- **HER source commit:** `a439c6eeef9f1d02a90e80a59d92940c519d2a84`
- **Expected:** every stream-json run emits exactly one `run_started` and exactly one
  terminal `run_finished`, including sanitized provider failures.
- **Actual:** a scripted HTTP 400 produced valid JSONL containing `run_started` then
  `api_http_error`, exited with status 1, and never emitted `run_finished`.
- **User-visible impact:** HASHI cannot distinguish a cleanly terminated provider failure
  from a truncated or crashed HER stream, weakening recovery and risking a generic error
  that loses the authoritative terminal state.
- **First divergent event:** process exit immediately after `api_http_error`, with
  `run_finished_count=0`.
- **Completion status / stop reason:** absent because the terminal event was missing
- **Evidence bundle:**
  `workspaces/ajiao/her_test_lab/runs/her-20260811T124319Z-406060/evidence/`
- **Secrets/redaction checked:** yes; evidence verdict includes a zero-finding scan
- **Owning layer:** HER stream-json CLI terminalization
- **Root cause:** `run_prompt_stream_json` emits `run_started`, then applies `?` directly
  to `run_turn_observed`; any runtime/provider error returns before the function's sole
  `run_finished` emission.
- **Known-bad commit/package:** HER source
  `a439c6eeef9f1d02a90e80a59d92940c519d2a84` / package `0.1.0-hashi.3`
- **Repair:** emit one sanitized error-shaped `run_finished`, preserve the recoverable
  session when possible, retain a nonzero process exit, and suppress the duplicate
  top-level error envelope after the terminal event.
- **HER source fix:** `b4253e4f28e5ec56ee963800b0bc3b820b95a77a`
- **Candidate package:** HER `0.1.0-hashi.4` / SHA-256
  `bc1f5cc19cdf2fec2e1dbda97e9d27b73603f959374322ef099de83a725a4fae`
- **Fixed-build automated result:** the source regression passed, all 1460 Rust workspace
  tests passed, and the full all-target Clippy gate passed with warnings denied.
- **Final offline candidate:** HASHI `56c5069781ae2f9e6da155eb00c78d04b6dc18ae`,
  HER source `228442af944868e4c2ce8992e5343dc75a60e2ab`, package
  `0.1.0-hashi.6` / SHA-256
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794`;
  HTTP 400 run `her-20260811T131331Z-8ea23a` emitted exactly one safe
  `run_finished` and exited nonzero as required.
- **Regression test:** HER source
  `stream_json_provider_error_emits_terminal_run_finished`
- **Required retest:** scripted HTTP status, malformed/truncated stream, and connection
  failure cases; then full Layer A before any live Flash cell.
- **Recurrence count:** 0

### HER-20260811-008 — the all-target Clippy certification gate had drifted red

- **Status:** Verified
- **Severity:** P2
- **Discovered:** 2026-08-11 22:45 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / `HD-001` / Layer A build preflight
- **Provider / model / mode / effort:** no provider request; local Rust build gate
- **HER source commit:** `a439c6eeef9f1d02a90e80a59d92940c519d2a84` plus the
  uncommitted `HER-20260811-007` repair
- **Toolchain:** `rustc 1.95.0`, `clippy 0.1.95`
- **Expected:** `cargo clippy --workspace --all-targets -- -D warnings` exits zero before
  any candidate binary is packaged.
- **Actual:** the gate failed deterministically across runtime, API, tools, CLI, and
  test targets on accumulated lint drift, including unused imports/variables and newer
  style lints. The failures were independent of the provider-terminal repair.
- **User-visible impact:** the candidate cannot cross Layer A or be used for live Flash
  certification while the source quality gate is red.
- **First divergent event:** `runtime::trident` failed the first workspace Clippy pass;
  later passes exposed the remaining crates after earlier failures were removed.
- **Owning layer:** HER source build and release hygiene
- **Root cause:** source accepted by an earlier toolchain had not been reconciled with
  the current Rust 1.95 all-target warning set, while the release rule promotes every
  warning to an error.
- **Repair:** apply behavior-preserving lint fixes, retain the intentionally rich
  `ApiError` representation with an explicit documented lint allowance, then rerun
  format, full Clippy, and the entire workspace test suite.
- **HER source fix:** `b4253e4f28e5ec56ee963800b0bc3b820b95a77a`
- **Fixed-build result:** format check passed; full all-target Clippy passed with warnings
  denied; all 1460 workspace tests passed; reproducible release build completed.
- **Final candidate:** HASHI `56c5069781ae2f9e6da155eb00c78d04b6dc18ae`,
  HER source `228442af944868e4c2ce8992e5343dc75a60e2ab`, package
  `0.1.0-hashi.6` / SHA-256
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794`.
- **Closure — 2026-08-11:** Rust format, full workspace tests, Rust 1.95
  all-target Clippy with warnings denied, release build, full HER certification, and
  the complete HASHI suite (`1889 passed, 3 skipped`) all passed. No live-provider
  retest applies to this source-quality gate; route behavior remains covered by the
  later live-cell stages.
- **Regression anchor:**
  `cargo clippy --workspace --all-targets -- -D warnings`
- **Required retest:** clean format, clean full Clippy, complete workspace tests, and a
  reproducible release build before packaging.
- **Recurrence count:** 0

### HER-20260811-009 — sequential-step evidence used the wrong state field

- **Status:** Verified
- **Severity:** P3
- **Discovered:** 2026-08-11 22:59 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / `HD-001` / `sequential_steps` /
  `her-20260811T125900Z-034a29`
- **Provider / model / mode / effort:** local scripted provider /
  `openai/deepseek-v4-flash` / one-shot / fixture
- **HER package version / SHA-256:** `0.1.0-hashi.4` /
  `bc1f5cc19cdf2fec2e1dbda97e9d27b73603f959374322ef099de83a725a4fae`
- **Expected:** three ordered tool calls are accepted once, three durable step events are
  recorded, and the evidence checker reports PASS.
- **Actual:** HER completed three balanced calls and `accepted_steps=3`, but the checker
  counted a nonexistent `receipts` field and reported `receipt_count=0`, producing a
  false FAIL.
- **User-visible impact:** no runtime behavior loss; the certification controller would
  incorrectly block Stage 1 on valid evidence.
- **First divergent event:** post-run evidence normalization after the valid terminal
  `run_finished` event.
- **Owning layer:** isolated HER certification harness
- **Root cause:** `SequentialStepState` persists receipts under `events`, while the new
  packaged-candidate runner queried `receipts`.
- **Repair:** use the canonical `events` field, assert ordered step numbers and unique
  token hashes, add an integration regression, and rerun the exact candidate.
- **HASHI fix:** `56c5069781ae2f9e6da155eb00c78d04b6dc18ae`.
- **Fixed candidate:** HER `0.1.0-hashi.6` / SHA-256
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794` /
  source `228442af944868e4c2ce8992e5343dc75a60e2ab`.
- **Fixed-build result:** run `her-20260811T131328Z-e43795` passed with three
  ordered, unique, balanced tool calls and three durable step events; the packaged
  integration regression passed.
- **Evidence bundle:**
  `workspaces/ajiao/her_test_lab/runs/her-20260811T125900Z-034a29/evidence/`
- **Secrets/redaction checked:** yes; the private canary was absent and the scan passed
- **Closure — 2026-08-11:** the known-bad checker failed on valid package evidence; the
  repaired checker and regression passed on the frozen candidate. The full HER
  certification and HASHI suite (`1889 passed, 3 skipped`) passed. No live-provider
  retest applies to this evidence-harness-only defect.
- **Recurrence count:** 0

### HER-20260811-010 — malformed streams lost their protocol-failure classification

- **Status:** Fixed — live verification pending
- **Severity:** P2
- **Discovered:** 2026-08-11 23:00 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / `HD-001` / `malformed_sse`, `truncated_sse` /
  `her-20260811T130034Z-4ac385`, `her-20260811T130036Z-c77706`
- **Provider / model / mode / effort:** local scripted provider /
  `openai/deepseek-v4-flash` / one-shot / fixture
- **HER package version / SHA-256:** `0.1.0-hashi.4` /
  `bc1f5cc19cdf2fec2e1dbda97e9d27b73603f959374322ef099de83a725a4fae`
- **Expected:** malformed or truncated SSE fails closed as a named stream protocol
  failure, records the last safe structured event, emits one terminal event, and never
  includes the unsafe provider body or private prompt.
- **Actual:** both runs emitted one safe error-shaped `run_finished`, but
  `error_kind=unknown`; the terminal record contained no `last_safe_event`.
- **User-visible impact:** recovery and monitoring cannot distinguish provider protocol
  corruption from an unclassified runtime failure or locate the safe replay boundary.
- **First divergent event:** terminal error classification after `run_started`; no
  provider delta was accepted.
- **Owning layer:** HER stream-json CLI diagnostics
- **Root cause:** `classify_error_kind` has no arm for `ApiError::Json` response parse
  failures or invalid SSE frames, and the stream observer did not retain its latest
  emitted event kind for the terminal envelope.
- **Repair:** classify sanitized stream parse errors as
  `stream_protocol_error`, track `last_safe_event`, add a failing mock-provider
  regression, rebuild the package, and rerun the full offline blast radius.
- **HER source fix:** `b2b1e550cbd8aeb4704d11ed78de1a944eaaa6e4`.
- **Fixed candidate:** HASHI `56c5069781ae2f9e6da155eb00c78d04b6dc18ae`,
  HER `0.1.0-hashi.6` / SHA-256
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794` /
  source `228442af944868e4c2ce8992e5343dc75a60e2ab`.
- **Fixed-build result:** malformed run `her-20260811T131350Z-4f83f8` and truncated
  run `her-20260811T131351Z-27d575` both emitted one terminal
  `stream_protocol_error`, named `run_started` as the last safe event, and passed the
  secret-canary scan; source and packaged regressions passed.
- **Verification still required:** live fault-proxy coverage through both provider
  routes before the entry can be closed as Verified.
- **Evidence bundles:**
  `workspaces/ajiao/her_test_lab/runs/her-20260811T130034Z-4ac385/evidence/`,
  `workspaces/ajiao/her_test_lab/runs/her-20260811T130036Z-c77706/evidence/`
- **Secrets/redaction checked:** yes; both canary scans passed
- **Recurrence count:** 0

### HER-20260811-011 — HTTP 403 was not classified as an authentication failure

- **Status:** Fixed — live verification pending
- **Severity:** P2
- **Discovered:** 2026-08-11 23:07 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / `HD-001` / `http_403` /
  `her-20260811T130719Z-d0f089`
- **Provider / model / mode / effort:** local scripted provider /
  `openai/deepseek-v4-flash` / one-shot / fixture
- **HER package version / SHA-256:** `0.1.0-hashi.5` /
  `baf6d9d64eaf8e1434320e9cf046dca12320118671e3285080909d598d037fe9`
- **Expected:** 401 and 403 both fail closed as `api_auth_error`, without credentials
  or private prompt text.
- **Actual:** 401 was `api_auth_error`; 403 was the generic `api_http_error`.
- **User-visible impact:** automated recovery may suggest a generic retry instead of
  correcting route permissions or credentials.
- **First divergent event:** error-kind classification of the sanitized
  `403 Forbidden` provider error.
- **Owning layer:** HER CLI error taxonomy
- **Root cause:** the auth classifier matched `401`, `Unauthorized`, and
  `authentication_error`, but omitted `403` and `Forbidden` despite the documented
  401/403 contract.
- **Repair:** extend the auth discriminant regression and classifier, rebuild
  the package, and rerun both 401 and 403 fixtures.
- **HER source fix:** `228442af944868e4c2ce8992e5343dc75a60e2ab`.
- **Fixed candidate:** HASHI `56c5069781ae2f9e6da155eb00c78d04b6dc18ae`,
  HER `0.1.0-hashi.6` / SHA-256
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794`.
- **Fixed-build result:** 401 run `her-20260811T131331Z-1a5fc0` and 403 run
  `her-20260811T131332Z-064b86` both failed closed as `api_auth_error`, emitted one
  terminal event, and passed the secret-canary scan; the source regression passed.
- **Verification still required:** controlled live authentication-failure coverage or
  equivalent route-owned fault-proxy evidence for both provider routes.
- **Evidence bundle:**
  `workspaces/ajiao/her_test_lab/runs/her-20260811T130719Z-d0f089/evidence/`
- **Secrets/redaction checked:** yes; the private canary was absent
- **Recurrence count:** 0

### HER-20260811-012 — delayed-response fixture logged an expected disconnect traceback

- **Status:** Verified
- **Severity:** P3
- **Discovered:** 2026-08-11 23:07 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / `HD-001` / `delayed_response_once` /
  `her-20260811T130723Z-c582e8`
- **Provider / model / mode / effort:** local scripted provider /
  `openai/deepseek-v4-flash` / one-shot / fixture
- **Expected:** the first delayed response exceeds the one-second client timeout, the
  client disconnect is treated as an expected fixture event, and the retry succeeds
  without controller stderr noise.
- **Actual:** the retry succeeded and the run passed, but the fixture server printed a
  Python `BrokenPipeError` traceback while writing to the timed-out first connection.
- **User-visible impact:** no HER behavior loss; uncontrolled fixture stderr can confuse
  evidence review and hide a later meaningful harness failure.
- **First divergent event:** fixture response write after the expected client disconnect.
- **Owning layer:** isolated HER scripted-provider harness
- **Root cause:** the HTTP handler did not treat `BrokenPipeError` and
  `ConnectionResetError` as expected after a deliberately induced timeout.
- **Repair:** absorb only those two disconnect exceptions at the handler
  boundary, add a deterministic regression, and rerun the delayed-response scenario.
- **HASHI fix:** `56c5069781ae2f9e6da155eb00c78d04b6dc18ae`.
- **Fixed-build result:** packaged run `her-20260811T131346Z-c1039c` retried once,
  completed successfully, counted the expected disconnect, and produced no traceback;
  the deterministic harness regression passed.
- **Evidence bundle:**
  `workspaces/ajiao/her_test_lab/runs/her-20260811T130723Z-c582e8/evidence/`
- **Secrets/redaction checked:** yes
- **Closure — 2026-08-11:** fixed and verified against HER `0.1.0-hashi.6` /
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794`;
  source `228442af944868e4c2ce8992e5343dc75a60e2ab`; full HER certification
  and HASHI tests passed. No live-provider retest applies to this fixture-only defect.
- **Recurrence count:** 0

### HER-20260811-013 — native-ceiling evidence counted the finalization turn as a tool turn

- **Status:** Verified
- **Severity:** P3
- **Discovered:** 2026-08-11 23:10 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / `HD-001` / `iteration_ceiling:12` /
  `her-20260811T131053Z-7645ee`
- **Provider / model / mode / effort:** local scripted provider /
  `openai/deepseek-v4-flash` / one-shot / fixture
- **HER package version / SHA-256:** `0.1.0-hashi.6` /
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794`
- **Expected:** a configured ceiling of N produces exactly N provider iterations; the
  final iteration is tool-free finalization, leaving N-1 balanced tool executions and
  `incomplete/max_iterations`.
- **Actual:** HER did exactly that for N=12, but the new checker expected 12 accepted
  tools and falsely failed the otherwise valid run.
- **User-visible impact:** no HER behavior loss; the controller would block valid native
  boundary evidence.
- **First divergent event:** post-run ceiling assertion after the valid terminal event.
- **Owning layer:** isolated HER certification harness
- **Root cause:** the checker equated the configured provider-iteration budget with tool
  executions and did not reserve the runtime's mandatory tool-free finalization turn.
- **Repair:** assert N provider requests, N terminal iterations, and N-1 ordered,
  unique, balanced tool executions; add a packaged integration regression and run all
  six native limits.
- **HASHI fix:** `56c5069781ae2f9e6da155eb00c78d04b6dc18ae`.
- **Fixed-build result:** limits 12, 32, 96, 192, 384, and 512 all passed on the
  frozen package with N provider iterations and N-1 balanced tool executions. Run IDs:
  `her-20260811T131205Z-83bf16`, `her-20260811T131206Z-9e93b3`,
  `her-20260811T131208Z-57fc12`, `her-20260811T131219Z-118d45`,
  `her-20260811T131222Z-df1ae7`, and `her-20260811T131229Z-6d91f4`.
- **Evidence bundle:**
  `workspaces/ajiao/her_test_lab/runs/her-20260811T131053Z-7645ee/evidence/`
- **Secrets/redaction checked:** yes
- **Closure — 2026-08-11:** fixed and verified against HER `0.1.0-hashi.6` /
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794`;
  source `228442af944868e4c2ce8992e5343dc75a60e2ab`; the packaged integration
  regression, full HER certification, and full HASHI suite passed. No live-provider
  retest applies to this harness-only defect.
- **Recurrence count:** 0

### HER-20260812-014 — configured MCP child logs leaked into structured CLI stderr

- **Status:** Verified
- **Severity:** P1
- **Recurrence of:** none; related output-isolation class `HER-20260811-003`
- **Discovered:** 2026-08-12 00:12 AEST
- **Reporter:** `lin_yueru@HASHI2` controller and `ajiao@HASHI2` worker
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / `HD-002` /
  `HD-002-A002-20260811T135928Z` /
  `allowed_tools_errors_have_typed_json_and_alias_map_432`
- **Provider / model / mode / effort:** no provider request / worker orchestration model
  `local/deepseek-v4-flash` / bounded offline verification / high
- **Presentation policy:** not applicable; CLI `--output-format json` error contract
- **HASHI commit and dirty state:**
  `eb0bb06c6903a757c6fb59e5dad1c6005bdd9daa`, clean when reproduced
- **HER package version / SHA-256:** `0.1.0-hashi.6` /
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794`
- **HER source commit:** `228442af944868e4c2ce8992e5343dc75a60e2ab`, clean when reproduced
- **Expected:** an invalid `--allowedTools` value in JSON output mode emits one typed
  JSON error on stdout and keeps stderr empty, even when the loaded configuration has
  a stdio MCP server that writes startup diagnostics to its own stderr.
- **Actual:** the configured `hashi-tools` MCP child wrote `ToolRegistry initialized`
  at INFO level; HER inherited the child's stderr into its own stderr, violating the
  structured error contract and failing the Rust workspace gate at 108 passed / 1 failed.
- **User-visible impact:** machine consumers that require the documented empty-stderr
  JSON error contract can reject an otherwise valid response; the same leak also made
  the deterministic HER certification gate depend on the invoking agent's live config.
- **First divergent event:** `McpStdioProcess::spawn` started the configured MCP child
  with `stderr(Stdio::inherit())` before the invalid-tool JSON error was returned.
- **Completion status / stop reason / provider stop reason:** offline dispatch FAIL /
  `offline_contract_divergence` / not applicable
- **Session and request IDs:** dispatch `HD-002-A002-20260811T135928Z`; Ajiao transcript
  line 1006; no provider request ID
- **Tool calls / results / iterations:** targeted HASHI tests `23 passed`; full HER
  certification failed in Rust workspace tests; isolated single-test control passed.
- **Reproduction rate:** 1/1 with Ajiao's configured `CLAW_CONFIG_HOME`; 0/1 with an
  empty `CLAW_CONFIG_HOME` control.
- **Minimal reproduction:** from the HER `rust/` directory, run
  `CLAW_CONFIG_HOME=/path/to/bridge-home/workspaces/agent/backend_state/her_config cargo test -p rusty-claude-cli --test output_format_contract allowed_tools_errors_have_typed_json_and_alias_map_432 -- --exact --nocapture`.
- **Evidence bundle:**
  `superloops/loops/sl-20260811-123616159717-57f4/evidence/HD-002-A002-receipt.json`;
  preserved full-verifier log SHA-256
  `7210213cc870e230ecd57ef1bbfd791c8217cd827d97933bed272fedf70983de`
- **Secrets/redaction checked:** yes; the journal retains no configured values or raw
  tool arguments.
- **Suspected owner:** HER stdio MCP runtime; the CLI test helper's ambient
  `CLAW_CONFIG_HOME` inheritance is a determinism hardening gap.
- **Root cause:** HER directly inherited each stdio MCP child's stderr. MCP servers are
  allowed to use stderr for diagnostics, so a configured server could write arbitrary
  text into HER's parent stderr. The test helper also failed to clear ambient
  `CLAW_CONFIG_HOME`, which made the latent product defect reproducible only in a
  configured worker environment.
- **Known-bad commit/package:** source
  `228442af944868e4c2ce8992e5343dc75a60e2ab`; HER `0.1.0-hashi.6` /
  `7cd1be43aa9c1786295ebe531ab49ddd3d03ba01b3c7841c2b6349b883493794`
- **Fix commits:** HER source
  `b78a5cc60db8b6d5944bc0f507f31e260dbca851`; HASHI package
  `bcc17dd38398cb7a6a08b771e5d4e28a0e83865e`.
- **Repair:** pipe and continuously drain stdio MCP child stderr without replaying or
  retaining raw content; expose only an observed-byte count for lifecycle checks; make
  the CLI test helper clear ambient `CLAW_CONFIG_HOME` unless a test explicitly supplies
  one.
- **Regression tests:**
  `allowed_tools_json_errors_isolate_configured_mcp_stderr` and
  `stdio_process_drains_child_stderr_without_inheriting_parent`.
- **Bad-build test result:** known-bad configured-environment reproduction failed with
  the exact `ToolRegistry initialized` stderr line; the empty-config control passed.
- **Fixed-build test result:** both focused regressions passed; all 23 stdio MCP runtime
  tests and all 110 output-format contract tests passed; full Rust workspace tests,
  all-target Clippy with warnings denied, release build, and full HER certification
  passed for source `b78a5cc60db8b6d5944bc0f507f31e260dbca851`. Packaged HER
  `0.1.0-hashi.7` / `e7c4b6ecf9cacd2dab1657f03dcc818d07239f34c0a3e187ea8dd65c6f2c75c8`
  returned typed `invalid_tool_name` with zero stderr bytes under Ajiao's configured MCP
  environment; the full HASHI suite passed with `1890 passed, 3 skipped`.
- **Required live retest cells:** no paid-provider cell applies to this offline process
  boundary; require an activated-package configured-MCP JSON smoke before Stage 1.
- **Live retest result:** direct packaged configured-MCP smoke passed without a provider
  request; Ajiao then activated the same `0.1.0-hashi.7` binary and returned the exact
  activation receipt `HERDBG_ACTIVATION_SMOKE_002_OK`. The correlated Layer A chain
  completed with A004's full verifier exit `0` and A005's single-call read-only
  provenance closeout. No paid-provider request or live certification cell applied.
- **Closure — 2026-08-12:** verified against HER `0.1.0-hashi.7` /
  `e7c4b6ecf9cacd2dab1657f03dcc818d07239f34c0a3e187ea8dd65c6f2c75c8`;
  source `b78a5cc60db8b6d5944bc0f507f31e260dbca851`. The focused regressions,
  full HER source certification, full HASHI suite (`1890 passed, 3 skipped`),
  activated-package smoke, and independent correlated Layer A verification passed.
  Evidence: `HD-002-A004-receipt.json` and `HD-002-A005-receipt.json` under Superloop
  `sl-20260811-123616159717-57f4`.
- **Remaining risk:** other child-process boundaries may independently inherit
  diagnostics and require their own output-contract review.
- **Recurrence count:** 0

### HER-20260812-015 — medium planning format deviation aborted a valid task

- **Status:** Verified
- **Severity:** P1
- **Recurrence of:** none; related terminal-output class `HER-20260811-004`
- **Discovered:** 2026-08-12 02:04 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / `HD-003` /
  `HER-LIVE-DS-FLASH-FIXED-MEDIUM` / `C01` /
  `her-20260811T155917Z-eac66a`
- **Provider / model / mode / effort:** official DeepSeek /
  `deepseek-v4-flash` / fixed / medium
- **Presentation policy:** thinking=true, verbose=true, typing=true
- **HASHI commit and dirty state:**
  `a13cdf71856b5474c76beb9d585b70c50f0478f7`, clean when reproduced
- **HER package version / SHA-256:** `0.1.0-hashi.7` /
  `e7c4b6ecf9cacd2dab1657f03dcc818d07239f34c0a3e187ea8dd65c6f2c75c8`
- **HER source commit:** `b78a5cc60db8b6d5944bc0f507f31e260dbca851`, clean when reproduced
- **Expected:** a valid exact-output request at medium effort either obtains a TaskFrame
  and executes normally or continues from a conservative, authorization-preserving
  planning fallback; the exact provider answer is delivered with a completed terminal.
- **Actual:** the initial task-planning control call returned the exact user-requested
  text instead of TaskFrame JSON. HER treated that single format deviation as fatal,
  emitted only `run_started` and `run_finished`, and returned
  `completion_status=error`, `stop_reason=runtime_error`, `error_kind=unknown`, and zero
  iterations even though the provider returned HTTP 200 and the assistant hash exactly
  matched the requested output.
- **User-visible impact:** safe medium-effort prompts that demand exact output can fail
  before execution with only “Execution failed before completion,” losing a valid
  provider response and offering no actionable diagnosis.
- **First divergent event:** initial `run_task_checkpoint` parsed the valid provider text
  as an invalid TaskFrame and returned a `RuntimeError` before the execution loop.
- **Completion status / stop reason / provider stop reason:** error / runtime_error /
  none; provider stream itself ended with `stop`.
- **Session and request IDs:** HASHI `req-0021`; HER
  `session-1786463958501-0`; proxy trace sequence 63.
- **Tool calls / results / iterations:** 0 / 0 / 0.
- **Reproduction rate:** 1/1 isolated HASHI live run and 1/1 bounded direct packaged
  run using the same route, model, medium planning configuration, and exact-output shape.
- **Minimal reproduction:** run packaged HER with `CLAW_TASK_PLANNING=1`,
  `CLAW_EXECUTION_EFFORT=medium`, stream-json output, and a safe exact-output prompt;
  use a scripted API in the regression so the planning response is valid text but not a
  TaskFrame, then require the execution response to complete.
- **Evidence bundle:**
  `superloops/loops/sl-20260811-123616159717-57f4/evidence/HD-003-HER-20260812-015-reproduction.json`;
  `superloops/loops/sl-20260811-123616159717-57f4/evidence/HD-003-HER-20260812-015-red.json`;
  `workspaces/ajiao/her_test_lab/runs/her-20260811T155917Z-eac66a/evidence/`.
- **Secrets/redaction checked:** yes; no authorization header, raw provider text, or raw
  prompt is retained in controller evidence.
- **Suspected owner:** HER conversation runtime initial task-planning recovery.
- **Root cause:** non-assurance task planning has one initial format attempt. Its
  invalid-format branch records `turn_failed` and returns immediately. The runtime
  already has a conservative MAX planning fallback and already preserves a prior frame
  on replan failure, but medium initial planning does neither.
- **Known-bad commit/package:** source
  `b78a5cc60db8b6d5944bc0f507f31e260dbca851`; HER `0.1.0-hashi.7` /
  `e7c4b6ecf9cacd2dab1657f03dcc818d07239f34c0a3e187ea8dd65c6f2c75c8`.
- **Fix commits:** HER source
  `69c88798237132da9874e5a300ab2c25e6fd9ae2`; HASHI package
  `b570daf2a00dd7ddeb620d8138c7450fee0737fe`.
- **Repair:** share the existing conservative frame builder between MAX and medium, but
  allow the medium fallback only for the exact syntactic “invalid task frame” error.
  The fallback keeps the authoritative request as `active_goal`, declares no tools,
  forbids authorization expansion, emits explicit control fallback telemetry, and does
  not manufacture an assurance plan. Provider failures, empty responses, generic
  acknowledgements, invalid task identification, and every assurance-enabled effort
  remain fail-closed.
- **Regression tests:**
  `medium_planning_format_failure_uses_conservative_fallback`; boundary coverage from
  `generic_acknowledgement_stops_before_execution`,
  `high_effort_rejects_a_plan_without_assurance_strategies`, and
  `max_planning_revision_format_exhaustion_continues_with_last_valid_plan`.
- **Bad-build test result:** deterministic RED selected one test and failed one test on
  source `b78a5cc60db8b6d5944bc0f507f31e260dbca851` with the exact pre-execution
  `invalid task frame` RuntimeError.
- **Fixed-build test result:** the focused regression and three boundary tests passed;
  the runtime crate passed 643 unit, 2 conformance, and 12 integration tests; all-target
  runtime Clippy with warnings denied, the release build, and full HER workspace
  certification passed. HASHI's 73 targeted HER/Superloop tests and full suite
  (`1890 passed, 3 skipped`) also passed. Packaged HER
  `0.1.0-hashi.8` / `4f5f5dad26a208cef67f8f3ed84fbecaa0ed3d8164df22010ceb918a7456269b`.
  Activation and the exact live failure retest passed as recorded below.
- **Required live retest cells:** rerun the exact fixed/medium C01 failure before
  closure, then rerun its flex and provider-route twins plus all prior Flash evidence
  after the documentation-only successor candidate is frozen; final certification
  requires one candidate.
- **Live retest result:** activated HER `0.1.0-hashi.8` under Ajiao after `/reboot min`.
  Activation smoke `req-0001` reproduced the exact medium planning deviation, emitted
  `invalid_format` then `fallback`, and completed with the exact requested line and zero
  tools. The exact official-DeepSeek fixed/medium C01 cell then passed as run
  `her-20260811T162738Z-80a98a`: both provider calls returned HTTP 200 from
  `deepseek-v4-flash`, the same fallback telemetry appeared, stream and visible hashes
  matched, one start and one terminal finish were present, and no funds signal appeared.
  Evidence: `ACTIVATION-SMOKE-003.json` and
  `HD-003-HER-20260812-015-verification.json` under Superloop
  `sl-20260811-123616159717-57f4`, plus the run's retained `evidence/verdict.json`.
- **Closure — 2026-08-12:** verified against source
  `69c88798237132da9874e5a300ab2c25e6fd9ae2` and HER `0.1.0-hashi.8` /
  `4f5f5dad26a208cef67f8f3ed84fbecaa0ed3d8164df22010ceb918a7456269b`.
  The known-bad RED, focused and boundary regressions, full HER certification, full
  HASHI suite, active-binary smoke, and exact live failure retest all passed. The next
  HASHI commit is documentation-only; the complete 48-cell campaign will restart after
  freezing that successor identity.
- **Cross-instance confirmation — 2026-08-12 09:40 AEST:** `zhaojun@HASHI1`
  request `req-0003` asked a safe, open-ended HER permission-design question at high
  effort. It stopped before tools after 20.28 seconds with zero response characters and
  the exact 92-character `INVALID_TASK_FRAME_ERROR`. HASHI1 was running packaged HER
  `0.1.0-hashi.1` / SHA-256
  `bb2d233bf4c2dc358bab20ed9e816bdce4cde3b80ec59e1f76ef59ab8924efb1`,
  source `8902f31bc5f887332335c0d152e76aabd539710d`; that source predates the
  bounded high-assurance initial-frame retry in `d3e659534b33823818630db309ceb3f319c981cf`.
  This is additional evidence for the same initial planning-format root-cause class,
  not a new defect ID and not a recurrence on a fixed build.
- **High-effort repair coverage:** assurance-enabled high effort makes at most two
  initial TaskFrame attempts. The second attempt carries an explicit format-recovery
  instruction and still validates the full assurance frame before any task tool can
  run. It does not retry a partially executed outer request, weaken authorization,
  change provider/model, or escalate Flash to Pro. Current source
  `8f0f7344fbf64951619e44dd4c834aae57c29f53` passed
  `high_effort_retries_an_invalid_initial_assurance_frame_once` and the medium fallback
  regression on 2026-08-12. Packaged live high-effort coverage remains part of the
  same-candidate Stage 1 Flash matrix.
- **Remaining risk:** assurance-enabled high/xhigh/max+ planning intentionally retains
  stricter validation after bounded format recovery; those effort cells must prove their
  own recovery and terminal contracts without weakening authorization boundaries.
- **Regression and invariant expansion — 2026-08-17:** Arale request `req-0004` at
  medium effort produced a structurally parseable TaskFrame whose `planned_tools`
  contained the non-canonical prose `write_file 或 hashi_file_write`. The strict
  validator correctly rejected that field, but the integrated runtime again treated the
  planner's semantic validation error as a terminal backend failure before the primary
  Agent or any task tool ran. This is a recurrence of the pre-execution planning-gate
  failure class, with a new semantic/tool-name subtype; it is not a model tool-execution
  capability failure.
- **Revised invariant:** planning and independent review are advisory. Their provider,
  response, schema, semantic, tool-registry, resolution or assurance validation failures
  remain strict and auditable, but cannot decide whether the primary Agent gets to run.
  Runtime permissions, safety controls and concrete tool dispatch remain authoritative.
  When user input is genuinely required, the primary Agent must end the turn with its
  persona-authored progress report and question instead of guessing or disappearing.
- **2026-08-17 source repair:** all exhausted initial TaskFrame failures now install the
  same conservative, authorization-preserving fallback used by the primary Agent; the
  original diagnostic is exported as `planning_status=failed` / `planning_error`, made
  user-visible without replacing the Agent's answer, and retained with the task
  checkpoint and verified execution ledger in cross-session receipts for fixed and flex
  continuation. Validation itself was not weakened, and planning retries remain only a
  bounded quality optimization.
- **2026-08-17 regression coverage:** exact Arale prose is covered by
  `medium_noncanonical_planned_tool_prose_reports_and_executes_real_tools`; provider
  failure by `initial_planner_provider_error_is_visible_but_does_not_block_agent`;
  canonical short-choice continuity by
  `unresolved_short_choice_plan_falls_back_to_canonical_context_execution`; generic and
  missing-assurance frames by the renamed non-blocking boundary tests; reviewer
  non-ownership by `max_planning_block_is_advisory_and_task_agent_still_answers`; and
  HASHI delivery/continuity by the planning-notice and cross-session receipt tests.
- **Recurrence count:** 1
- **Recurrence count:** 0

### HER-20260812-016 — request activity timestamps moved backwards

- **Status:** Verified
- **Severity:** P2
- **Recurrence of:** none
- **Discovered:** 2026-08-12 02:54 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / `HD-003` /
  `HER-LIVE-DS-FLASH-FIXED-HIGH` / `C00` /
  `her-20260811T165326Z-9d3f30`
- **Provider / model / mode / effort:** official DeepSeek /
  `deepseek-v4-flash` / fixed / high
- **Presentation policy:** thinking=true, verbose=true, typing=true
- **HASHI commit and dirty state:**
  `5e2df67f32f8458d0a6ddaaaf41900d2882e33bd`, clean when reproduced
- **HER package version / SHA-256:** `0.1.0-hashi.8` /
  `4f5f5dad26a208cef67f8f3ed84fbecaa0ed3d8164df22010ceb918a7456269b`
- **HER source commit:** `69c88798237132da9874e5a300ab2c25e6fd9ae2`, clean when reproduced
- **Expected:** request activity sequence and timestamps are nondecreasing in delivery
  order, including across ordinary wall-clock corrections.
- **Actual:** sequence 3 (`HER task started`) carried timestamp
  `1786467207.619370`, then sequence 4 (`HER stream started`) carried the earlier
  timestamp `1786467206.7893784`. All response, route, model, stream, visible-output,
  completion, and terminal assertions otherwise passed.
- **User-visible impact:** activity consumers can observe a negative elapsed interval or
  move a progress display backwards even though the request itself succeeds.
- **First divergent event:** `RequestActivityStore._append_unlocked` accepted the
  regressing event timestamp verbatim instead of preserving the store's sequence-order
  invariant.
- **Completion status / stop reason / provider stop reason:** completed / end_turn /
  end_turn.
- **Session and request IDs:** HASHI `req-0032`; HER
  `session-1786467207631-0`.
- **Tool calls / results / iterations:** 0 / 0 / 1.
- **Reproduction rate:** 1/1 live observation; deterministic store-level reproduction
  is 1/1 by appending timestamps `12.0` then `11.0` in sequence order.
- **Minimal reproduction:** create a request activity record, publish one event at
  timestamp `12.0`, then publish the next event at `11.0`; the known-bad store exposes
  `[12.0, 11.0]` for increasing sequence numbers.
- **Evidence bundle:**
  `superloops/loops/sl-20260811-123616159717-57f4/evidence/HD-003-HASHI-20260812-016-reproduction.json`;
  `superloops/loops/sl-20260811-123616159717-57f4/evidence/HD-003-HASHI-20260812-016-red.json`;
  `superloops/loops/sl-20260811-123616159717-57f4/evidence/HD-003-HASHI-20260812-016-offline-verification.json`;
  `superloops/loops/sl-20260811-123616159717-57f4/evidence/HD-003-HASHI-20260812-016-verification.json`;
  `workspaces/ajiao/her_test_lab/runs/her-20260811T165326Z-9d3f30/evidence/verdict.json`.
- **Secrets/redaction checked:** yes; no credential, raw prompt, or private reasoning is
  included in the controller record.
- **Suspected owner:** HASHI request activity projection.
- **Root cause:** activity sequence is authoritative, but the display-only store copied
  each adapter's wall-clock timestamp without clamping it to the preceding sequenced
  event. A backwards wall-clock adjustment therefore broke the public monotonic-time
  contract while sequence numbers remained correct.
- **Known-bad commit/package:** HASHI
  `5e2df67f32f8458d0a6ddaaaf41900d2882e33bd`; HER `0.1.0-hashi.8` /
  `4f5f5dad26a208cef67f8f3ed84fbecaa0ed3d8164df22010ceb918a7456269b`.
- **Fix commits:** HASHI
  `89c8ebbefed0790f6370628680eb7c611e4631b0`.
- **Repair:** normalize each appended projection timestamp to be no earlier than the
  prior sequenced event, and derive `started_at` / `completed_at` from those normalized
  lifecycle events. Backend source events remain untouched.
- **Regression tests:**
  `tests/test_request_activity.py::test_request_activity_clamps_regressing_timestamps_to_sequence_order`.
- **Bad-build test result:** deterministic RED selected one test and failed one test on
  HASHI `5e2df67f32f8458d0a6ddaaaf41900d2882e33bd`; the store exposed
  `[10.0, 11.0, 12.0, 11.0, 9.0]` instead of the required nondecreasing timestamps.
- **Fixed-build test result:** the focused regression passed; all 6 request-activity
  tests passed; the 106 request-activity, HER-adapter, and runtime-pipeline tests passed;
  the full HASHI suite passed with `1891 passed, 3 skipped`.
- **Required live retest cells:** defect closeout requires the exact fixed/high C00
  failure after activating the repaired HASHI runtime. The campaign separately requires
  all 48 cells to restart because the repair is in a shared HASHI delivery layer.
- **Live retest result:** after `/reboot min`, the first cold attempt was a preserved
  planning-format model deviation and its activity timestamps were already monotonic.
  The second cold attempt passed as run `her-20260811T171109Z-42fdeb`: request
  `req-0002`, session `session-1786468271163-0`, two official-DeepSeek HTTP 200 calls,
  exact configured model and route, exact stream and visible output, one start, one
  terminal completion, and nondecreasing activity sequence and timestamps.
- **Closure — 2026-08-12:** verified with HASHI runtime fix
  `89c8ebbefed0790f6370628680eb7c611e4631b0` under activated documentation successor
  `71e8761b722dbd791c4356e174f70a4440c4d5e6`; HER source and packaged binary remained
  `69c88798237132da9874e5a300ab2c25e6fd9ae2` and `0.1.0-hashi.8` /
  `4f5f5dad26a208cef67f8f3ed84fbecaa0ed3d8164df22010ceb918a7456269b`.
  The known-bad RED, focused regression, 106-test blast radius, full HASHI suite
  (`1891 passed, 3 skipped`), narrow activation, and exact live failure retest passed.
  All pre-repair live evidence remains excluded from final certification.
- **Remaining risk:** other timestamped presentation projections may require independent
  monotonicity review; this repair is intentionally scoped to request activity.
- **Recurrence count:** 0

### HER-20260812-017 — AskUserQuestion terminal UI corrupted structured JSONL

- **Status:** Fixed — blast-radius verification pending
- **Severity:** P1
- **Recurrence of:** none; related output-isolation class `HER-20260811-003`
- **Discovered:** 2026-08-12 07:17 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-123616159717-57f4` / `HD-003` /
  `HER-LIVE-DS-FLASH-FLEX-MAX` / `C02` attempt 2 /
  `her-20260811T204652Z-767754`
- **Provider / model / mode / effort:** official DeepSeek /
  `deepseek-v4-flash` / flex / max
- **Presentation policy:** thinking=true, verbose=true, typing=true
- **HASHI commit and dirty state:**
  `9b48935e324ad69eea9026ea4a505046f73c31bb`, clean when reproduced
- **HER package version / SHA-256:** `0.1.0-hashi.8` /
  `4f5f5dad26a208cef67f8f3ed84fbecaa0ed3d8164df22010ceb918a7456269b`
- **HER source commit:** `69c88798237132da9874e5a300ab2c25e6fd9ae2`, clean when
  reproduced
- **Expected:** every emitted `tool_start` has one valid JSONL `tool_end` with the same
  id and name. A terminal-owned question tool must not write human prompt text into a
  machine-readable stream or block on unavailable input.
- **Actual:** HASHI parsed three tool starts but only two tool ends. The missing parsed
  event was `AskUserQuestion` id `call_00_vh7JabQ7qERSQ8xq7hf43097` at iteration 3,
  while `run_finished` reported three tool uses and three tool results.
- **User-visible impact:** the canonical event ledger contradicted its terminal
  accounting, so HASHI could not trust the run as certification evidence even though
  the request reached a completed terminal state. Other strict JSONL consumers could
  fail the stream entirely.
- **First divergent event:** after the valid `AskUserQuestion` `tool_start`, the tool
  wrote `[Question]`, options, and `Enter choice` terminal text directly to stdout. HER
  did emit the correlated `tool_end`, but it was appended to the same non-JSON prompt
  line and was therefore invisible to a line-oriented JSON parser.
- **Completion status / stop reason / provider stop reason:** completed / end_turn /
  end_turn.
- **Session and request IDs:** HASHI `req-0079`; HER
  `session-1786481212692-0`.
- **Tool calls / results / iterations:** terminal ledger 3 / 3 / 8; parseable event
  ledger 3 starts / 2 ends.
- **Reproduction rate:** 1/1 original live observation and 1/1 deterministic known-bad
  regression.
- **Minimal reproduction:** use scripted provider output that calls
  `AskUserQuestion`, run packaged HER with stdin closed and
  `--output-format=stream-json`, and require every stdout line to parse as JSON plus a
  same-id `tool_start` / `tool_end` pair.
- **Evidence bundle:**
  `superloops/loops/sl-20260811-123616159717-57f4/evidence/HD-003-HER-20260812-017-reproduction.json`;
  `superloops/loops/sl-20260811-123616159717-57f4/evidence/HD-003-HER-20260812-017-red.json`;
  `superloops/loops/sl-20260811-123616159717-57f4/evidence/HD-003-HER-20260812-017-offline-verification.json`;
  `workspaces/ajiao/her_test_lab/runs/her-20260811T204652Z-767754/evidence/`.
- **Secrets/redaction checked:** yes; the permanent evidence contains no raw
  credential, authorization header, private prompt, or provider reasoning.
- **Suspected owner:** HER CLI tool executor output isolation for terminal-owned tools.
- **Root cause:** structured and other non-interactive command modes disabled HER's
  ordinary renderer but still invoked `AskUserQuestion` through the global terminal
  tool registry. That tool owns stdin/stdout and bypassed the structured event writer.
  Runtime accounting remained correct, but terminal UI bytes corrupted the JSONL
  framing around the emitted `tool_end`.
- **Known-bad commit/package:** source
  `69c88798237132da9874e5a300ab2c25e6fd9ae2`; HER `0.1.0-hashi.8` /
  `4f5f5dad26a208cef67f8f3ed84fbecaa0ed3d8164df22010ceb918a7456269b`.
- **Fix commits:** HER source
  `8f0f7344fbf64951619e44dd4c834aae57c29f53`; HASHI package
  `e171909a872a23cf2aabcfa575f10df8a7295e4e`.
- **Repair:** when output emission is disabled, intercept `AskUserQuestion` before its
  terminal implementation runs and return a normal `ToolError`. ConversationRuntime
  then emits the correlated error `tool_end`, records the result in the terminal
  ledger, and continues without reading stdin or printing terminal prompts. Interactive
  TTY behavior remains unchanged.
- **Regression test:** HER source
  `stream_json_ask_user_question_preserves_correlated_tool_events`.
- **Bad-build test result:** the deterministic RED selected one test and failed one
  test on source `69c88798237132da9874e5a300ab2c25e6fd9ae2`; an empty/non-JSON
  stdout line failed parsing before the correlated tool end could be observed.
- **Fixed-build test result:** all 5 mock-parity tests passed; full HER workspace
  certification passed with 1464 tests passed and 1 ignored; format, all-target Clippy
  with warnings denied, and release build passed. HASHI's 107 targeted HER/Superloop
  tests passed. Packaged HER `0.1.0-hashi.9` /
  `431876b9120be26e6ecaffa7f0f5b1dc4cebd2c8bf123c135996e71ffa0367f1`
  passed the exact local scripted reproduction: all stdout lines were JSON, stderr was
  empty, the single start/end ids matched, the end was an explicit tool error, and
  `run_finished` reported one use and one result.
- **Required live retest cells:** after explicit authorization, activate the frozen
  successor candidate and rerun the original Ajiao
  `HER-LIVE-DS-FLASH-FLEX-MAX/C02` failure first. Then restart Flash from the same
  candidate; Pro remains locked until Flash passes.
- **Live retest result:** the authorized successor-candidate retest ran in Superloop
  `sl-20260811-231651520023-b272`, attempt `HD-003-A002`, on candidate
  `b9da84597fc3409d6cf3b032a5becd6021258f374b1589cee0b886290bc5e7f9` via official
  DeepSeek `deepseek-v4-flash`, flex/max. The C02 transaction itself passed: the exact
  receipt returned, all three file/Git tools ran once in order, all three structured
  `tool_start` / `tool_end` pairs correlated, and `run_finished` reported three uses
  and three results. Because the C02 run emitted no `AskUserQuestion`, one bounded
  defect-only continuation invoked it exactly once without repeating the completed C02
  side effects. The raw ledger contains one `tool_call`, one same-id/name `tool_start`,
  and one same-id/name error `tool_end`; no `Enter choice` text, interactive prompt
  event, raw terminal write, retry, or other tool call occurred. Ajiao initially
  misclassified the expected error result, so that failed reply was preserved; a
  correction-only request then returned the exact reconciliation receipt with zero
  task tool calls. The exact live retest is therefore **PASS**. Evidence:
  `superloops/loops/sl-20260811-231651520023-b272/evidence/HD-003-A002-c02-pass-defect-inconclusive.json`;
  `superloops/loops/sl-20260811-231651520023-b272/evidence/HD-003-A002-her017-worker-failure-raw-pass.json`;
  `superloops/loops/sl-20260811-231651520023-b272/evidence/HD-003-A002-her017-reconciled-pass.json`.
- **Outstanding blast radius:** restart the Stage 1 Flash matrix on the same candidate
  from `HER-LIVE-DS-FLASH-FIXED-LOW/C00`; retain **Fixed — blast-radius verification
  pending** until the required same-route neighbor, other-mode twin, and Flash/offline
  gates pass. Pro remains locked.
- **Remaining risk:** other terminal-owned tools must preserve the same non-interactive
  isolation rule. Interactive TTY question behavior is intentionally outside this
  repair and remains enabled.

### HER-20260812-018 — controller nudge livelocked between packets of an in-progress task

- **Status:** Verified
- **Severity:** P1
- **Recurrence of:** none
- **Discovered:** 2026-08-12 14:17 AEST
- **Affected area:** HER debug Superloop liveness policy, instantiated controller state,
  and template validation.
- **Expected:** once the operator starts the campaign and the current phase task is
  `in_progress`, a verified terminal packet may select and dispatch exactly one next
  eligible packet after the normal worker, stage, candidate, wait, and duplicate-dispatch
  checks pass. Starting that next packet does not mark a new pending phase task in
  progress.
- **Actual:** after `HD-003-A002` reached a verified PASS, the controller selected
  `HER-LIVE-DS-FLASH-FIXED-LOW/C00` but persisted
  `pending_non_nudge_start_authority=true`. Forty-one later nudge observations found
  Ajiao online and idle with no active dispatch, yet only moved the next-check timestamp.
  The campaign remained `running` without executing any test packet.
- **Reproduction:** loop `sl-20260811-231651520023-b272`, event
  `loop-event-20260812041737000001`; current task `HD-003` is `in_progress`, the prior
  receipt is terminal PASS, the selected packet is unstarted, Ajiao is idle, and Pro is
  locked. The existing validator reported zero errors and warnings.
- **Root cause:** pending **phase-task** authority and continuation of an already
  authorized **in-progress task's packet queue** were treated as the same transition.
  The nudge policy prohibited the former but did not explicitly require the latter, and
  validation had no invariant rejecting the contradictory persisted authority flag.
- **Repair plan:** make the distinction explicit in the liveness contract; persist
  campaign-scoped operator continuation authority; reject an unstarted selected packet
  that requests fresh start authority while its owning task is already `in_progress`;
  and add a bounded no-progress rule that must dispatch, wait on a concrete blocker, or
  surface a validation finding instead of scheduling another identical observation.
- **Regression:**
  `tests/test_her_debug_superloop_template.py::test_in_progress_packet_continuation_cannot_require_new_start_authority`.
- **Fix:** the template now distinguishes pending phase-task activation from packet
  continuation inside an `in_progress` task, persists campaign-scoped execution
  authority at start, forbids fresh per-packet authority, and caps identical stagnant
  observations at three. The instantiated-loop validator now reports
  `in_progress_packet_authority_livelock` for the exact contradictory state that the
  prior validator accepted.
- **Offline verification:** the regression and six neighboring HER debug template tests
  passed; the template validator returned zero findings. Running the fixed validator
  against the preserved live state produced exactly the new livelock finding before the
  state repair.
- **Live verification:** after the controller selected and dispatched the formerly
  stranded packet, exact `HER-LIVE-DS-FLASH-FIXED-LOW/C00` passed as run
  `her-20260812T044436Z-32e47e`: HASHI request `req-0001`, one official-DeepSeek HTTP
  200 call, exact `deepseek-v4-flash` route/model, exact stream and visible output,
  terminal successful request activity, and monotonic activity sequence/timestamps.
  This closes the liveness defect; the evidence is defect closeout only, and the final
  same-candidate Flash matrix restarts after the journal commit. Pro remains locked.
- **Recurrence count:** 0

### HER-20260812-019 — live harness accepted a pre-restart runtime as the restart receipt

- **Status:** Verified
- **Severity:** P1
- **Recurrence of:** none
- **Discovered:** 2026-08-12 14:37 AEST
- **Reporter:** `lin_yueru@HASHI2`, `her_debug` controller
- **Batch / cell / scenario / run IDs:**
  `sl-20260811-231651520023-b272` / `HD-003` /
  `HER-LIVE-DS-FLASH-FIXED-LOW` / `C00` /
  `her-20260812T043252Z-e75db5`
- **Provider / model / mode / effort:** official DeepSeek /
  `deepseek-v4-flash` / fixed / low
- **Presentation policy:** thinking=true, verbose=true, typing=true
- **HASHI commit and dirty state:**
  `b308a46273e87c1cff35c33e7ad1bbdca814c27e`, clean when reproduced
- **HER package version / SHA-256:** `0.1.0-hashi.9` /
  `431876b9120be26e6ecaffa7f0f5b1dc4cebd2c8bf123c135996e71ffa0367f1`
- **HER source commit:** `8f0f7344fbf64951619e44dd4c834aae57c29f53`, clean when reproduced
- **Expected:** after requesting `/reboot min`, the live harness must observe a new
  Ajiao runtime-start marker and only then accept online+idle as the restart receipt.
  No provider request may be dispatched against the runtime being replaced.
- **Actual:** the harness requested the asynchronous restart at
  `2026-08-12T04:32:51.897749Z`, accepted the still-running old runtime as online+idle,
  marked setup complete at `04:32:52.894474Z`, and dispatched HASHI `req-0004` at
  `04:32:53.092214Z`. Ajiao's replacement runtime did not finish starting until
  `04:32:59.241298Z`; the queued request and its in-memory activity projection were
  discarded with the old runtime.
- **User-visible impact:** the first real-world certification packet appeared accepted
  but never reached HER or the provider. The controller then waited the full 300-second
  cell timeout for an activity record that could no longer exist.
- **First divergent event:** `wait_agent_online()` accepted online+idle without proving
  that `.runtime_session.json:last_started_at` had advanced past the pre-reboot marker.
- **Completion status / stop reason / provider stop reason:** no HER completion /
  controller timeout / provider not reached.
- **Session and request IDs:** HASHI `req-0004`; no HER session was created.
- **Tool calls / results / iterations:** 0 / 0 / 0.
- **Reproduction rate:** 1/1 cold restart-dispatch race.
- **Minimal reproduction:** record Ajiao's current runtime-start marker, issue
  `/reboot min`, immediately poll only online+idle, then enqueue a request before the
  marker changes. The old runtime accepts the request and is subsequently replaced.
- **Evidence bundle:**
  `superloops/loops/sl-20260811-231651520023-b272/evidence/HD-003-HER-20260812-019-reproduction.json`;
  `superloops/loops/sl-20260811-231651520023-b272/evidence/HD-003-HER-20260812-019-verification.json`;
  `workspaces/ajiao/her_test_lab/runs/her-20260812T044436Z-32e47e/evidence/verdict.json`.
- **Secrets/redaction checked:** yes; no credential, raw prompt, provider text, or
  private reasoning is persisted.
- **Suspected owner:** HER debug live harness restart handshake.
- **Root cause:** `/reboot min` acknowledges a restart request asynchronously. The
  harness treated generic readiness as completion and did not bind readiness to a new
  runtime generation/start marker.
- **Known-bad commit/package:** HASHI
  `b308a46273e87c1cff35c33e7ad1bbdca814c27e`; HER `0.1.0-hashi.9` /
  `431876b9120be26e6ecaffa7f0f5b1dc4cebd2c8bf123c135996e71ffa0367f1`.
- **Fix commits:** HASHI
  `e1677e1698b97ce16920cef756635820d98410de`.
- **Regression tests:**
  `tests/test_her_debug_restart_guard.py::test_restart_receipt_rejects_pre_restart_online_idle_runtime`.
- **Bad-build test result:** the preserved live timeline proves the known-bad predicate:
  old runtime online=true, idle=true, and unchanged start marker was accepted. The new
  deterministic regression rejects that exact state.
- **Fixed-build test result:** all three restart-guard tests passed; the 27-test HER
  debug focused suite passed; the operational runner compiled and its guarded restart
  advanced Ajiao's start marker before returning.
- **Required live retest cells:** guarded Ajiao restart followed by the exact
  `HER-LIVE-DS-FLASH-FIXED-LOW/C00` packet; the dropped pre-provider request does not
  count toward the final matrix.
- **Live retest result:** guarded restart requested at
  `2026-08-12T04:44:22.189148Z` and returned only after the start marker advanced from
  `2026-08-12T14:32:59.241298` to `2026-08-12T14:44:26.587044`, with Ajiao online and
  idle. The immediately following exact fixed/low C00 completed PASS as run
  `her-20260812T044436Z-32e47e`; provider trace count was one, status was HTTP 200,
  route/model and stream hashes were exact, and no funds signal was present.
- **Remaining risk:** every later harness-triggered restart must use the same generation
  receipt; generic online+idle remains insufficient. The final certification matrix is
  independently restarted after this journal closure.
- **Recurrence count:** 0

### HER-20260813-020 — MCP image results were flattened before provider translation

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **Recurrence of:** none
- **Discovered:** 2026-08-13 AEST during HASHI1 multimedia integration review
- **HASHI checkpoint:** `764258cf`; follow-up `ed5dcc9e`
- **Known-bad HER:** `0.1.0-hashi.9` /
  `431876b9120be26e6ecaffa7f0f5b1dc4cebd2c8bf123c135996e71ffa0367f1`
- **Fixed HER:** `0.1.0-hashi.10` /
  `882c9a71013bdd6155558ff4dc8df4a8e002188e144b04f7fda2fb96f0f83ac2`
  / source `85a481d9e5c94804ed9c0bd300ca9a635732c22d`
- **Expected:** validated MCP image content reaches the active provider's native
  multimodal message shape while normal session output stores no raw base64.
- **Actual:** `.9` serialized the MCP result and later collapsed tool output to
  one text block, so the model saw text rather than pixels.
- **User-visible impact:** HER could receive an image path or call a screenshot
  tool but could not reason over the returned image.
- **Root cause:** structured MCP image content was not preserved through HER's
  internal tool-result and provider-translation boundaries.
- **Fix:** `.10` preserves bounded private image content, emits Anthropic
  `tool_result` image blocks or ordered OpenAI-compatible image messages, and
  safely downgrades historical media.
- **Regression tests:** `tests/test_tool_gateway_mcp.py::test_packaged_her_bridges_media_read_image_into_provider_vision_input`
  plus the pinned HER workspace provider/media suite.
- **Offline result:** focused Python multimedia follow-up passed `81 passed, 2
  skipped`; HER workspace passed `1468 passed, 1 ignored`; strict Clippy and
  package certification passed.
- **Required live retest:** packaged HER through the real Gateway with one
  image, mixed PDF, and parallel tool results on each release provider route.
- **Remaining risk:** no live post-reboot rollout evidence is recorded yet.
- **Secrets/redaction checked:** yes; base64 is excluded from normal audit and
  session output.
- **Recurrence count:** 0

### HER-20260813-021 — legacy screenshot strings were not model-visible images

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **Recurrence of:** HER-20260813-020 at a compatibility boundary
- **Discovered:** 2026-08-13 AEST during browser/desktop/Windows screenshot review
- **HASHI fix:** `ed5dcc9e`
- **HER package:** `0.1.0-hashi.10` /
  `882c9a71013bdd6155558ff4dc8df4a8e002188e144b04f7fda2fb96f0f83ac2`
- **Expected:** reviewed screenshot tool return shapes are decoded, bounded,
  audited without base64, ordered after paired tool receipts, and exposed as MCP
  image content.
- **Actual:** legacy tools returned image/base64 inside strings, which bypassed
  the canonical structured-content bridge.
- **User-visible impact:** a screenshot tool could report success while HER saw
  only serialized metadata or base64 text.
- **Root cause:** compatibility normalization existed for canonical MCP content
  but not the established browser, desktop, Windows, and session screenshot
  string shapes.
- **Fix:** Gateway normalization recognizes only the reviewed shapes, validates
  media and limits, preserves order, and writes safe audit metadata.
- **Regression tests:**
  `test_gateway_bridges_legacy_browser_screenshot_string_to_image`,
  `test_gateway_bridges_legacy_desktop_and_session_screenshots_in_order`,
  `test_gateway_rejects_malformed_legacy_screenshot_payload`, and
  `test_gateway_bounds_legacy_browser_session_screenshot_count`.
- **Required live retest:** one real screenshot through each enabled legacy tool
  family after canary reboot, with provider-visible image evidence.
- **Remaining risk:** compatibility is shape-specific by design; unreviewed
  string formats remain text.
- **Secrets/redaction checked:** yes.
- **Recurrence count:** 0

### HER-20260813-022 — full-context modes can also resume the stored HER session

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **Recurrence of:** none
- **Discovered:** 2026-08-13 AEST during documentation/source consistency audit
- **Known-bad HASHI checkpoint:** `ed5dcc9e`
- **HER package:** `0.1.0-hashi.10`
- **Expected:** fixed mode uses incremental prompts plus `--resume`; Flex,
  Wrapper, Audit, and Dual Brain use HASHI-assembled full context without also
  resuming HER's previous internal conversation.
- **Actual:** `runtime_pipeline.build_turn_prompt()` assembles full context
  outside fixed mode, while `HERAdapter.generate_response()` passes its stored
  `_session_id` and the adapter has no `set_session_mode()` hook. Generic
  ephemeral HER construction also does not force `ephemeral_session`; only
  callers such as runtime-owned Meditation that pass the override are isolated.
- **User-visible impact:** repeated or conflicting context, unnecessary tokens,
  and potentially incorrect continuation in Flex/composed HER turns.
- **First divergent event:** mode selection disables sessions only on adapters
  implementing `set_session_mode()`; HER does not implement that method.
- **Reproduction:** source-path proof is deterministic; the regression now
  exercises a full-context adapter turn with a stale fixed checkpoint.
- **Root cause:** HER session persistence was added without integrating the
  runtime's fixed-versus-full-context session-mode contract.
- **Fix commits / regression:** `2270f5be`; HER now implements
  `set_session_mode()`, generic ephemeral construction forces
  `ephemeral_session`, and
  `test_her_full_context_session_mode_clears_stale_checkpoint` plus
  `test_her_full_context_turn_never_resumes_or_checkpoints_session` cover the
  checkpoint and turn boundaries.
- **Required retest:** fixed first/second turn proves session capture and
  incremental resume; Flex and each composed mode prove no `--resume`; mode
  switches and `/new` prove checkpoint cleanup; every health/helper/Meditation
  sidecar proves it neither loads nor overwrites the user session.
- **Remaining risk:** live provider verification across fixed, Flex, Wrapper,
  Audit, and Dual Brain remains pending before rollout certification.
- **Secrets/redaction checked:** yes; source inspection used no runtime secrets.
- **Recurrence count:** 0

### HER-20260813-023 — both HER Habit pipelines can process one run

- **Status:** Fixed — live verification pending
- **Severity:** P2
- **Recurrence of:** none
- **Discovered:** 2026-08-13 AEST during documentation/source consistency audit
- **Known-bad HASHI checkpoint:** `ed5dcc9e`
- **Expected:** one authoritative Habit Planning/Meditation policy processes a
  foreground HER run, or multiple policies have explicit, tested coordination.
- **Actual:** `runtime_pipeline` can inject/schedule the runtime-governed SQLite
  path while `HERAdapter.generate_response()` independently retrieves/schedules
  the adapter-direct JSON path when `habit_meditation.enabled` is true.
- **User-visible impact:** duplicate planning advice, two model calls, divergent
  writes, confusing `/skill habits` versus `/habit` state, and duplicate cost.
- **Root cause:** the later adapter-direct feature introduced an independent
  eligibility gate and store without a shared owner or mutual-exclusion rule.
- **Resolution:** the adapter-owned `/habit` path is authoritative. Standalone
  HASHI removed the legacy runtime writer; `HERAdapter.habit_pipeline_owner =
  "adapter"` also gives downstream compatibility runtimes an explicit
  suppression signal.
- **Fix commits / regression:** `2270f5be`;
  `test_her_adapter_declares_habit_pipeline_ownership` and
  `test_runtime_intake_ineligibility_disables_adapter_habit_pipeline`.
- **Required retest:** one foreground run proves exactly one Planning injection,
  one Meditation owner, one durable write policy, and correct no-change/failure
  behavior for every supported configuration.
- **Remaining risk:** live create/no-change/failure/recovery and Verbose
  notification evidence remains pending; storage migration is deliberately not
  performed.
- **Secrets/redaction checked:** yes; source inspection used no Habit contents.
- **Recurrence count:** 0

### HER-20260813-024 — Meditation isolation overrides were rejected before inference

- **Status:** Fixed — live verification pending
- **Severity:** P1
- **Recurrence of:** none
- **Discovered:** 2026-08-13 AEST by the session-mode regression run
- **Known-bad HASHI checkpoint:** `ed5dcc9e`
- **HER package:** `0.1.0-hashi.10`
- **Expected:** adapter-owned Meditation invokes HER in a non-resumable,
  read-only call with its bounded tool allowlist, disabled task planning, and an
  eight-iteration ceiling.
- **Actual:** `_run_habit_meditation()` passed `track_session_identity`,
  `permission_mode_override`, `allowed_tools_override`, and
  `task_env_overrides`, but the post-multimedia `_run_task_async()` signature no
  longer accepted them. A real Meditation job therefore raised `TypeError`
  before starting HER; mocked job tests did not exercise the concrete runner.
- **User-visible impact:** the foreground task still succeeded, but Meditation
  could not form or update a Habit, and only logs exposed the background
  failure.
- **Root cause:** the multimedia integration retained the hardened foreground
  task runner but dropped the previously implemented request-scoped override
  parameters and stream session-tracking flag.
- **Fix commits / regression:** `2270f5be` restores all four overrides while
  preserving `.10` broad-workzone and credential-redaction behavior;
  `test_her_task_runner_applies_meditation_safety_overrides` executes a real
  subprocess fixture and verifies the effective CLI arguments and environment.
- **Required retest:** enable `/habit`, complete one eligible HER request, and
  prove the journal reaches `no_change` or a validated Write with no foreground
  checkpoint mutation.
- **Remaining risk:** live provider and restart replay verification pending.
- **Secrets/redaction checked:** yes; the regression uses synthetic arguments
  and environment values only.
- **Recurrence count:** 0

### HER-20260813-025 — Debug Lab required machine-local operator state

- **Status:** Verified
- **Severity:** P2
- **Discovered:** 2026-08-13 AEST during the clean public-history regression
- **Expected:** offline HER Debug Lab scenarios run from a clean clone and do
  not copy private operator state into retained evidence.
- **Actual:** baseline capture unconditionally read local `ajiao` state,
  runtime preferences, and `agents.json`, and embedded a developer-specific HER
  source checkout path. A clean clone failed before every packaged scenario.
- **Root cause:** the original lab was built inside one configured HASHI2
  instance and treated optional operator inputs as required fixtures.
- **Resolution:** `66ce0ffe` accepts missing local state, fingerprints present
  state by presence plus SHA-256 without copying contents, treats `agents.json`
  as optional, and discovers an optional source checkout only through
  `HASHI_HER_SOURCE_ROOT`.
- **Regression:** `test_optional_operator_baseline_is_clone_portable_and_content_free`
  plus the complete `tests/test_her_debug_lab.py` selection (`15 passed`).
- **Clean integration retest:** the 24-file publication selection completed
  with `428 passed, 0 failed`.
- **Secrets/redaction checked:** yes; the new regression proves a private
  sentinel never appears in the retained baseline record.
- **Recurrence count:** 0

## New-entry template

Copy this section, replace every placeholder, and add the new ID to the index.

```markdown
### HER-YYYYMMDD-NNN — short symptom

- **Status:** New
- **Severity:** P0/P1/P2/P3
- **Recurrence of:** none / HER-...
- **Discovered:** YYYY-MM-DD HH:MM TZ
- **Reporter:**
- **Batch / cell / scenario / run IDs:**
- **Provider / model / mode / effort:**
- **Presentation policy:** thinking=?, verbose=?, typing=?
- **HASHI commit and dirty state:**
- **HER package version / SHA-256:**
- **HER source commit:**
- **Expected:**
- **Actual:**
- **User-visible impact:**
- **First divergent event:**
- **Completion status / stop reason / provider stop reason:**
- **Session and request IDs:**
- **Tool calls / results / iterations:**
- **Reproduction rate:** x/y cold, x/y warm
- **Minimal reproduction:**
- **Evidence bundle:**
- **Secrets/redaction checked:** yes/no
- **Suspected owner:** HASHI / HER / Tool Gateway / provider adapter / delivery / unknown
- **Root cause:**
- **Known-bad commit/package:**
- **Fix commits:**
- **Regression tests:**
- **Bad-build test result:**
- **Fixed-build test result:**
- **Required live retest cells:**
- **Live retest result:**
- **Remaining risk:**
- **Recurrence count:** 0
```

## Triage checklist

For every new symptom:

1. Freeze the run directory and hash the evidence files.
2. Confirm route, model, mode, effort, request ID, session ID, and package SHA.
3. Locate the first divergence among provider trace, HER JSONL, HASHI stream events,
   Tool Gateway audit, and delivery transcript.
4. Check whether workspace state proves the tool work happened once.
5. Scan diagnostics for prompt/secret canaries before sharing logs.
6. Reproduce cold once and warm once; stop early for P0 or unsafe duplication.
7. Reduce to an offline scripted-provider or scripted-tool regression.
8. Decide owner from evidence, not from where the symptom appeared.
9. Make the regression fail on the known-bad build before applying the repair.
10. After repair, run the affected cell, same-route other model, other-mode twin, other
    provider twin, and the full deterministic HER suite.

## Closure record

When an entry becomes `Verified`, append a dated closure note containing:

- immutable fix commits and package SHA;
- automated regression results;
- exact live retest cells and run IDs;
- whether HASHI full tests and HER certification passed;
- any deferred coverage or remaining risk.

If any required item is missing, use `Fixed, verification pending`, not `Verified`.
