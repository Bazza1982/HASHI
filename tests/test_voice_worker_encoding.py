from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import pytest

from orchestrator import voice_synthesis_worker as worker


def test_kokoro_cache_download_is_scoped_to_configured_voices(tmp_path, monkeypatch):
    calls = []

    def snapshot_download(**kwargs):
        calls.append(kwargs)
        snapshot = tmp_path / "snapshot"
        for relative in kwargs["allow_patterns"]:
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"ready")
        return str(snapshot)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )

    payload = worker.prepare_kokoro_assets(
        tmp_path / "cache",
        ("af_bella", "jm_kumo"),
        local_files_only=False,
    )

    assert calls == [
        {
            "repo_id": "hexgrad/Kokoro-82M",
            "revision": "f3ff3571791e39611d31c381e3a41a3af07b4987",
            "allow_patterns": [
                "config.json",
                "kokoro-v1_0.pth",
                "voices/af_bella.pt",
                "voices/jm_kumo.pt",
            ],
            "local_files_only": False,
        }
    ]
    assert payload["status"] == "ok"
    assert payload["voices"] == ["af_bella", "jm_kumo"]


@pytest.mark.parametrize("text", ["中文语音", "晓晓与晓伊，您好。🌸", "日本語と English"], ids=["chinese", "emoji", "mixed"])
def test_worker_preserves_utf8_input_despite_windows_pipe_encoding(tmp_path, monkeypatch, text):
    # An isolated Python 3.12 helper ignores PYTHONUTF8/PYTHONIOENCODING.
    # Its Windows pipe therefore starts as cp1252, while the caller sends UTF-8.
    stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="cp1252", errors="surrogateescape")
    output = io.BytesIO()
    stdout = io.TextIOWrapper(output, encoding="cp1252")
    monkeypatch.setattr(worker.sys, "stdin", stdin)
    monkeypatch.setattr(worker.sys, "stdout", stdout)
    received = []

    async def synthesize(actual, **kwargs):
        received.append(actual)
        return {"status": "ok", "text": actual}

    monkeypatch.setattr(worker, "synthesize", synthesize)
    assert worker.main(["--output", str(tmp_path / "speech.mp3"), "--voice", "zh-CN-XiaoxiaoNeural"]) == 0
    assert received == [text]
    result = output.getvalue().decode("utf-8")
    assert json.loads(result.removeprefix(worker.RESULT_PREFIX))["text"] == text


@pytest.mark.parametrize("payload, error", [(b"\xff", "UnicodeDecodeError"), (b" " * 4, "speech text is required"), ("中".encode("utf-8") * 20_001, "speech text is too long")], ids=["invalid-utf8", "blank", "too-long"])
def test_worker_rejects_invalid_or_excessive_input_before_synthesis(tmp_path, monkeypatch, payload, error):
    stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252", errors="surrogateescape")
    output = io.BytesIO()
    stdout = io.TextIOWrapper(output, encoding="cp1252")
    monkeypatch.setattr(worker.sys, "stdin", stdin)
    monkeypatch.setattr(worker.sys, "stdout", stdout)

    async def unexpected_synthesis(*args, **kwargs):
        pytest.fail("Rejected input must not reach synthesis")

    monkeypatch.setattr(worker, "synthesize", unexpected_synthesis)
    assert worker.main(["--output", str(tmp_path / "speech.mp3"), "--voice", "zh-CN-XiaoxiaoNeural"]) == 1
    result = json.loads(output.getvalue().decode("utf-8").removeprefix(worker.RESULT_PREFIX))
    assert result["status"] == "error"
    assert error in result["error"]


def test_worker_classifies_protocol_rejection_without_exposing_signed_url():
    class HandshakeError(RuntimeError):
        status = 403

    payload = worker._safe_error_payload(
        HandshakeError(
            "WSServerHandshakeError: 403, "
            "url='wss://speech.invalid/path?Sec-MS-GEC=not-a-real-secret'"
        )
    )

    assert payload == {
        "status": "error",
        "error_code": "tts_protocol_rejected",
        "error": (
            "Edge TTS rejected the speech request (HTTP 403). "
            "The installed Edge speech protocol may be out of date."
        ),
        "retryable": False,
    }
    assert "speech.invalid" not in payload["error"]
    assert "Sec-MS-GEC" not in payload["error"]
