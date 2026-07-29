# Aptenra Local Packaging Debug Evidence Schema

## Loop Header

```text
loop_id:
template: aptenra_local_packaging_debug
max_rounds: 30
current_round:
scheduler_auto_advance: false
liveness_nudge_id:
product_repo:
product_main_commit:
packaging_repo:
packaging_commit:
failure_journal:
failure_journal_commit:
original_debug_runtime_identity:
```

## Mandatory Round Reflection

```text
round_id:
started_at:
what_mistake_did_i_make_last_time:
how_to_avoid_it_most_straightforwardly_this_round:
what_is_materially_different_from_last_round:
known_failure_registry_count:
journal_pfj_count:
missing_registry_ids:
known_signature_detected:
candidate_build_allowed:
candidate_install_allowed:
```

No source change, build or install may precede this record.

## Failure And Diagnosis

```text
candidate_id:
product_code:
product_commit:
packaging_commit:
msi_sha256:
media_directory:
installed_at:
failed_at:
user_visible_symptom:
expected_behavior:
actual_behavior:
actual_gui_or_terminal_evidence:
logs:
process_inventory:
listener_inventory:
state_identity:
credential_precondition:
root_cause:
responsibility_layer:
known_or_new_signature:
matching_pfj_id:
```

## Journal Update

```text
new_pfj_id:
journal_path:
journal_commit:
source_defect_status:
failed_media_status:
regression_gate_status:
permanent_prevention_gate:
```

## Failed Candidate Uninstall

```text
candidate_owned_processes_before:
candidate_owned_processes_stopped:
ownership_evidence:
uninstall_mode:
uninstall_started_at:
uninstall_finished_at:
uninstall_exit_code:
product_registration_after:
shortcuts_after:
services_after:
program_files_residue_after:
candidate_processes_after:
candidate_listeners_after:
pending_delete_entries_after:
user_state_policy_result:
original_debug_runtime_unchanged:
cleanup_evidence_paths:
```

## Repair Plan

```text
last_round_difference:
correct_responsibility_layer:
files_in_scope:
files_out_of_scope:
minimal_fix:
why_this_prevents_last_mistake:
rollback_path:
new_test_or_gate:
review_verdict:
build_go_no_go:
```

## Prebuild Gate

```text
gate_results_by_pfj:
product_main_commit:
product_worktree_clean:
packaging_worktree_clean:
new_state_root:
state_root_absent_before:
credential_directory_absent_before:
borrowed_state_or_credentials: false
unpacked_entry_actual_launch:
usable_ui_or_authorised_first_run:
host_hashi_primary:
workbench_agents:
workbench_transcript_status:
workbench_poll_status:
real_response:
injected_failure_rollback:
orphan_process_count:
retry_result:
proof_generator_exit_code:
proof_observation_refs:
proof_hash:
prebuild_gate_passed:
```

## Candidate Media

```text
candidate_id:
version:
product_code:
upgrade_code:
product_commit:
packaging_commit:
payload_inventory_hash:
msi_sha256:
cab_hashes:
manifest_hash:
proof_hash:
media_file_count:
atomic_directory:
ice_result:
decompile_result:
shortcut_results:
icon_result:
target_preflight:
verbose_install_log_path:
install_allowed:
```

## Actual Installed Validation

```text
actual_install_attempted: true
install_mode: human_gui_usecomputer
usecomputer_request_or_event_ref:
interactive_window_observed:
install_started_at:
install_finished_at:
msi_exit_code:
msi_log:
apps_features_registration:
installed_manifest_match:
installed_shortcut_actual_launch_attempted: true
launch_started_at:
aptenra_shortcut_launch_attempted: true
aptenra_window_visible:
workbench_shortcut_launch_attempted: true
workbench_window_visible:
provider_credentials_used: false
launch_finished_at:
user_visible_launch_result: success | failure
window_or_error_observed:
launch_evidence:
host_result:
hashi_result:
primary_result:
aptenra_ui_result:
workbench_result:
real_response_result:
basic_functions_passed:
stop_result:
cold_restart_result:
repair_attempted:
repair_exit_code:
post_repair_launch_result:
post_repair_response_result:
actual_uninstall_attempted: true
uninstall_exit_code:
residue_audit:
original_debug_runtime_unchanged:
```

An installed validation record without `actual_install_attempted=true`,
`install_mode=human_gui_usecomputer` and
`installed_shortcut_actual_launch_attempted=true` is invalid.

## Round Decision

```text
round_outcome: lifecycle_accepted | fail_new | fail_known | await_human
candidate_disposition:
known_signature_recurrence:
failed_regression_gate:
failure_journal_updated:
failed_candidate_uninstalled:
cleanup_passed:
next_round:
closeout_status:
remaining_risk:
decided_at:
```

For `fail_new` or `fail_known`, `failed_candidate_uninstalled` and
`cleanup_passed` must be true before the next round can build.
