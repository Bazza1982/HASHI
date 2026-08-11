# Aptenra KVM Functional Round Record

```text
round_id:
target_id:
main_commit:
packaging_commit:
source_checkout_clean:
candidate_id:
product_version:
product_code:
setup_sha256:
msi_sha256:
upgrade_from_candidate:
kvm_upgrade_result:
identity_and_launch_smoke:
user_state_preserved:
disk_free_before_gib:
disk_free_after_gib:

open_or_reopened_apbs_at_start:
new_or_changed_user_surfaces:
discovery_pass_complete: true|false
mandatory_cases_total:
mandatory_cases_passed:
mandatory_cases_failed:
mandatory_cases_blocked:
typed_five_turn_result:
voice_agent_push_five_turn_result:
voice_agent_wake_five_turn_result:
voice_chat_push_five_turn_result:
voice_chat_wake_five_turn_result:
voice_mixed_push_five_turn_result:
voice_mixed_wake_five_turn_result:
mixed_input_five_turn_result:
mode_switch_result:
cancel_and_delayed_result:
new_chat_reset_result:
stt_tts_result:
primary_action_escalation_result:
desktop_companion_result:
widgets_workbench_activity_result:
browser_result:
test_directory_file_result:
level_3_to_5_to_3_result:
restart_reboot_recovery_result:
program_files_runtime_writes:

new_apb_ids:
reopened_apb_ids:
duplicate_symptoms_merged_into:
environment_or_harness_incidents:
diagnosed_apb_ids:
fixed_in_main_apb_ids:
fix_commits:
automated_regressions:
permanent_kvm_regressions:
device_verified_apb_ids:
closed_apb_ids:
product_bug_register_updated: true|false

packaging_failure_ids:
packaging_journal_updated: true|false|not_applicable
conditional_lifecycle_tests:
failure_evidence_refs:
scratch_media_cleaned:
installer_cache_untouched: true|false
outcome: repeat|accepted|emergency_stop
```

For each functional case, append one compact row:

| Case | Pre-action marker | KVM action | Visible result | Request/log proof | Side effect | Verdict/APB |
| --- | --- | --- | --- | --- | --- | --- |

Allowed verdicts are `PASS`, `FAIL-APB-NNN`, `BLOCKED-BY-APB-NNN`, `NOT RUN`
and `ENVIRONMENT/HARNESS`. Secret values, PINs, tokens, recordings and full
credential fingerprints are forbidden in this record.
