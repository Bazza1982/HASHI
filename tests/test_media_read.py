from __future__ import annotations

import base64
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from tools.gateway.context import load_gateway_context, write_gateway_context
from tools.gateway.mcp_stdio import ToolGateway
from tools.media_read import MediaProbe, MediaReadError, _probe_av, execute_media_read
from tools.ocr import OCRResult
from tools.registry import StructuredToolOutput, ToolRegistry


def _registry(root: Path, *, media_roots: list[Path] | None = None) -> ToolRegistry:
    return ToolRegistry(
        allowed_tools=["media_read"],
        access_root=root,
        workspace_dir=root,
        secrets={},
        media_roots=media_roots,
    )


def test_media_read_is_not_accidentally_enabled_by_non_her_wildcard(tmp_path):
    registry = ToolRegistry(["*"], tmp_path, tmp_path, {})

    assert registry.is_allowed("file_read")
    assert not registry.is_allowed("media_read")


@pytest.mark.asyncio
async def test_media_read_normalizes_image_to_mcp_image_without_bytes_in_output(tmp_path):
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (40, 30), (255, 0, 0, 80)).save(path)

    result = await _registry(tmp_path).execute(
        "media_read", {"path": str(path), "ocr_mode": "off"}, "call-1"
    )

    assert result.is_error is False
    assert result.content is not None
    assert result.content[0]["type"] == "text"
    image = next(block for block in result.content if block["type"] == "image")
    assert image["mimeType"] == "image/jpeg"
    assert base64.b64decode(image["data"]).startswith(b"\xff\xd8\xff")
    assert image["data"] not in result.output
    assert "normalized=40x30" in result.output
    audit = json.loads(
        (tmp_path / "tool_action_audit.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert audit["tool_name"] == "media_read"
    assert image["data"] not in json.dumps(audit)


@pytest.mark.asyncio
async def test_media_read_adds_bounded_multilingual_ocr_before_the_image(tmp_path, monkeypatch):
    path = tmp_path / "medicine.png"
    Image.new("RGB", (40, 30), "white").save(path)
    monkeypatch.setattr(
        "tools.media_read.extract_image_bytes",
        lambda *_args, **_kwargs: OCRResult(
            status="ok",
            text="感冒薬 약 دواء médicament Medikament",
            requested_languages=("chi_sim", "jpn", "kor", "ara", "fra", "deu"),
            used_languages=("chi_sim", "jpn", "kor", "ara", "fra", "deu"),
        ),
    )

    result = await execute_media_read(
        {
            "path": str(path),
            "ocr_languages": ["chi_sim", "jpn", "kor", "ara", "fra", "deu"],
        },
        access_root=tmp_path,
        workspace_dir=tmp_path,
        media_roots=[],
    )

    assert isinstance(result, StructuredToolOutput)
    assert [block["type"] for block in result.content] == ["text", "text", "image"]
    assert "感冒薬 약 دواء médicament Medikament" in result.content[1]["text"]
    assert "ocr_status=ok" in result.output
    assert "感冒薬" not in result.output


@pytest.mark.asyncio
async def test_media_read_required_ocr_fails_closed_when_models_are_missing(tmp_path, monkeypatch):
    path = tmp_path / "medicine.png"
    Image.new("RGB", (40, 30), "white").save(path)
    monkeypatch.setattr(
        "tools.media_read.extract_image_bytes",
        lambda *_args, **_kwargs: OCRResult(
            status="unavailable",
            requested_languages=("chi_tra",),
            missing_languages=("chi_tra",),
            error="language model missing",
        ),
    )

    result = await execute_media_read(
        {"path": str(path), "ocr_mode": "required", "ocr_languages": ["chi_tra"]},
        access_root=tmp_path,
        workspace_dir=tmp_path,
        media_roots=[],
    )

    assert isinstance(result, str)
    assert result.startswith("Error: media_read failed: required OCR")


@pytest.mark.asyncio
async def test_media_read_rejects_extension_mismatch_and_symlink_escape(tmp_path):
    disguised = tmp_path / "not-really.png"
    disguised.write_bytes(b"%PDF-1.7\n")
    mismatch = await _registry(tmp_path).execute("media_read", {"path": str(disguised)})
    assert mismatch.is_error is True
    assert "does not match" in mismatch.output

    access = tmp_path / "access"
    outside = tmp_path / "outside"
    access.mkdir()
    outside.mkdir()
    target = outside / "secret.png"
    Image.new("RGB", (8, 8), "blue").save(target)
    link = access / "escape.png"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    escaped = await _registry(access).execute("media_read", {"path": str(link)})
    assert escaped.is_error is True
    assert "outside the allowed" in escaped.output


@pytest.mark.asyncio
async def test_media_read_accepts_explicit_agent_media_root(tmp_path):
    workspace = tmp_path / "workspace"
    media = tmp_path / "media" / "zelda"
    workspace.mkdir()
    media.mkdir(parents=True)
    path = media / "photo.jpg"
    Image.new("RGB", (12, 9), "green").save(path)

    result = await _registry(workspace, media_roots=[media]).execute(
        "media_read", {"path": str(path), "ocr_mode": "off"}
    )

    assert result.is_error is False
    assert result.content and result.content[-1]["type"] == "image"


@pytest.mark.asyncio
async def test_media_read_extracts_pdf_text_and_renders_scanned_page(tmp_path):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "mixed.pdf"
    document = fitz.open()
    text_page = document.new_page()
    text_page.insert_text((72, 72), "HER multimedia text layer")
    document.new_page()
    document.save(path)
    document.close()

    result = await _registry(tmp_path).execute(
        "media_read", {"path": str(path), "ocr_mode": "off"}
    )

    assert result.is_error is False
    assert result.content is not None
    assert any("HER multimedia text layer" in block.get("text", "") for block in result.content)
    assert any(block.get("type") == "image" for block in result.content)
    assert "rendered_pages=[2]" in result.output


@pytest.mark.asyncio
async def test_audio_path_uses_normalized_transcription(tmp_path, monkeypatch):
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"OggS" + b"\x00" * 64)

    monkeypatch.setattr(
        "tools.media_read._probe_av",
        lambda _path: MediaProbe(
            kind="audio",
            format_name="ogg",
            duration=2.5,
            has_audio=True,
        ),
    )

    async def transcribe(_path):
        return "normalized transcript"

    monkeypatch.setattr("tools.media_read._transcribe_normalized", transcribe)
    result = await execute_media_read(
        {"path": str(path)},
        access_root=tmp_path,
        workspace_dir=tmp_path,
        media_roots=[],
    )

    assert isinstance(result, StructuredToolOutput)
    assert any("normalized transcript" in block.get("text", "") for block in result.content)


def test_media_probe_rejects_nonfinite_duration(tmp_path, monkeypatch):
    path = tmp_path / "invalid-duration.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr("tools.media_read.shutil.which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        "tools.media_read._run_process",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout='{"format":{"duration":"NaN","format_name":"mp4"},'
            '"streams":[{"codec_type":"video"}]}',
            stderr="",
        ),
    )

    with pytest.raises(MediaReadError, match="missing or invalid"):
        _probe_av(path)


