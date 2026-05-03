from __future__ import annotations

import json
from types import SimpleNamespace

from apps.voice_whatsapp_desktop_runtime import _ocr_probe
from orchestrator.voice.events import VoiceEventLogger
from orchestrator.voice.windows_helper_client import WindowsHelperClient
from tools.windows_helper.whatsapp_call_probe import probe_whatsapp_call


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


def test_probe_treats_missed_voice_call_as_history_evidence(monkeypatch):
    monkeypatch.setattr("tools.windows_helper.whatsapp_call_probe._whatsapp_windows", lambda: [])
    monkeypatch.setattr("tools.windows_helper.whatsapp_call_probe._whatsapp_processes", lambda: [{"pid": 1234}])

    def fake_uia_probe(*_args, **_kwargs):
        return (
            [
                {
                    "source": "uia",
                    "kind": "missed_call",
                    "control_name": "Missed voice call",
                    "is_missed_call_signal": True,
                }
            ],
            False,
            {"uia_controls_visited": 42},
            None,
        )

    monkeypatch.setattr("tools.windows_helper.whatsapp_call_probe._uia_probe", fake_uia_probe)

    result = probe_whatsapp_call()

    assert result["detected"] is True
    assert result["active_call_detected"] is False
    assert result["missed_call_detected"] is True
    assert result["detection_method"] == "uia_missed_call"
    assert result["diagnostics"]["uia_controls_visited"] == 42


def test_probe_separates_active_call_from_missed_history(monkeypatch):
    monkeypatch.setattr(
        "tools.windows_helper.whatsapp_call_probe._whatsapp_windows",
        lambda: [{"title": "Incoming voice call", "id": 1, "pid": 1234}],
    )
    monkeypatch.setattr("tools.windows_helper.whatsapp_call_probe._whatsapp_processes", lambda: [])

    result = probe_whatsapp_call(use_uia=False)

    assert result["detected"] is True
    assert result["active_call_detected"] is True
    assert result["missed_call_detected"] is False
    assert result["detection_method"] == "window_title"


def test_ocr_probe_detects_active_call_text(monkeypatch, tmp_path):
    class FakeClient:
        def action(self, action, args):
            assert action == "screenshot"
            assert "save_path" in args
            return {"text": "screenshot ok"}

    monkeypatch.setattr("apps.voice_whatsapp_desktop_runtime.shutil.which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(
        "apps.voice_whatsapp_desktop_runtime.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="Barry Incoming voice call Answer Decline",
            stderr="",
            returncode=0,
        ),
    )

    result = _ocr_probe(FakeClient(), tmp_path / "screen.png", timeout=1.0)

    assert result["detected"] is True
    assert result["active_call_detected"] is True
    assert result["missed_call_detected"] is False
    assert result["signals"][0]["source"] == "visual_ocr"


def test_ocr_probe_detects_missed_call_text(monkeypatch, tmp_path):
    class FakeClient:
        def action(self, action, args):
            return {"text": "screenshot ok"}

    monkeypatch.setattr("apps.voice_whatsapp_desktop_runtime.shutil.which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(
        "apps.voice_whatsapp_desktop_runtime.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="Barry Missed voice call",
            stderr="",
            returncode=0,
        ),
    )

    result = _ocr_probe(FakeClient(), tmp_path / "screen.png", timeout=1.0)

    assert result["detected"] is True
    assert result["active_call_detected"] is False
    assert result["missed_call_detected"] is True
    assert result["signals"][0]["kind"] == "missed_call"
