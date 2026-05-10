from __future__ import annotations

from orchestrator.runtime_common import QueuedRequest, _md_to_html, _safe_excerpt, resolve_authorized_telegram_ids


def test_safe_excerpt_compacts_and_truncates():
    assert _safe_excerpt("a   b   c", limit=20) == "a b c"
    assert _safe_excerpt("x" * 12, limit=10) == "xxxxxxx..."


def test_md_to_html_preserves_code_and_formats_headings():
    text = "# Title\nUse `code`.\n```py\nx < 1\n```"
    html = _md_to_html(text)
    assert "<b>Title</b>" in html
    assert "<code>code</code>" in html
    assert "<pre>x &lt; 1\n</pre>" in html


def test_resolve_authorized_telegram_ids_filters_invalid_and_deduplicates():
    assert resolve_authorized_telegram_ids({"authorized_telegram_ids": ["123", None, "123", "bad", 0, 456]}, 999) == (
        123,
        456,
    )
    assert resolve_authorized_telegram_ids({}, 999) == (999,)


def test_queued_request_defaults_match_runtime_expectations():
    item = QueuedRequest(
        request_id="req-0001",
        chat_id=123,
        prompt="hello",
        source="text",
        summary="summary",
        created_at="2026-05-09T23:00:00+10:00",
    )
    assert item.silent is False
    assert item.is_retry is False
    assert item.deliver_to_telegram is True
    assert item.active_habits is None
    assert item.skip_memory_injection is False
