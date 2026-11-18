from __future__ import annotations

import base64
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from tools.gateway.context import GatewayContext, load_gateway_context, write_gateway_context
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
async def test_wildcard_her_media_capability_uses_same_grant_and_respects_opt_out(tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGB", (12, 9), "green").save(path)
    registry = ToolRegistry(["*"], tmp_path, tmp_path, {}, audit_context={"backend": "her-v2"})
    assert registry.is_allowed("media_read")
    assert "media_read" in {item["function"]["name"] for item in registry.get_tool_definitions()}
    gateway_registry = GatewayContext.from_registry(registry, backend="her-v2").build_registry()
    assert gateway_registry.is_allowed("media_read")
    result = await registry.execute("media_read", {"path": str(path), "ocr_mode": "off"})
    assert not result.is_error
    assert result.content and result.content[-1]["type"] == "image"

    disabled = ToolRegistry(["*", "media_read"], tmp_path, tmp_path, {},
                            audit_context={"backend": "her-v2"},
                            tool_options={"media_read": {"enabled": False}})
    assert not disabled.is_allowed("media_read")


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
@pytest.mark.platform
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
@pytest.mark.platform
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
@pytest.mark.platform
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



@pytest.mark.asyncio
async def test_media_pdf_tail_page_above_old_page_limit(tmp_path):
    fitz = pytest.importorskip("fitz")
    source = tmp_path / "many-pages.pdf"
    with fitz.open() as document:
        for index in range(45):
            page = document.new_page()
            page.insert_text((72, 72), f"page-{index + 1}-marker")
        document.save(source)
    registry = _registry(tmp_path)
    tail = await registry.execute("media_read", {
        "path": str(source), "pdf_pages": "none", "page_start": 45, "page_end": 45,
    })
    assert not tail.is_error, tail.output
    assert "processed_pages=45-45" in tail.output and "next_page=None" in tail.output
    assert any("page-45-marker" in item.get("text", "") for item in tail.content)
    initial = await registry.execute("media_read", {"path": str(source), "pdf_pages": "none"})
    assert not initial.is_error and "processed_pages=1-30" in initial.output and "next_page=31" in initial.output
    denied = await registry.execute("media_read", {
        "path": str(source), "page_start": 46, "page_end": 46,
    })
    assert denied.is_error and "outside" in denied.output



@pytest.mark.asyncio
@pytest.mark.platform
async def test_long_audio_selected_time_range_uses_real_normalizer(tmp_path, monkeypatch):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is unavailable")
    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        for second in range(605):
            audio.writeframes((b"\x00\x3f" if second == 601 else b"\x00\x00") * 8000)
    monkeypatch.setattr("tools.media_read._probe_av", lambda _path: MediaProbe(
        kind="audio", format_name="wav", duration=605.0, has_audio=True))
    class Transcriber:
        async def transcribe(self, normalized):
            with wave.open(str(normalized), "rb") as audio:
                assert audio.getframerate() == 16000 and audio.getnchannels() == 1
                assert audio.getnframes() == 32000
                assert any(audio.readframes(audio.getnframes()))
            return "tail marker recognized"
    monkeypatch.setattr("orchestrator.voice_transcriber.get_transcriber", lambda: Transcriber())
    result = await execute_media_read({
        "path": str(path), "start_seconds": 600, "end_seconds": 602,
    }, access_root=tmp_path, workspace_dir=tmp_path, media_roots=[])
    assert isinstance(result, StructuredToolOutput), result
    assert "audio_seconds=600.000-602.000" in result.output
    assert "audio_next_start=None" in result.output
    assert any("tail marker recognized" in item.get("text", "") for item in result.content)
    invalid = await execute_media_read({
        "path": str(path), "start_seconds": 604, "end_seconds": 606,
    }, access_root=tmp_path, workspace_dir=tmp_path, media_roots=[])
    assert isinstance(invalid, str) and "outside" in invalid


@pytest.mark.asyncio
async def test_long_audio_default_reports_next_time_window(tmp_path, monkeypatch):
    path = tmp_path / "long.ogg"
    path.write_bytes(b"OggS" + b"\x00" * 64)
    monkeypatch.setattr("tools.media_read._probe_av", lambda _path: MediaProbe(
        kind="audio", format_name="ogg", duration=1205.0, has_audio=True))
    seen = []
    async def transcribe(_path, *, start, end):
        seen.append((start, end))
        return "chunk"
    monkeypatch.setattr("tools.media_read._transcribe_normalized", transcribe)
    result = await execute_media_read({"path": str(path)}, access_root=tmp_path,
                                      workspace_dir=tmp_path, media_roots=[])
    assert isinstance(result, StructuredToolOutput), result
    assert seen == [(0.0, 600.0)] and "audio_next_start=600.0" in result.output



@pytest.mark.asyncio
@pytest.mark.platform
async def test_long_video_selected_time_range_uses_real_frame_extraction(tmp_path, monkeypatch):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is unavailable")
    path = tmp_path / "long.mp4"
    created = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "color=c=red:s=64x48:r=1:d=605", "-c:v", "mpeg4", "-y", str(path),
    ], capture_output=True, text=True, timeout=60, check=False)
    assert created.returncode == 0, created.stderr
    monkeypatch.setattr("tools.media_read._probe_av", lambda _path: MediaProbe(
        kind="video", format_name="mp4", duration=605.0, has_video=True))
    result = await execute_media_read({
        "path": str(path), "start_seconds": 600, "end_seconds": 604, "video_frames": 3,
    }, access_root=tmp_path, workspace_dir=tmp_path, media_roots=[])
    assert isinstance(result, StructuredToolOutput), result
    assert "selected_seconds=600.000-604.000" in result.output
    assert "frame_timestamps=[600.4, 602.0, 603.6]" in result.output
    assert len([item for item in result.content if item.get("type") == "image"]) == 3



