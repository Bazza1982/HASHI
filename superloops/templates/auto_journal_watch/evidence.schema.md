# Auto Journal Watch Evidence Schema

Every evidence bundle should be artifact-first. A narrative claim is not enough
unless it points to a concrete file, command output, receipt, or record.

## Run Charter

- `loop_id`
- `created_at`
- `watch_window_start`
- `watch_window_end`
- `journal_scope`
- `discovery_mode`
- `sop_sources`
- `orchestrator_agent`
- `librarian_agent`
- `reviewer_agent`
- `human_approver`
- `exit_condition`

## Dispatch Record

- `dispatch_id`
- `target_role`
- `target_agent`
- `status`: `dispatch_prepared`, `dispatch_delivered`, `reply_received`, or
  `reply_reviewed`
- `message_path`
- `delivery_evidence`
- `reply_path`
- `classified_at`

## Batch Preflight

- `manifest_path`
- `manifest_rows`
- `completed_before`
- `planned_count`
- `planned_targets`
- `output_root`
- `route_distribution`
- `fail_fast_checks`

## Heartbeat

- `task_id`
- `owner`
- `pid_or_session`
- `current_item`
- `completed`
- `failed`
- `skipped`
- `pending`
- `log_path`
- `status_path`
- `updated_at`

## Duplicate Audit

- `selected_rows`
- `unique_papers`
- `unique_pdfs`
- `duplicate_groups`
- `normalization_rules`
- `resolved_action`

## Download Reconciliation

- `selected_rows`
- `already_have`
- `downloaded`
- `manual_supplied`
- `pending`
- `failed`
- `valid_pdf_headers_checked`
- `file_paths`

## Processing Reconciliation

- `selected_rows_covered`
- `unique_pdf_targets`
- `raw_markdown_count`
- `sectioned_markdown_count`
- `zotero_attachment_count`
- `zotero_note_count`
- `search_docs`
- `search_chunks`
- `search_errors`

## Review Record

- `review_id`
- `reviewer_agent`
- `scope`
- `artifacts_reviewed`
- `findings`
- `blockers`
- `non_blockers`
- `follow_ups`
- `recommendation`

