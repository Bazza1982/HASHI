from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.frontend_contracts import FrontendCaptionError
from orchestrator.frontend_failures import frontend_failure
from orchestrator.multimodal_contract import MultimodalContractError
from transports.whatsapp import WhatsAppTransport


@pytest.mark.parametrize("error", [
    ValueError("private-token-and-caption"),
    RuntimeError("private-token-and-caption"),
    MultimodalContractError("private-token-and-caption", code="PRIVATE_TOKEN_AND_CAPTION",
                            diagnostic={"reason": "private-token-and-caption"}),
])
def test_failure_presentation_and_diagnostic_never_copy_untrusted_error_fields(error):
    failure = frontend_failure(error, phase="validate", submission_state="not_submitted")
    for locale in ("en", "zh-CN"):
        assert "private" not in failure.render(locale=locale).lower()
    assert "private" not in str(failure.diagnostic()).lower()


def test_caption_limit_and_wrapped_file_denial_keep_safe_specific_reasons():
    failure = frontend_failure(FrontendCaptionError("caption_too_long", maximum=4096),
                               phase="validate", submission_state="not_submitted")
    assert "4096" in failure.render(locale="en")
    wrapped = MultimodalContractError("private file", code="MEDIA_UNAVAILABLE")
    wrapped.__cause__ = PermissionError("private path")
    failure = frontend_failure(wrapped, phase="store", submission_state="not_submitted")
    assert "访问被拒绝" in failure.render(locale="zh-CN")
    assert "private" not in str(failure.diagnostic())


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["download", "store"])
async def test_whatsapp_media_failure_reports_safe_stage_and_never_queues_media(tmp_path, monkeypatch, phase):
    transport = WhatsAppTransport.__new__(WhatsAppTransport)
    transport.global_cfg = SimpleNamespace(ui_language="zh-CN")
    transport._media_dir = tmp_path
    transport._send_text = AsyncMock()
    transport._message_payload = lambda msg: SimpleNamespace(ImageMessage=SimpleNamespace(Caption="private-caption"))
    transport._client = SimpleNamespace(download_any=AsyncMock(
        side_effect=TimeoutError("private-url") if phase == "download" else None,
        return_value=b"image data"))
    if phase == "store":
        def denied(*args, **kwargs):
            raise PermissionError("private-path")
        monkeypatch.setattr(Path, "write_bytes", denied)
    result = await transport._handle_media_message(SimpleNamespace(Message=object()), "chat")
    assert result == (None, None)
    assert transport._client.download_any.await_count == 1
    feedback = transport._send_text.await_args.args[1]
    assert ("下载附件" if phase == "download" else "保存附件") in feedback
    assert ("及时响应" if phase == "download" else "访问被拒绝") in feedback
    assert "尚未提交" in feedback and "fcerr_" in feedback
    assert "private" not in feedback