@pytest.mark.asyncio
async def test_pdf_cursor_reconstructs_text_and_all_rendered_pages(tmp_path):
    fitz = pytest.importorskip("fitz")
    source = tmp_path / "complete.pdf"
    with fitz.open() as document:
        for index in range(35):
            page = document.new_page(width=1000, height=3000 if index == 0 else 842)
            if index == 0:
                for line in range(160):
                    page.insert_text((20, 20 + line * 15),
                                     f"{line:03d} " + "long first page marker " * 4, fontsize=8)
            elif index not in (19, 34):
                page.insert_text((72, 72), f"page-{index + 1}-marker")
        document.save(source)
    with fitz.open(source) as document:
        expected = [page.get_text("text") for page in document]
    assert len(expected[0]) > 12000
    registry = _registry(tmp_path)
    first = await registry.execute("media_read", {
        "path": str(source), "pdf_pages": "all", "page_start": 1, "page_end": 35,
    })
    assert not first.is_error, first.output
    first_cursor = first.details["pdf_next_cursor"]
    assert first_cursor
    mismatched = await registry.execute("media_read", {
        "path": str(source), "pdf_cursor": first_cursor, "pdf_pages": "none",
    })
    assert mismatched.is_error and "original range or mode" in mismatched.output
    actual = {index: "" for index in range(1, 36)}
    rendered = []
    result = first
    for _ in range(100):
        assert not result.is_error, result.output
        rendered.extend(result.details["pdf_rendered_pages"])
        for part in result.details["pdf_text_segments"]:
            index = part["page"]
            assert part["offset"] == len(actual[index])
            block = result.content[part["content_index"]]["text"]
            piece = block.split("\n", 1)[1]
            assert len(piece) == part["length"]
            actual[index] += piece
        cursor = result.details["pdf_next_cursor"]
        if not cursor:
            assert result.details["pdf_complete"]
            break
        result = await registry.execute("media_read", {"path": str(source), "pdf_cursor": cursor})
    else:
        pytest.fail("PDF continuation did not finish")
    assert [actual[index] for index in range(1, 36)] == expected
    assert rendered == list(range(1, 36))
    source.write_bytes(source.read_bytes() + b"x")
    stale = await registry.execute("media_read", {"path": str(source), "pdf_cursor": first_cursor})
    assert stale.is_error and "source changed" in stale.output


