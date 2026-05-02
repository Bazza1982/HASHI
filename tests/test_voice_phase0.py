from __future__ import annotations

import json

from orchestrator.voice.events import VoiceEventLogger
from orchestrator.voice.windows_helper_client import WindowsHelperClient


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_windows_helper_client_parses_json_output(monkeypatch):
    def fake_urlopen(_req, timeout):
        assert timeout == 5.0
        return _FakeResponse(
            {
                "ok": True,
                "request_id": "req-1",
                "elapsed_ms": 12.5,
                "output": json.dumps({"detected": True, "signals": [{"source": "uia"}]}),
            }
        )

    monkeypatch.setattr("orchestrator.voice.windows_helper_client.request.urlopen", fake_urlopen)

    result = WindowsHelperClient().action("whatsapp_call_probe", {"auto_answer": False})

    assert result["detected"] is True
    assert result["signals"] == [{"source": "uia"}]
    assert result["_helper_request_id"] == "req-1"
    assert result["_helper_elapsed_ms"] == 12.5


def test_voice_event_logger_writes_jsonl(tmp_path):
    log_path = tmp_path / "voice.jsonl"
    logger = VoiceEventLogger(log_path)

    logger.write("call.incoming_detected", detection_method="uia", detection_latency_ms=1200)

    [line] = log_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["event"] == "call.incoming_detected"
    assert record["detection_method"] == "uia"
    assert record["detection_latency_ms"] == 1200
    assert isinstance(record["ts"], float)
