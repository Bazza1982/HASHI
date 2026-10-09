from __future__ import annotations

import io
import json

import pytest

from orchestrator import voice_synthesis_worker as worker


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
