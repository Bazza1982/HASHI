"""A named file is a lossless document, not an audio-generation request."""
import pytest
from tests.test_getfile_delivery import target
from orchestrator.getfile_service import GetFileService


@pytest.mark.asyncio
async def test_arbitrary_audio_named_bytes_are_not_transcoded_or_rejected(target):
    runtime,update,work,session=target
    path=work/"original.wav";path.write_bytes(b"binary-file-not-a-decodable-wave")
    result=(await GetFileService(runtime).transfer(update,raw_path=str(path)))["getfile"]
    metadata,payload=runtime.session_store.attachment_bytes(session_id=session["session_id"],owner_id="user:7",
        attachment_id=result["attachment_id"])
    assert payload==path.read_bytes()
    assert metadata["media_type"]=="application/octet-stream"