@pytest.mark.asyncio
async def test_long_ocr_can_be_continued_and_timeout_is_configurable(tmp_path, monkeypatch):
    path = tmp_path / "large-ocr.png"
    Image.new("RGB", (40, 30), "white").save(path)
    text = ("recognized text " * 4000) + "OCR-FINAL-MARKER"
    observed = []
    def fake_ocr(_encoded, *, languages, timeout_seconds, cancellation_event=None):
        observed.append(timeout_seconds)
        return OCRResult(status="ok", text=text, requested_languages=("eng",), used_languages=("eng",))
    monkeypatch.setattr("tools.media_read.extract_image_bytes", fake_ocr)
    registry = ToolRegistry(
        ["media_read"], tmp_path, tmp_path, {},
        audit_context={"instance_id": "NP", "agent_name": "fixture", "owner_id": "owner",
                       "hashi_session_id": "session", "hashi_run_id": "run", "request_id": "request"},
        tool_options={"media_read": {"ocr_timeout_seconds": 75}},
    )
    first = await registry.execute("media_read", {"path": str(path)})
    assert not first.is_error, first.output
    assert observed == [75]
    assert first.content[-1]["type"] == "image"
    reference = first.details["content_continuations"][0]["reference"]
    parts, offset = [], 0
    for _ in range(100):
        page = await registry.execute("tool_output_read", {"reference": reference, "offset": offset})
        assert not page.is_error, page.output
        data = json.loads(page.output)
        parts.append(data["text"])
        if data["complete"]:
            break
        offset = data["next_offset"]
    else:
        pytest.fail("OCR continuation did not terminate")
    from tools.ocr import format_ocr_block
    expected = format_ocr_block(OCRResult(status="ok", text=text, requested_languages=("eng",), used_languages=("eng",)))
    assert "".join(parts) == expected
    denied = await registry.execute_with_audit_context(
        "tool_output_read", {"reference": reference, "offset": 0},
        audit_context={"hashi_run_id": "other", "request_id": "other"},
    )
    assert denied.is_error and "OCR-FINAL-MARKER" not in denied.output
    second = await registry.execute("media_read", {"path": str(path), "ocr_timeout_seconds": 120})
    assert not second.is_error and observed == [75, 120]
    for invalid in [0, -1, float("nan"), True, "120"]:
        result = await registry.execute("media_read", {"path": str(path), "ocr_timeout_seconds": invalid})
        assert result.is_error and "positive finite" in result.output


