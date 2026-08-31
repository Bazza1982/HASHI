from __future__ import annotations

import json

from adapters.codex_errors import parse_codex_failure
from adapters.codex_event_log import CodexEventLogWriter, sanitise_codex_event_line


def test_parse_nested_codex_bad_request_preserves_provider_status():
    event = {
        "type": "turn.failed",
        "error": {
            "message": json.dumps(
                {
                    "type": "error",
                    "status": 400,
                    "error": {
                        "type": "invalid_request_error",
                        "message": (
                            "The 'gpt-example' model is not supported when using "
                            "Codex with this account."
                        ),
                    },
                }
            )
        },
    }

    failure = parse_codex_failure(event)

    assert failure.message.startswith("The 'gpt-example' model is not supported")
    assert failure.code == "PROVIDER_MODEL_UNAVAILABLE"
    assert failure.retryable is False
    assert failure.http_status == 400


def test_parse_codex_detail_only_bad_request_from_real_cli_shape():
    failure = parse_codex_failure(
        {
            "type": "turn.failed",
            "error": {"message": '{"detail":"Bad Request"}'},
        }
    )

    assert failure.message == "Bad Request"
    assert failure.code == "PROVIDER_BAD_REQUEST"
    assert failure.http_status == 400
    assert failure.retryable is False


def test_parse_codex_auth_error_extracts_request_id():
    failure = parse_codex_failure(
        {
            "type": "turn.failed",
            "error": {
                "message": (
                    "unexpected status 401 Unauthorized: Missing bearer authentication, "
                    "request id: req_abc123"
                )
            },
        }
    )

    assert failure.code == "PROVIDER_AUTHENTICATION_FAILED"
    assert failure.http_status == 401
    assert failure.provider_request_id == "req_abc123"
    assert failure.retryable is False


def test_parse_codex_rate_limit_honours_numeric_retry_after():
    failure = parse_codex_failure(
        {
            "type": "turn.failed",
            "error": {
                "message": "Too many requests; try again in 1.5 minutes.",
                "retry_after_s": 12,
            },
        }
    )

    assert failure.code == "PROVIDER_RATE_LIMITED"
    assert failure.retry_after_s == 12
    assert failure.retryable is True


def test_parse_codex_capacity_and_safety_text_override_generic_http_statuses():
    capacity = parse_codex_failure(
        {
            "type": "turn.failed",
            "error": {
                "status": 429,
                "message": "Selected model is at capacity. Try another model.",
            },
        }
    )
    safety = parse_codex_failure(
        {
            "type": "turn.failed",
            "error": {
                "status": 403,
                "message": "Request was rejected under the safety policy.",
            },
        }
    )

    assert capacity.code == "PROVIDER_CAPACITY_UNAVAILABLE"
    assert capacity.http_status == 429
    assert safety.code == "PROVIDER_SAFETY_REJECTED"
    assert safety.http_status == 403


def test_parse_codex_quota_reset_without_numeric_delay_blocks_immediate_retry():
    failure = parse_codex_failure(
        {
            "type": "turn.failed",
            "error": {
                "message": (
                    "You've hit your usage limit. Purchase more credits or try "
                    "again at 5:30 PM."
                )
            },
        }
    )

    assert failure.code == "PROVIDER_QUOTA_EXHAUSTED"
    assert failure.retry_after_s is None
    assert failure.retryable is False


def test_codex_event_sanitizer_redacts_credentials_and_personal_paths():
    synthetic_key = "sk-" + "example0123456789"
    safe = sanitise_codex_event_line(
        json.dumps(
            {
                "type": "item.completed",
                "token": "secret-token",
                "item": {
                    "type": "command_execution",
                    "command": (
                        f"curl -H 'Authorization: Bearer {synthetic_key}' "
                        "/home/example-user/private/file"
                    ),
                    "changes": [
                        {
                            "path": "/home/example-user/private/file",
                            "kind": "update",
                        }
                    ],
                },
            }
        )
    )

    decoded = json.loads(safe)
    assert decoded["token"] == "[REDACTED]"
    assert "secret-token" not in safe
    assert synthetic_key not in safe
    assert "/home/example-user" not in safe
    assert decoded["item"]["command"]["redacted"] is True
    assert decoded["item"]["changes"][0]["path"] == "$HOME/private/file"


def test_codex_event_log_bounds_and_sanitizes_oversized_legacy_file(tmp_path):
    path = tmp_path / "codex_exec_events.jsonl"
    legacy = json.dumps(
        {
            "type": "item.completed",
            "password": "plain-secret",
            "item": {"type": "command_execution", "output": "x" * 90_000},
        }
    )
    path.write_text(legacy + "\n", encoding="utf-8")

    with CodexEventLogWriter(
        path,
        max_bytes=64 * 1024,
        backup_count=1,
        max_event_bytes=8 * 1024,
    ) as writer:
        writer.append(json.dumps({"type": "turn.completed"}))

    backup = tmp_path / "codex_exec_events.jsonl.1"
    assert path.stat().st_size <= 64 * 1024
    assert backup.stat().st_size <= 64 * 1024
    assert "plain-secret" not in backup.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["type"] == "turn.completed"


def test_codex_event_log_bounds_non_json_content_and_oversized_event_setting(tmp_path):
    path = tmp_path / "codex_exec_events.jsonl"
    raw = "model output that must not persist verbatim " + ("x" * 100_000)

    with CodexEventLogWriter(
        path,
        max_bytes=64 * 1024,
        backup_count=0,
        max_event_bytes=1024 * 1024,
    ) as writer:
        writer.append(raw)

    persisted = path.read_text(encoding="utf-8")
    decoded = json.loads(persisted)
    assert path.stat().st_size <= 64 * 1024
    assert "model output that must not persist verbatim" not in persisted
    assert decoded["content"]["redacted"] is True
