"""Actual managed tool receipts with Fingerprint left off."""
import pytest
from tools.registry import ToolRegistry
from orchestrator.canonical_audit import CanonicalAuditStore
from orchestrator.getfile_index import RecentFileIndex


@pytest.mark.asyncio
async def test_successful_managed_create_modify_and_patch_have_verified_distinct_evidence(tmp_path):
    work=tmp_path/"work";work.mkdir()
    audit=CanonicalAuditStore(tmp_path/"home",instance_id="TEST",agent_id="alice")
    registry=ToolRegistry(allowed_tools=["file_write","apply_patch"],access_root=work,workspace_dir=work,
        secrets={},canonical_audit=audit,audit_context={"owner_id":"owner","agent_name":"alice","request_id":"run"})
    created=await registry.execute("file_write",{"path":"report.txt","content":"old\n"},"create")
    assert not created.is_error,created.output
    first,=RecentFileIndex(audit).recent(owner_id="owner")
    assert first["action"]=="created" and first["evidence"]=="verified"
    modified=await registry.execute("file_write",{"path":"report.txt","content":"new\n"},"modify")
    assert not modified.is_error,modified.output
    patch=await registry.execute("apply_patch",{"path":"report.txt",
        "patch":"--- a/report.txt\n+++ b/report.txt\n@@ -1 +1 @@\n-new\n+patched\n"},"patch")
    assert not patch.is_error,patch.output
    row,=RecentFileIndex(audit).recent(owner_id="owner")
    assert row["source_event_id"]!=first["source_event_id"]
    assert row["action"]=="modified" and row["evidence"]=="verified"
    assert (work/"report.txt").read_text()=="patched\n"


@pytest.mark.asyncio
async def test_failed_dry_run_does_not_invent_an_edit(tmp_path):
    audit=CanonicalAuditStore(tmp_path/"home",instance_id="TEST",agent_id="alice")
    registry=ToolRegistry(allowed_tools=["apply_patch"],access_root=tmp_path,workspace_dir=tmp_path,
        secrets={},canonical_audit=audit,audit_context={"owner_id":"owner","agent_name":"alice"})
    (tmp_path/"file.txt").write_text("keep\n")
    result=await registry.execute("apply_patch",{"path":"file.txt","patch":"invalid patch"},"bad")
    assert result.is_error
    assert not RecentFileIndex(audit).recent(owner_id="owner")
