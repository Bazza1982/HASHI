import base64

import pytest

from orchestrator.frontend_call.billing import MediaBilling, wav_seconds
from test_call_contract import wav


OPENROUTER = "https://openrouter.ai/api/v1"


def test_call_billing_uses_exact_stt_cost_and_estimates_tts_media_only():
    ledger = MediaBilling()
    stt = {
        "base_url": OPENROUTER,
        "model": "x-ai/grok-stt-1.0",
    }
    tts = {
        "base_url": OPENROUTER,
        "model": "google/gemini-3.8-flash-lite-tts",
    }

    ledger.record_stt(stt, 3.0, {"seconds": 3.0, "cost": 0.000084})
    ledger.record_tts(tts, "Hello 世界", {"audio_seconds": 2.0})
    view = ledger.view({"stt": stt, "tts": tts})

    assert view["scope"] == "voice_media"
    assert view["currency"] == "USD"
    assert view["estimated"] is True
    assert view["incomplete"] is False
    assert view["breakdown"]["stt"]["cost_usd"] == pytest.approx(0.000084)
    assert view["breakdown"]["stt"]["estimated"] is False
    assert view["breakdown"]["tts"]["audio_seconds"] == 2.0
    assert view["breakdown"]["tts"]["cost_usd"] > 0
    assert view["excludes"] == ["backend_text_model", "vision"]


def test_wav_duration_is_measured_from_valid_audio():
    assert wav_seconds(base64.b64decode(wav(0.25))) == pytest.approx(0.25)
