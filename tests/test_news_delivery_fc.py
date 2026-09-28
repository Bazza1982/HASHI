from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_news_relay_publishes_text_and_voice_through_fc(tmp_path, monkeypatch):
    from orchestrator import frontend_telegram_connector
    from tools import news_deliver_to_sunny as news

    (tmp_path / "agents.json").write_text(
        json.dumps(
            {
                "global": {"instance_id": "HASHI1"},
                "agents": [
                    {
                        "name": "sunny",
                        "telegram_token_key": "sunny-token",
                        "agent_lifecycle_id": "a" * 32,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {
                "sunny-token": "test-token",
                "_authorized_telegram_id": 7,
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "2026-09-26_07-05-00.md"
    output.write_text("news", encoding="utf-8")
    voice = tmp_path / "news.ogg"
    voice.write_bytes(b"OggS-news")
    publish = AsyncMock(
        side_effect=[
            {"accepted": True, "state": "delivered", "event_id": "evt_text"},
            {"accepted": True, "state": "delivered", "event_id": "evt_voice"},
        ]
    )
    monkeypatch.setattr(news, "HASHI_ROOT", tmp_path)
    monkeypatch.setenv("HASHI_NEWS_CHAT_ID", "7")
    monkeypatch.setattr(
        frontend_telegram_connector,
        "publish_explicit_telegram_notification",
        publish,
    )

    result = await news.publish_news_via_fc(
        job_id="c2fcf85a5c81",
        output_file=output,
        text="morning news",
        media_paths=[voice],
        force=False,
    )

    assert [item["event_id"] for item in result] == ["evt_text", "evt_voice"]
    assert publish.await_count == 2
    text_call = publish.await_args_list[0].kwargs
    voice_call = publish.await_args_list[1].kwargs
    assert text_call["text"] == "morning news"
    assert "file_path" not in text_call
    assert voice_call["file_path"] == voice
    assert voice_call["semantic_role"] == "voice_message"
    assert text_call["publication_id"].startswith("news-text-")
    assert voice_call["publication_id"].startswith("news-media-")
