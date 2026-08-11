# Aptenra Packaging Fast-Loop Record

This is a compact result record, not a prebuild approval checklist.

## Round focus

```text
round_id:
latest_relevant_failure:
smallest_direct_avoidance:
failure_journal_path:
```

## Candidate identity

```text
candidate_id:
product_code:
product_commit:
packaging_commit:
media_directory:
msi_sha256:
```

## Actual installed observation

```text
actual_install_attempted:
install_mode: human_gui_usecomputer
validation_source: installed_msi
source_or_unpacked_substituted: false
aptenra_shortcut_launch_attempted:
aptenra_user_visible_launch_result:
workbench_shortcut_launch_attempted:
workbench_user_visible_launch_result:
basic_functions_result:
```

Source checks and unpacked payload runs may be attached as diagnostic notes,
but never populate the installed-observation result.

## Failure handling

```text
candidate_failed:
journal_updated:
journal_entry:
uninstall_completed:
cleanup_passed:
```

For a failed installation or installed launch, update the Journal immediately
and uninstall the exact candidate before opening the next round.

## Environment boundary

```text
user_environment_unchanged:
original_debug_runtime_unchanged:
```

Only the current candidate may be installed, stopped, repaired, or removed.
