"""Real regressions for the first working Office execution candidate."""
import asyncio
import json
import zipfile
from pathlib import Path

from tests.test_office_runtime import office, office_manifest, call


def test_template_without_content_preserves_cells(office, tmp_path):
    created = call(office, "office_create", {"format": "xlsx", "output": "template.xlsx",
                   "content": "Name,Amount\n甲,12"}, "original")
    assert call(office, "office_finish", {"file_id": created["file_id"]}, "save")["checks"]["saved"]
    copied = call(office, "office_create", {"format": "xlsx", "output": "copy.xlsx",
                  "template": str(tmp_path / "template.xlsx")}, "copy")
    assert copied["status"] == "ok", copied
    values = call(office, "office_read", {"file_id": copied["file_id"], "target": "A1:B2"}, "read")
    assert values["data"]["rows"] == [["Name", "Amount"], ["甲", 12]]


def test_external_formula_rejected_in_every_write_route(office):
    unsafe = '=WEBSERVICE("https://example.invalid")'
    created = call(office, "office_create", {"format": "xlsx", "output": "a.xlsx",
                   "content": json.dumps({"sheets":[{"name":"Data","rows":[["Name","Data"],["甲",unsafe]]}]})}, "unsafe")
    assert created["code"] == "EXTERNAL_FORMULA", created
    safe = call(office, "office_create", {"format": "xlsx", "output": "b.xlsx",
                "content": "Name,Amount\n甲,1"}, "safe")
    edited = call(office, "office_edit", {"file_id": safe["file_id"], "action": "set_cells",
                  "target": "B2", "content": json.dumps([[unsafe]])}, "unsafe-edit")
    assert edited["code"] == "EXTERNAL_FORMULA", edited


def test_pending_call_cannot_be_poisoned_by_conflicting_arguments(office, monkeypatch):
    async def scenario():
        original = office.runtime.invoke
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(office.runtime, "invoke", delayed)
        args = {"format": "docx", "output": "a.docx", "content": "original"}
        first = asyncio.create_task(office.execute("office_create", args, "same-id"))
        await entered.wait()
        conflict = await office.execute("office_create", {**args, "content": "different"}, "same-id")
        assert conflict["code"] == "IDEMPOTENCY_CONFLICT", conflict
        pending = await office.execute("office_create", args, "same-id")
        assert pending["code"] == "OPERATION_UNCERTAIN", pending
        release.set()
        created = await first
        replay = await office.execute("office_create", args, "same-id")
        assert replay == created and created["status"] == "ok"
    asyncio.run(scenario())


def test_gateway_reopen_retains_handles_and_saved_delivery_failure(office, tmp_path):
    from tools.office.gateway import OfficeGateway
    created = call(office, "office_create", {"format": "docx", "output": "x.docx",
                   "content": "# Persistent"}, "create")
    reopened = OfficeGateway(office.runtime, read_roots=(tmp_path,), write_roots=(tmp_path,),
                             cwd=tmp_path, context=office.context)
    assert call(reopened, "office_inspect", {"file_id": created["file_id"]}, "inspect")["checks"]["structure"] == "passed"
    response = call(reopened, "office_finish", {"file_id": created["file_id"], "deliver": True}, "finish")
    assert response["status"] == "partial" and response["checks"]["saved"]
    assert not response["checks"]["delivered"] and (tmp_path / "x.docx").exists()


def test_excel_recipes_compute_summary_reconcile_and_chart(office):
    created = call(office, "office_create", {"format": "xlsx", "output": "analysis.xlsx",
                   "content": "Group,Amount\n A ,10\nB,20\n A ,10\nA,5"}, "data")
    file_id = created["file_id"]
    assert call(office, "office_edit", {"file_id": file_id, "action": "excel_clean", "content": "{}"}, "clean")["status"] == "ok"
    summary = call(office, "office_edit", {"file_id": file_id, "action": "excel_summary",
                   "content": '{"group_by":"Group","value":"Amount"}'}, "summary")
    assert summary["status"] == "ok", summary
    rows = call(office, "office_read", {"file_id": file_id, "target": "Summary!A1:B3"}, "summary-read")["data"]["rows"]
    assert rows[1:] == [["A", 15], ["B", 20]]
    chart = call(office, "office_edit", {"file_id": file_id, "action": "excel_chart",
                 "target": "Summary!A1:B3", "content": '{"type":"bar","title":"Totals"}'}, "chart")
    assert chart["status"] == "ok" and chart["data"]["sheets"][1]["charts"] == 1, chart
    second = call(office, "office_create", {"format": "xlsx", "output": "other.xlsx",
                  "content": "Group,Amount\nA,99\nC,1"}, "second")
    reconciled = call(office, "office_edit", {"file_id": file_id, "source_id": second["file_id"],
                      "action": "excel_reconcile", "target": "Summary!A1:B3",
                      "content": '{"key":"Group"}'}, "reconcile")
    assert reconciled["status"] == "ok", reconciled
    rows = call(office, "office_read", {"file_id": file_id, "target": "Reconciliation!A1:D4"}, "reconcile-read")["data"]["rows"]
    assert [r[1] for r in rows[1:]] == ["changed", "only_left", "only_right"]


