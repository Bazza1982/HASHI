"""Observable Office lifecycle, isolation and durable edit tests (real files)."""
import asyncio
from pathlib import Path

import pytest

from tools.office.gateway import OfficeGateway
from tools.office.runtime import OfficeRuntime, qualify_runtime


@pytest.fixture(scope="session")
def office_manifest(tmp_path_factory):
    import os
    python = os.environ.get("HASHI_TEST_OFFICE_PYTHON")
    if not python:
        pytest.skip("Set HASHI_TEST_OFFICE_PYTHON to the independent Office Runtime")
    return qualify_runtime(Path(python), tmp_path_factory.mktemp("qualified") / "runtime")


@pytest.fixture
def office(tmp_path, office_manifest):
    manifest = office_manifest
    import os
    options = {"manifest": str(manifest), "state_root": str(tmp_path / "state"),
               "work_root": str(Path(os.environ.get("TEMP",str(tmp_path))) / "hashi-office-test-work")}
    context = {"agent_name": "office-a", "hashi_session_id": "session-a",
               "request_id": "request-a", "instance_id": "TEST"}
    gateway = OfficeGateway(OfficeRuntime(options), read_roots=(tmp_path,),
                            write_roots=(tmp_path,), cwd=tmp_path, context=context)
    return gateway


def call(gateway, name, arguments, call_id):
    return asyncio.run(gateway.execute(name, arguments, call_id))


def test_real_word_file_with_long_work_root(office_manifest, tmp_path):
    work_root = tmp_path / ("long-office-working-directory-" * 3)
    assert len(str(work_root)) > 100
    runtime = OfficeRuntime({"manifest": str(office_manifest), "state_root": str(tmp_path / "state"),
                             "work_root": str(work_root)})
    gateway = OfficeGateway(runtime, read_roots=(tmp_path,), write_roots=(tmp_path,), cwd=tmp_path,
                            context={"agent_name": "office-a", "hashi_session_id": "long-path",
                                     "request_id": "long-path", "instance_id": "TEST"})
    created = call(gateway, "office_create", {"format": "docx", "output": "long.docx",
                   "content": "# Long path\n\nContent"}, "create")
    assert created["status"] == "ok", created
    saved = call(gateway, "office_finish", {"file_id": created["file_id"]}, "finish")
    assert saved["status"] == "ok" and (tmp_path / "long.docx").is_file(), saved


def test_actual_workbook_edit_reopen_and_idempotence(office, tmp_path):
    created = call(office, "office_create", {"format": "xlsx", "output": "sales.xlsx",
                   "content": "Name,Amount\n甲,10\n乙,20"}, "create")
    assert created["status"] == "ok", created
    file_id = created["file_id"]
    changed = call(office, "office_edit", {"file_id": file_id, "action": "set_formula",
                   "target": "Sheet1!C2", "content": "=SUM(B2:B3)"}, "formula")
    assert changed["status"] == "ok", changed
    assert call(office, "office_edit", {"file_id": file_id, "action": "set_formula",
                "target": "Sheet1!C2", "content": "=SUM(B2:B3)"}, "formula") == changed
    result = call(office, "office_finish", {"file_id": file_id}, "finish")
    assert result["checks"]["saved"] and result["checks"]["structure"] == "passed", result
    read = call(office, "office_read", {"file_id": file_id, "target": "Sheet1!A1:C3"}, "read")
    assert read["data"]["rows"][1] == ["甲", 10, "=SUM(B2:B3)"]
    assert (tmp_path / "sales.xlsx").is_file()


def test_cross_session_handles_and_changed_source_are_denied(office, tmp_path):
    result = call(office, "office_create", {"format": "docx", "output": "report.docx",
                  "content": "# 标题\n\n原始内容"}, "create")
    file_id = result["file_id"]
    other = OfficeGateway(office.runtime, read_roots=(tmp_path,), write_roots=(tmp_path,),
                          cwd=tmp_path, context={**office.context, "hashi_session_id": "other"})
    assert call(other, "office_read", {"file_id": file_id}, "steal")["code"] == "FILE_NOT_FOUND"
    assert call(office, "office_finish", {"file_id": file_id}, "save")["status"] == "ok"
    inspected = call(office, "office_inspect", {"path": str(tmp_path / "report.docx")}, "inspect")
    (tmp_path / "report.docx").write_bytes(b"changed externally")
    edited = call(office, "office_edit", {"file_id": inspected["file_id"],
                  "action": "append_section", "content": "extra"}, "edit")
    assert edited["status"] == "ok", edited
    assert edited["data"]["source_provenance"] == "changed"
    saved = call(office, "office_finish", {"file_id": inspected["file_id"],
                 "output": str(tmp_path / "edited-copy.docx")}, "copy")
    assert saved["status"] == "ok", saved
    assert saved["data"]["source_provenance"] == "changed"
    conflict = call(office, "office_finish", {"file_id": inspected["file_id"],
                    "output": str(tmp_path / "report.docx")}, "overwrite")
    assert conflict["code"] == "ORIGINAL_PROTECTED"
    assert (tmp_path / "report.docx").read_bytes() == b"changed externally"