@pytest.mark.asyncio
async def test_cancelling_media_ocr_reaps_the_owned_worker(tmp_path, monkeypatch):
    import asyncio
    import sys
    import psutil
    from tools import ocr
    path = tmp_path / "cancel-ocr.png"
    Image.new("RGB", (40, 30), "white").save(path)
    marker = tmp_path / "worker.pid"
    monkeypatch.setattr(ocr, "resolve_tesseract_binary", lambda: "fixture-tesseract")
    monkeypatch.setattr(ocr, "resolve_ocr_model_root", lambda: tmp_path)
    monkeypatch.setattr(ocr, "_available_languages", lambda *_args: {"eng"})
    monkeypatch.setattr(ocr, "_detect_script", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ocr, "_paddle_runtime_ready", lambda: False)
    def run_fixture(_command, *, timeout_seconds):
        code = "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)"
        return ocr._run_ocr_process([sys.executable, "-c", code, str(marker)],
                                    capture_output=True, text=True, timeout=timeout_seconds, check=False)
    monkeypatch.setattr(ocr, "_run_tesseract", run_fixture)
    task = asyncio.create_task(execute_media_read(
        {"path": str(path), "ocr_languages": ["eng"], "ocr_timeout_seconds": 180},
        access_root=tmp_path, workspace_dir=tmp_path, media_roots=[],
    ))
    pid = None
    try:
        for _ in range(200):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert marker.exists(), "OCR worker did not start"
        pid = int(marker.read_text())
        assert psutil.pid_exists(pid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert not psutil.pid_exists(pid), "cancel returned while OCR worker still existed"
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if pid is not None and psutil.pid_exists(pid):
            psutil.Process(pid).kill()


@pytest.mark.asyncio
async def test_media_ocr_fallback_can_finish_after_old_fifteen_second_limit(tmp_path, monkeypatch):
    import asyncio
    import sys
    from tools import ocr
    path = tmp_path / "slow-ocr.png"
    Image.new("RGB", (40, 30), "white").save(path)
    monkeypatch.setattr(ocr, "resolve_tesseract_binary", lambda: "fixture-tesseract")
    monkeypatch.setattr(ocr, "resolve_ocr_model_root", lambda: tmp_path)
    monkeypatch.setattr(ocr, "_available_languages", lambda *_args: {"eng"})
    monkeypatch.setattr(ocr, "_detect_script", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ocr, "_paddle_runtime_ready", lambda: False)
    def run_fixture(_command, *, timeout_seconds):
        return ocr._run_ocr_process([sys.executable, "-c", "import time; time.sleep(15.2); print('slow OCR finished')"],
                                    capture_output=True, text=True, timeout=timeout_seconds, check=False)
    monkeypatch.setattr(ocr, "_run_tesseract", run_fixture)
    result = await asyncio.wait_for(execute_media_read(
        {"path": str(path), "ocr_languages": ["eng"], "ocr_timeout_seconds": 20},
        access_root=tmp_path, workspace_dir=tmp_path, media_roots=[],
    ), timeout=25)
    assert isinstance(result, StructuredToolOutput), result
    assert "ocr_status=ok" in result.output
    assert any("slow OCR finished" in part.get("text", "") for part in result.content)


@pytest.mark.asyncio
async def test_image_regions_recover_full_spatial_resolution_and_reject_outside(tmp_path):
    import io
    path = tmp_path / "wide.png"
    source = Image.new("RGB", (6000, 80), "red")
    source.paste("blue", (3000, 0, 6000, 80))
    source.save(path)
    original = path.read_bytes()
    registry = _registry(tmp_path)
    preview = await registry.execute("media_read", {"path": str(path), "ocr_mode": "off"})
    assert not preview.is_error and "normalized=4000x53" in preview.output
    for x, expected in [(0, "red"), (3000, "blue")]:
        result = await registry.execute("media_read", {
            "path": str(path), "ocr_mode": "off",
            "image_region": {"x": x, "y": 0, "width": 3000, "height": 80},
        })
        assert not result.is_error, result.output
        assert f"region=({x}, 0, 3000, 80)" in result.output
        with Image.open(io.BytesIO(base64.b64decode(result.content[-1]["data"]))) as tile:
            assert tile.size == (3000, 80)
            r, g, b = tile.getpixel((1500, 40))
            assert (r > 240 and b < 15) if expected == "red" else (b > 240 and r < 15)
    denied = await registry.execute("media_read", {
        "path": str(path), "ocr_mode": "off",
        "image_region": {"x": 5999, "y": 0, "width": 2, "height": 80},
    })
    assert denied.is_error and "outside" in denied.output
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_media_source_byte_limits_are_operator_configurable(tmp_path, monkeypatch):
    from tools.media_read import IMAGE_MAX_BYTES
    monkeypatch.setattr("tools.media_read._available_media_memory", lambda: None)
    path = tmp_path / "large-source.png"
    Image.new("RGB", (16, 16), "white").save(path)
    with path.open("ab") as handle:
        handle.write(b"\x00" * (IMAGE_MAX_BYTES + 1 - path.stat().st_size))
    default = await _registry(tmp_path).execute("media_read", {"path": str(path), "ocr_mode": "off"})
    assert default.is_error and "configured" in default.output
    enabled = ToolRegistry(["media_read"], tmp_path, tmp_path, {},
                           tool_options={"media_read": {"resource_budgets": {"image_source_bytes": IMAGE_MAX_BYTES + 2}}})
    result = await enabled.execute("media_read", {"path": str(path), "ocr_mode": "off"})
    assert not result.is_error, result.output
    assert "normalized=16x16" in result.output
    monkeypatch.setattr("tools.media_read._available_media_memory", lambda: 2 * 1024 ** 3)
    automatic = await _registry(tmp_path).execute("media_read", {"path": str(path), "ocr_mode": "off"})
    assert not automatic.is_error and "normalized=16x16" in automatic.output
    invalid = ToolRegistry(["media_read"], tmp_path, tmp_path, {},
                           tool_options={"media_read": {"resource_budgets": {"image_source_bytes": True}}})
    rejected = await invalid.execute("media_read", {"path": str(path), "ocr_mode": "off"})
    assert rejected.is_error and "positive integer" in rejected.output
    assert path.stat().st_size == IMAGE_MAX_BYTES + 1


@pytest.mark.asyncio
async def test_pdf_and_av_source_limits_apply_to_the_selected_resource_budget(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "pdf budget marker")
        document.save(pdf)
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 64)
    monkeypatch.setattr("tools.media_read._probe_av", lambda _path: MediaProbe(kind="audio", format_name="wav", duration=2, has_audio=True))
    async def transcribe(_path):
        return "audio budget marker"
    monkeypatch.setattr("tools.media_read._transcribe_normalized", transcribe)
    options = {"resource_budgets": {"pdf_source_bytes": pdf.stat().st_size, "av_source_bytes": audio.stat().st_size}}
    for path, extra, marker in [(pdf, {"pdf_pages": "none"}, "pdf budget marker"), (audio, {}, "audio budget marker")]:
        result = await execute_media_read({"path": str(path), **extra}, access_root=tmp_path,
                                          workspace_dir=tmp_path, media_roots=[], options=options)
        assert isinstance(result, StructuredToolOutput), result
        assert any(marker in block.get("text", "") for block in result.content)
    options["resource_budgets"]["pdf_source_bytes"] -= 1
    options["resource_budgets"]["av_source_bytes"] -= 1
    for path in [pdf, audio]:
        result = await execute_media_read({"path": str(path)}, access_root=tmp_path,
                                          workspace_dir=tmp_path, media_roots=[], options=options)
        assert isinstance(result, str) and "configured" in result


