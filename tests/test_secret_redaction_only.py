"""Guardrail: redact real secrets/tokens while preserving readable local diagnostics.

Replaces the deleted *_failure_details blind-box tests: credentials must never
leak, while local owners must still receive actionable error messages and paths.
"""
from __future__ import annotations

import logging

from adapters.codex_event_log import redact_codex_diagnostic_text
from orchestrator.bootstrap_logging import (
    CredentialRedactingFormatter,
    redact_log_text,
)
from orchestrator.commands.remote import redact_inline_token_args


def test_log_redaction_strips_bearer_bot_tokens_api_keys_and_query_secrets():
    bearer = "eyJhbGciOiJIUzI1NiJ9.super_secret_payload_123456"
    bot_token = "123456789:AAH_abcdefghijklmnopqrstuvwxyz1234"
    api_key = "sk-live-secret1234567890abcdef"
    query_secret = "tok_super_sensitive_value_99"
    passwd = "MySuperSecretPassword!99"

    raw = (
        f"HTTP 401 Authorization: Bearer {bearer}; "
        f"bot={bot_token}; "
        f"provider_key={api_key}; "
        f"url=https://api.example.invalid/v1/chat?access_token={query_secret}&ok=1; "
        f"password={passwd}"
    )

    redacted = redact_log_text(raw)

    for leaked in (bearer, bot_token, api_key, query_secret, passwd):
        assert leaked not in redacted, f"Secret leaked in redacted output: {redacted!r}"
    assert "HTTP 401" in redacted
    assert "ok=1" in redacted


def test_log_redaction_preserves_local_paths_and_actionable_diagnostics():
    diagnostic = (
        "PermissionError: cannot open C:\\Users\\owner\\projects\\hashi\\config.json "
        "or /home/owner/workspace/runtime.py at line 42 (errno=13)"
    )

    redacted = redact_log_text(diagnostic)

    assert "PermissionError" in redacted
    assert "C:\\Users\\owner\\projects\\hashi\\config.json" in redacted
    assert "/home/owner/workspace/runtime.py" in redacted
    assert "line 42" in redacted
    assert "errno=13" in redacted


def test_credential_redacting_formatter_scrubs_formatted_records_but_keeps_context():
    formatter = CredentialRedactingFormatter("%(levelname)s %(message)s")
    secret = "sk-proj-verysecretkeyvalue987654321"
    record = logging.LogRecord(
        name="hashi.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=10,
        msg="Upstream failed for /opt/hashi/worker.py with key %s (HTTP 502)",
        args=(secret,),
        exc_info=None,
    )

    rendered = formatter.format(record)

    assert secret not in rendered
    assert "ERROR" in rendered
    assert "/opt/hashi/worker.py" in rendered
    assert "HTTP 502" in rendered


def test_inline_remote_token_arguments_are_redacted_before_persistence():
    raw_token = "hashi_remote_secret_token_abcdef123456"

    redacted_set = redact_inline_token_args("remote", ["token", "set", raw_token])
    assert raw_token not in " ".join(redacted_set)
    assert redacted_set[:2] == ["token", "set"]

    redacted_eq = redact_inline_token_args("remote", ["token", f"set={raw_token}"])
    assert raw_token not in " ".join(redacted_eq)

    redacted_colon = redact_inline_token_args("remote", ["token", f"set:{raw_token}"])
    assert raw_token not in " ".join(redacted_colon)

    # Non-secret commands remain readable and untouched.
    safe_args = redact_inline_token_args("remote", ["status", "peer-alpha"])
    assert safe_args == ["status", "peer-alpha"]


def test_codex_diagnostic_redaction_hides_tokens_but_keeps_error_context():
    secret = "sk-codex-secret-9988776655443322"
    raw = f"codex exec failed at /tmp/work/main.py: Authorization: Bearer {secret}"
    redacted = redact_codex_diagnostic_text(raw)

    assert secret not in redacted
    assert "codex exec failed" in redacted
    assert "/tmp/work/main.py" in redacted
