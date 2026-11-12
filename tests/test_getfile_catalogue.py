"""The actual catalogue source response used by explicit Workbench retention."""
import hashlib
import json
import pytest
from tests.test_session_api import _server, _Request
from orchestrator import runtime_session


@pytest.mark.asyncio
async def test_source_and_feed_agree_on_agent_custody_without_exposing_hash_or_path(tmp_path):
    server,runtime=_server(tmp_path)
    runtime.global_config=server.global_config
    runtime.session_store=server.session_store
    runtime.name="lily"
    session=server.session_store.resolve_primary_session(owner_id="user:7",agent_id="lily",establish=True)
    publication=runtime_session.publish_frontend_media_notification(runtime,filename="report.docx",
        media_type="application/octet-stream",payload=b"actual bytes",sha256=hashlib.sha256(b"actual bytes").hexdigest(),
        caption="report",publication_id="getfile-catalogue",surface="workbench",channel_key="test",
        explicit_owner_id="user:7",explicit_session_id=session["session_id"],expected_context_generation=1,
        presentation_role="document",origin_only=True)
    response=await server.handle_v1_attachment_catalogue_source(_Request(query={
        "session_id":session["session_id"],"message_id":publication["message_id"],
        "attachment_id":publication["attachment_id"]}))
    assert response.status==200,response.text
    page=json.loads(response.text)
    message=next(m for m in page["messages"] if m["message_id"]==publication["message_id"])
    assert message["agent_id"]=="lily"
    assert message["agent_identity"]=="legacy-session:"+session["session_id"]
    assert message["context_generation"]==1
    item,=message["attachments"]
    assert item["size_bytes"]==len(b"actual bytes")
    assert "sha256" not in item and "local_ref" not in item