@pytest.mark.asyncio
async def test_image_region_uses_oriented_source_coordinates(tmp_path):
    import io
    path = tmp_path / "oriented.jpg"
    image = Image.new("RGB", (120, 80), "green")
    exif = image.getexif()
    exif[274] = 6
    image.save(path, exif=exif)
    result = await _registry(tmp_path).execute("media_read", {
        "path": str(path), "ocr_mode": "off",
        "image_region": {"x": 0, "y": 0, "width": 80, "height": 120},
    })
    assert not result.is_error, result.output
    assert "source=80x120" in result.output and "region=(0, 0, 80, 120)" in result.output
    with Image.open(io.BytesIO(base64.b64decode(result.content[-1]["data"]))) as tile:
        assert tile.size == (80, 120)


@pytest.mark.asyncio
async def test_audio_continuation_binds_source_and_preserves_time_range(tmp_path, monkeypatch):
    path = tmp_path / "continuation.ogg"
    path.write_bytes(b"OggS" + bytes(64))
    monkeypatch.setattr("tools.media_read._probe_av", lambda _path: MediaProbe(
        kind="audio", format_name="ogg", duration=1800.0, has_audio=True))
    seen = []
    async def transcribe(_path, *, start, end):
        seen.append((start, end))
        return f"recognized {start} to {end}"
    monkeypatch.setattr("tools.media_read._transcribe_normalized", transcribe)
    registry = _registry(tmp_path)
    first = await registry.execute("media_read", {"path": str(path), "start_seconds": 0.000125,
                                                   "end_seconds": 1200.000125})
    assert not first.is_error, first.output
    cursor = first.details["audio_next_cursor"]
    assert cursor and first.details["audio_range"] == {"start": 0.000125, "end": 600.000125, "requested_end": 1200.000125}
    second = await registry.execute("media_read", {"path": str(path), "audio_cursor": cursor})
    assert not second.is_error, second.output
    assert second.details["audio_next_cursor"] is None
    assert seen == [(0.000125, 600.000125), (600.000125, 1200.000125)]
    mismatch = await registry.execute("media_read", {"path": str(path), "audio_cursor": cursor, "end_seconds": 1300})
    assert mismatch.is_error and "does not match" in mismatch.output
    with path.open("ab") as stream:
        stream.write(b"changed")
    changed = await registry.execute("media_read", {"path": str(path), "audio_cursor": cursor})
    assert changed.is_error and "source changed" in changed.output
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_audio_ffmpeg_selection_preserves_submillisecond_times(tmp_path, monkeypatch):
    from tools.media_read import _transcribe_normalized
    path = tmp_path / "source.wav"
    path.write_bytes(b"fixture")
    commands = []
    monkeypatch.setattr("tools.media_read.shutil.which", lambda _name: "fixture-ffmpeg")
    def run(command, *, timeout):
        commands.append(command)
        Path(command[-1]).write_bytes(b"normalized")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    monkeypatch.setattr("tools.media_read._run_process", run)
    class Transcriber:
        async def transcribe(self, _path):
            return "recognized"
    monkeypatch.setattr("orchestrator.voice_transcriber.get_transcriber", lambda: Transcriber())
    assert await _transcribe_normalized(path, start=0.000125, end=0.000376) == "recognized"
    command = commands[0]
    assert command[command.index("-ss") + 1] == "0.000125"
    assert command[command.index("-t") + 1] == "0.000251"