def test_word_text_replacement_and_powerpoint_native_chart(office, tmp_path):
    created = call(office, "office_create", {"format": "docx", "output": "word.docx",
                   "content": "# Report\n\nKeep formatting and replace this."}, "word")
    changed = call(office, "office_edit", {"file_id": created["file_id"], "action": "replace_text",
                   "content": '{"find":"replace this","replace":"改好了"}'}, "replace")
    assert changed["status"] == "ok", changed
    call(office, "office_finish", {"file_id": created["file_id"]}, "save-word")
    with zipfile.ZipFile(tmp_path / "word.docx") as package:
        document = package.read("word/document.xml").decode()
    assert "改好了" in document and "Keep formatting and " in document
    deck = call(office, "office_create", {"format": "pptx", "output": "chart.pptx",
                "content": json.dumps({"slides":[{"title":"Chart","chart":{"categories":["A","B"],"series":[{"name":"Sales","values":[1,2]}]}}]})}, "deck")
    assert deck["status"] == "ok" and deck["data"]["slides"][0]["charts"] == 1, deck
    changed = call(office, "office_edit", {"file_id":deck["file_id"],"action":"slides_from_outline",
                   "content":"# Next\n- Chart must survive"}, "append-deck")
    assert changed["status"] == "ok", changed
    call(office, "office_finish", {"file_id":deck["file_id"]}, "save-deck")
    with zipfile.ZipFile(tmp_path / "chart.pptx") as package:
        assert any(n.startswith("ppt/charts/") for n in package.namelist())
        assert len([n for n in package.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]) == 2



def test_word_create_preset_and_template_append_keep_style(office,tmp_path):
    original = call(office,"office_create",{"format":"docx","output":"academic.docx","style":"academic",
        "content":"# Academic\\nOriginal"},"academic-create")
    call(office,"office_finish",{"file_id":original["file_id"]},"academic-save")
    with zipfile.ZipFile(tmp_path/"academic.docx") as package:
        styles = package.read("word/styles.xml")
    assert b'Cambria' in styles
    copied = call(office,"office_create",{"format":"docx","output":"append.docx",
        "template":str(tmp_path/"academic.docx"),"content":"Appended"},"academic-append")
    assert copied["status"]=="ok",copied
    call(office,"office_finish",{"file_id":copied["file_id"]},"academic-append-save")
    with zipfile.ZipFile(tmp_path/"append.docx") as package:
        assert package.read("word/styles.xml")==styles
        assert b"Appended" in package.read("word/document.xml")

def test_markdown_pipe_table_becomes_native_word_table(office,tmp_path):
    created = call(office,"office_create",{"format":"docx","output":"markdown.docx",
        "content":"# Budget\n办公工具验收\n\n| Item | Amount |\n| --- | ---: |\n| Alpha | 10 |\n| Beta | 20 |"},"markdown-create")
    assert created["status"]=="ok" and created["data"]["tables"]==1,created
    saved = call(office,"office_finish",{"file_id":created["file_id"]},"markdown-save")
    assert saved["checks"]["saved"]
    with zipfile.ZipFile(tmp_path/"markdown.docx") as package:
        xml = package.read("word/document.xml").decode()
    assert "<w:tbl>" in xml and "Alpha" in xml and "办公工具验收" in xml

def test_targeted_word_table_and_slide_shape_reads(office):
    document = call(office, "office_create", {"format":"docx","output":"table.docx",
                    "content":json.dumps({"sections":[{"table":[["Item","Count"],["Alpha",10]]}]})}, "table-doc")
    table = call(office,"office_read",{"file_id":document["file_id"],"target":"table:1","view":"text"}, "table-read")
    assert table["status"] == "ok" and table["data"]["rows"] == [["Item","Count"],["Alpha","10"]], table
    deck = call(office,"office_create",{"format":"pptx","output":"shapes.pptx","content":"# Heading\n- Body"}, "shape-deck")
    shapes = call(office,"office_read",{"file_id":deck["file_id"],"target":"slide:1","view":"text"}, "shapes-read")
    assert shapes["data"]["slides"][0]["shapes"][0]["index"] == 1

def test_invalid_source_does_not_return_uncommitted_handle(office,tmp_path):
    (tmp_path/"broken.docx").write_bytes(b"broken")
    observed = call(office,"office_inspect",{"path":str(tmp_path/"broken.docx")}, "broken")
    assert observed["status"] == "error" and observed["file_id"] is None and observed["next"] is None, observed

def test_runtime_integrity_failure_does_not_create_output(office, tmp_path):
    manifest = json.loads(office.runtime.manifest_path.read_bytes())
    worker = Path(manifest["worker"])
    original = worker.read_bytes()
    try:
        worker.write_bytes(original + b"\n# changed\n")
        response = call(office, "office_create", {"format": "docx", "output": "bad.docx", "content": "x"}, "integrity")
        assert response["code"] == "RUNTIME_UNAVAILABLE", response
        assert not (tmp_path / "bad.docx").exists()
    finally:
        worker.write_bytes(original)