@pytest.mark.asyncio
async def test_audio_fallback_really_normalizes_before_transcription(tmp_path, monkeypatch):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe are unavailable")
    path = tmp_path / "voice.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(8_000)
        audio.writeframes(b"\x00\x00\x00\x00" * 4_000)

    class _Transcriber:
        async def transcribe(self, normalized):
            with wave.open(str(normalized), "rb") as audio:
                assert audio.getnchannels() == 1
                assert audio.getframerate() == 16_000
            return "normalized real fallback"

    monkeypatch.setattr(
        "orchestrator.voice_transcriber.get_transcriber",
        lambda: _Transcriber(),
    )
    result = await execute_media_read(
        {"path": str(path)},
        access_root=tmp_path,
        workspace_dir=tmp_path,
        media_roots=[],
    )

    assert isinstance(result, StructuredToolOutput)
    assert any("normalized real fallback" in block.get("text", "") for block in result.content)


@pytest.mark.asyncio
async def test_video_returns_deterministic_bounded_frames(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe are unavailable")
    path = tmp_path / "clip.mp4"
    created = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x48:d=1",
            "-c:v",
            "mpeg4",
            "-y",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert created.returncode == 0, created.stderr

    result = await execute_media_read(
        {"path": str(path), "video_frames": 3},
        access_root=tmp_path,
        workspace_dir=tmp_path,
        media_roots=[],
    )

    assert isinstance(result, StructuredToolOutput), result
    images = [block for block in result.content if block.get("type") == "image"]
    assert len(images) == 3
    assert "frame_timestamps=[0.1, 0.5, 0.9]" in result.output


@pytest.mark.asyncio
async def test_gateway_preserves_structured_content_and_context_scopes_media(tmp_path):
    workspace = tmp_path / "workspace"
    media = tmp_path / "media" / "zelda"
    workspace.mkdir()
    media.mkdir(parents=True)
    registry = ToolRegistry(["file_read"], workspace, workspace, {})
    context_path = tmp_path / "context.json"
    write_gateway_context(
        registry,
        context_path,
        additional_allowed_tools={"media_read"},
        media_roots=[media],
    )
    context = load_gateway_context(context_path)
    gateway = ToolGateway(context)

    assert gateway.registry.is_allowed("media_read")
    assert gateway.registry.media_roots == [media.resolve()]

    async def structured_execute(name, arguments, tool_call_id=""):
        from tools.registry import ToolResult

        return ToolResult(
            tool_call_id=tool_call_id,
            output="safe metadata only",
            content=[
                {"type": "text", "text": "visible metadata"},
                {"type": "image", "mimeType": "image/jpeg", "data": "YWJj"},
            ],
        )

    gateway.registry.execute = structured_execute
    response = await gateway.call("media_read", {"path": "photo.jpg"}, "call-2")
    assert response["content"][1] == {
        "type": "image",
        "mimeType": "image/jpeg",
        "data": "YWJj",
    }


@pytest.mark.asyncio
async def test_gateway_strips_image_blocks_for_text_only_backend(tmp_path):
    workspace = tmp_path / "workspace"
    media = tmp_path / "media" / "zelda"
    workspace.mkdir()
    media.mkdir(parents=True)
    registry = ToolRegistry(["file_read"], workspace, workspace, {})
    context_path = tmp_path / "context.json"
    write_gateway_context(
        registry,
        context_path,
        additional_allowed_tools={"media_read"},
        media_roots=[media],
        vision_enabled=False,
    )
    context = load_gateway_context(context_path)
    assert context.vision_enabled is False
    gateway = ToolGateway(context)

    async def structured_execute(name, arguments, tool_call_id=""):
        from tools.registry import ToolResult

        return ToolResult(
            tool_call_id=tool_call_id,
            output="safe metadata only",
            content=[
                {"type": "text", "text": "visible metadata"},
                {"type": "text", "text": "[IMAGE_OCR] extracted text [/IMAGE_OCR]"},
                {"type": "image", "mimeType": "image/jpeg", "data": "YWJj"},
            ],
        )

    gateway.registry.execute = structured_execute
    response = await gateway.call("media_read", {"path": "photo.jpg"}, "call-3")
    kinds = [block["type"] for block in response["content"]]
    assert kinds == ["text", "text"], kinds
    assert any("IMAGE_OCR" in block.get("text", "") for block in response["content"])
