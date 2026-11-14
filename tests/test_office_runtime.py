"""Observable Office lifecycle, isolation and durable edit tests (real files)."""
import asyncio
import json
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
    conflict = call(office, "office_edit", {"file_id": inspected["file_id"],
                    "action": "append_section", "content": "extra"}, "edit")
    assert conflict["code"] == "SOURCE_CHANGED"
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