def test_prepared_save_recovers_after_crash_without_overwriting(office, tmp_path, monkeypatch):
    import pytest
    from tools.office.gateway import OfficeGateway
    created = call(office, "office_create", {"format": "docx", "output": "recover.docx",
                   "content": "# Crash recovery"}, "create")
    original_record = office._record
    def interrupted(op, response, **kwargs):
        if response["checks"]["saved"]:
            raise RuntimeError("simulated process loss after file publication")
        return original_record(op, response, **kwargs)
    monkeypatch.setattr(office, "_record", interrupted)
    with pytest.raises(RuntimeError, match="simulated"):
        call(office, "office_finish", {"file_id": created["file_id"]}, "first-finish")
    original = (tmp_path / "recover.docx").read_bytes()
    reopened = OfficeGateway(office.runtime, read_roots=(tmp_path,), write_roots=(tmp_path,),
                             cwd=tmp_path, context=office.context)
    recovered = call(reopened, "office_finish", {"file_id": created["file_id"]}, "reconcile-finish")
    assert recovered["checks"]["saved"] and recovered["status"] == "ok", recovered
    assert (tmp_path / "recover.docx").read_bytes() == original


def test_prepared_save_recovers_before_publication(office,tmp_path,monkeypatch):
    import pytest
    from tools.office.gateway import OfficeGateway
    created = call(office,"office_create",{"format":"docx","output":"before.docx","content":"Before publication"},"before-create")
    def lose_process(*args):
        raise RuntimeError("process lost before publication")
    monkeypatch.setattr(office,"_atomic_new_file",lose_process)
    with pytest.raises(RuntimeError,match="before publication"):
        call(office,"office_finish",{"file_id":created["file_id"]},"before-finish")
    assert not (tmp_path/"before.docx").exists()
    reopened = OfficeGateway(office.runtime,read_roots=(tmp_path,),write_roots=(tmp_path,),cwd=tmp_path,context=office.context)
    recovered = call(reopened,"office_finish",{"file_id":created["file_id"]},"before-recovery")
    assert recovered["checks"]["saved"] and (tmp_path/"before.docx").is_file(),recovered

def test_cancelled_edit_and_stale_intent_keep_current_version(office):
    async def cancel():
        created = await office.execute("office_create",{"format":"docx","output":"cancel.docx","content":"Original"},"cancel-create")
        entered = asyncio.Event()
        invoke = office.runtime.invoke
        async def wait(*args,**kwargs):
            entered.set()
            await asyncio.Future()
        office.runtime.invoke = wait
        task = asyncio.create_task(office.execute("office_edit",{"file_id":created["file_id"],"action":"append_section","content":"Lost"},"cancel-edit"))
        await entered.wait()
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        office.runtime.invoke = invoke
        changed = await office.execute("office_edit",{"file_id":created["file_id"],"action":"append_section","content":"Committed"},"after-cancel")
        assert changed["status"]=="ok",changed
        stale = office.op_key("office_edit","crashed")
        office._begin(stale,"office_edit",{"file_id":created["file_id"],"action":"append_section","content":"Never committed"})
        with office.db() as db: db.execute("UPDATE operations SET created=0 WHERE id=?",(stale,))
        recovered = await office.execute("office_edit",{"file_id":created["file_id"],"action":"append_section","content":"Recovered"},"after-crash")
        assert recovered["status"]=="ok",recovered
        with pytest.raises(Exception,match="retired"):
            office._record(stale,{"file_id":created["file_id"]})
        read = await office.execute("office_read",{"file_id":created["file_id"],"view":"text"},"cancel-read")
        text = str(read["data"])
        assert "Original" in text and "Committed" in text and "Recovered" in text
        assert "Lost" not in text and "Never committed" not in text
    import pytest
    asyncio.run(cancel())

def test_large_read_is_bounded_with_usable_continuation(office):
    content = json.dumps([["z"*1000]*3 for _ in range(4)])
    created = call(office, "office_create", {"format":"xlsx","output":"long.xlsx","content":content}, "create-long")
    file_id = created["file_id"]
    assert call(office, "office_edit", {"file_id":file_id,"action":"append_rows","content":content}, "append-long")["status"] == "ok"
    read = call(office, "office_read", {"file_id":file_id,"target":"A1:C8","view":"formulas"}, "read-long")
    assert len(json.dumps(read, ensure_ascii=False)) < 18000
    assert read["data"]["truncated"] and read["data"]["next_target"]
    rows = list(read["data"]["rows"])
    for index in range(10):
        target = read["data"].get("next_target")
        if not target:
            break
        read = call(office, "office_read", {"file_id":file_id,"target":target,"view":"formulas"}, "read-rest-"+str(index))
        assert len(json.dumps(read, ensure_ascii=False)) < 18000
        rows.extend(read["data"]["rows"])
    assert rows == json.loads(content) * 2