def test_output_refuses_existing_file_and_path_escape(office, tmp_path):
    created = call(office, "office_create", {"format": "pptx", "output": "deck.pptx",
                   "content": "# Deck\n\n## First\n- One\n- Two"}, "create")
    (tmp_path / "deck.pptx").write_bytes(b"do not overwrite")
    result = call(office, "office_finish", {"file_id": created["file_id"]}, "finish")
    assert result["code"] == "OUTPUT_EXISTS"
    assert (tmp_path / "deck.pptx").read_bytes() == b"do not overwrite"
    denied = call(office, "office_inspect", {"path": str(tmp_path.parent / "secret.xlsx")}, "escape")
    assert denied["code"] == "PATH_DENIED"


def test_real_formats_and_pdf_preview(office, tmp_path):
    for fmt, content in (("docx", "# Report\n\n## Summary\nHello 世界"),
                         ("pptx", "# Report\n\n## Summary\n- Hello")):
        result = call(office, "office_create", {"format": fmt, "output": "report." + fmt,
                      "content": content}, "create-" + fmt)
        assert result["status"] == "ok", result
        saved = call(office, "office_finish", {"file_id": result["file_id"], "preview": True},
                     "finish-" + fmt)
        assert saved["checks"]["structure"] == "passed", saved
        assert (tmp_path / ("report." + fmt)).stat().st_size > 500
        assert saved["data"]["preview"]["kind"] in {"pdf", "unavailable"}


def test_failed_edit_retains_prior_version(office):
    created = call(office, "office_create", {"format": "xlsx", "output": "a.xlsx",
                   "content": "A,B\n1,2"}, "create")
    file_id = created["file_id"]
    failure = call(office, "office_edit", {"file_id": file_id, "action": "set_cells",
                   "target": "Sheet1!A0", "content": "[[9]]"}, "invalid")
    assert failure["status"] == "error"
    read = call(office, "office_read", {"file_id": file_id, "target": "Sheet1!A1:B2"}, "read")
    assert read["data"]["rows"] == [["A", "B"], [1, 2]]
# HASHI-Fingerprint: hfp_cc53e51a78da421691d877b7c22e61be



def test_real_word_inspect_through_authorized_alias_and_save_copy(office, tmp_path):
    import os

    created = call(office, "office_create", {"format": "docx", "output": "original.docx",
                   "content": "# Original\n\nBefore"}, "create-alias-source")
    assert created["status"] == "ok", created
    finished = call(office, "office_finish", {"file_id": created["file_id"]}, "finish-alias-source")
    assert finished["status"] == "ok", finished
    source = tmp_path / "original.docx"
    original_bytes = source.read_bytes()
    alias = tmp_path / "document-alias.docx"
    try:
        os.symlink(source, alias)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink creation unavailable: {type(error).__name__}")

    opened = call(office, "office_inspect", {"path": str(alias)}, "inspect-alias")
    assert opened["status"] == "ok", opened
    edited = call(office, "office_edit", {"file_id": opened["file_id"],
                  "action": "append_section", "content": "After alias"}, "edit-alias")
    assert edited["status"] == "ok", edited
    saved = call(office, "office_finish", {"file_id": opened["file_id"],
                 "output": str(tmp_path / "alias-copy.docx")}, "save-alias-copy")
    assert saved["status"] == "ok", saved
    saved_path = tmp_path / "alias-copy.docx"
    assert saved_path.is_file()
    reopened = call(office, "office_inspect", {"path": str(saved_path)}, "reopen-alias-copy")
    assert reopened["status"] == "ok", reopened
    read = call(office, "office_read", {"file_id": reopened["file_id"],
                "target": "paragraph:1-10", "view": "text"}, "read-alias-copy")
    assert read["status"] == "ok", read
    assert "After alias" in str(read["data"])
    assert source.read_bytes() == original_bytes