@pytest.mark.parametrize("child_still_alive", [False, True])
def test_ocr_cancellation_waits_for_owned_children(monkeypatch, child_still_alive):
    import subprocess
    import threading
    from types import SimpleNamespace
    import psutil
    from tools import ocr

    event = threading.Event()
    calls = []

    class Child:
        def kill(self):
            calls.append("child-kill")

    child = Child()

    class Process:
        pid = 12345

        def kill(self):
            calls.append("parent-kill")

        def communicate(self):
            calls.append("parent-reaped")

    def start(*args, **kwargs):
        event.set()
        return Process()

    monkeypatch.setattr(ocr, "subprocess", SimpleNamespace(
        Popen=start, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess,
    ))
    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(children=lambda recursive: [child]))

    def wait_children(children, timeout):
        assert children == [child] and timeout == 5
        calls.append("children-reaped")
        return ([], children) if child_still_alive else (children, [])

    monkeypatch.setattr(psutil, "wait_procs", wait_children)
    ocr._ocr_context.cancellation_event = event
    try:
        expected = ocr.OCRCleanupUnconfirmed if child_still_alive else ocr.OCRCancelled
        with pytest.raises(expected):
            ocr._run_ocr_process(["fixture"], capture_output=True, timeout=30, check=False)
    finally:
        del ocr._ocr_context.cancellation_event
    assert calls == ["child-kill", "parent-kill", "parent-reaped", "children-reaped"]


@pytest.mark.asyncio
async def test_media_ocr_reports_unconfirmed_cleanup_on_cancellation(tmp_path, monkeypatch):
    import asyncio
    import threading
    from tools import media_read
    from tools.ocr import OCRCleanupUnconfirmed

    path = tmp_path / "fixture.png"
    Image.new("RGB", (20, 20), "white").save(path)
    started = threading.Event()

    def fake_read(_path, *, cancellation_event, **_kwargs):
        started.set()
        assert cancellation_event.wait(timeout=5)
        raise OCRCleanupUnconfirmed("fixture child remained alive")

    monkeypatch.setattr(media_read, "_read_image", fake_read)
    task = asyncio.create_task(execute_media_read(
        {"path": str(path), "ocr_mode": "auto"}, access_root=tmp_path,
        workspace_dir=tmp_path, media_roots=[],
    ))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError, match="cleanup unconfirmed") as raised:
        await task
    assert isinstance(raised.value.__cause__, OCRCleanupUnconfirmed)
