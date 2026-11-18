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


def test_external_formula_text_is_preserved_by_create_and_edit_without_recalculation(office):
    formula = '=WEBSERVICE("https://example.invalid")'
    created = call(office, "office_create", {
        "format": "xlsx", "output": "formulas.xlsx",
        "content": json.dumps({"sheets": [{"name": "Data", "rows": [["Name", "Data"], ["fixture", formula]]}]}),
    }, "inert-create")
    assert created["status"] == "ok", created
    first = call(office, "office_read", {
        "file_id": created["file_id"], "target": "Data!A1:B2", "view": "formulas",
    }, "inert-read")
    assert first["data"]["rows"][1] == ["fixture", formula]
    edited = call(office, "office_edit", {
        "file_id": created["file_id"], "action": "set_cells",
        "target": "Data!B2", "content": json.dumps([[formula]]),
    }, "inert-edit")
    assert edited["status"] == "ok", edited
    saved = call(office, "office_finish", {"file_id": created["file_id"]}, "inert-finish")
    assert saved["status"] == "ok" and saved["data"]["recalculation"] == "not_performed", saved


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



def test_read_cursor_reconstructs_wide_workbook_and_binds_version(office, tmp_path):
    from tools.office.gateway import OfficeGateway

    long_text = ("alpha-\u03b1\n" * 1550)
    original_rows = [[long_text] + [f"a-{col}" for col in range(2, 107)],
                     [f"b-{col}" for col in range(1, 107)]]
    created = call(office, "office_create", {
        "format": "xlsx", "output": "wide.xlsx",
        "content": json.dumps(original_rows, ensure_ascii=False),
    }, "wide-create")
    assert created["status"] == "ok", created
    file_id = created["file_id"]
    first = call(office, "office_read", {
        "file_id": file_id, "target": "A1:DB2", "view": "formulas",
    }, "wide-first")
    assert first["status"] == "ok" and first["next"], first
    first_cursor = first["next"]["arguments"]["cursor"]
    assert first["data"]["next_cursor"] == first_cursor

    reopened = OfficeGateway(office.runtime, read_roots=(tmp_path,), write_roots=(tmp_path,),
                             cwd=tmp_path, context=office.context)
    mismatch = call(reopened, "office_read", {
        "file_id": file_id, "cursor": first_cursor, "target": "A1:A1",
    }, "wrong-target")
    assert mismatch["code"] == "INVALID_CURSOR", mismatch
    wrong_token = call(reopened, "office_read", {
        "file_id": file_id, "cursor": first_cursor + "x",
    }, "wrong-cursor")
    assert wrong_token["code"] == "INVALID_CURSOR", wrong_token

    other = OfficeGateway(office.runtime, read_roots=(tmp_path,), write_roots=(tmp_path,),
                          cwd=tmp_path, context={**office.context, "hashi_session_id": "other"})
    other_file = call(other, "office_create", {
        "format": "xlsx", "output": "other.xlsx", "content": "[[1]]",
    }, "other-create")
    denied = call(other, "office_read", {
        "file_id": other_file["file_id"], "cursor": first_cursor,
    }, "other-cursor")
    assert denied["code"] == "INVALID_CURSOR", denied

    actual = {}
    page = first
    for index in range(200):
        assert len(json.dumps(page, ensure_ascii=False)) < 18000
        for item in page["data"]["cell_fragments"]:
            prior = actual.get(item["cell"], "")
            assert item["offset"] == len(prior)
            actual[item["cell"]] = prior + item["value"]
        if not page["next"]:
            break
        page = call(reopened, "office_read", page["next"]["arguments"], f"wide-page-{index}")
        assert page["status"] == "ok", page
    else:
        raise AssertionError("Office read continuation did not terminate")
    def column_name(col):
        result = ""
        while col:
            col, remainder = divmod(col - 1, 26)
            result = chr(65 + remainder) + result
        return result

    expected = {f"{column_name(col)}{row}": value
                for row, values in enumerate(original_rows, 1)
                for col, value in enumerate(values, 1)}
    assert actual == expected

    changed = call(office, "office_edit", {
        "file_id": file_id, "action": "set_cells", "target": "A1", "content": '[["changed"]]',
    }, "wide-edit")
    assert changed["status"] == "ok", changed
    stale = call(office, "office_read", {
        "file_id": file_id, "cursor": first_cursor,
    }, "wide-stale")
    assert stale["code"] == "STALE_CURSOR", stale




def test_read_cursor_covers_more_than_a_thousand_cells(office):
    rows = [[row * 10 + col for col in range(6)] for row in range(205)]
    created = call(office, "office_create", {
        "format": "xlsx", "output": "many.xlsx", "content": json.dumps(rows),
    }, "many-create")
    assert created["status"] == "ok", created
    page = call(office, "office_read", {
        "file_id": created["file_id"], "target": "A1:F205", "view": "formulas",
    }, "many-first")
    actual = []
    for index in range(20):
        assert page["status"] == "ok", page
        actual.extend(page["data"]["rows"])
        if not page["next"]:
            break
        page = call(office, "office_read", page["next"]["arguments"], f"many-page-{index}")
    else:
        raise AssertionError("large range continuation did not terminate")
    assert actual == rows



def test_word_long_paragraph_and_more_than_fifty_items_continue(office):
    long_text = "word-\u03b1" * 1800
    paragraphs = [long_text] + [f"paragraph-{i}" for i in range(2, 66)]
    created = call(office, "office_create", {
        "format": "docx", "output": "long-word.docx",
        "content": json.dumps({"sections": paragraphs}, ensure_ascii=False),
    }, "word-pages-create")
    assert created["status"] == "ok", created
    page = call(office, "office_read", {
        "file_id": created["file_id"], "target": "paragraph:1-65", "view": "text",
    }, "word-pages-first")
    actual = {}
    for index in range(200):
        assert page["status"] == "ok", page
        assert len(json.dumps(page, ensure_ascii=False)) < 18000
        for fragment in page["data"]["paragraph_fragments"]:
            previous = actual.get(fragment["index"], "")
            assert fragment["offset"] == len(previous)
            actual[fragment["index"]] = previous + fragment["text"]
        if not page["next"]:
            break
        page = call(office, "office_read", page["next"]["arguments"], f"word-page-{index}")
    else:
        raise AssertionError("Word read continuation did not terminate")
    assert [actual[i] for i in range(1, 66)] == paragraphs



def test_word_wide_table_long_cells_have_complete_continuation(office):
    long_text = "table-\u03b1" * 1400
    rows = [[long_text] + [f"a-{i}" for i in range(2, 56)],
            [f"b-{i}" for i in range(1, 56)]]
    created = call(office, "office_create", {
        "format": "docx", "output": "wide-table.docx",
        "content": json.dumps({"sections": [{"table": rows}]}, ensure_ascii=False),
    }, "table-create")
    assert created["status"] == "ok", created
    page = call(office, "office_read", {
        "file_id": created["file_id"], "target": "table:1!1:2", "view": "text",
    }, "table-first")
    actual = {}
    for index in range(200):
        assert page["status"] == "ok", page
        assert len(json.dumps(page, ensure_ascii=False)) < 18000
        for item in page["data"]["table_fragments"]:
            key = (item["row"], item["column"])
            prior = actual.get(key, "")
            assert item["offset"] == len(prior)
            actual[key] = prior + item["text"]
        if not page["next"]:
            break
        page = call(office, "office_read", page["next"]["arguments"], f"table-page-{index}")
    else:
        raise AssertionError("Word table continuation did not terminate")
    assert [[actual[(row, col)] for col in range(1, 56)] for row in range(1, 3)] == rows


def test_pdf_long_page_text_can_be_reconstructed(office, tmp_path):
    import os
    import subprocess
    import sys

    source = tmp_path / "long-page.pdf"
    script = '''
import json, pymupdf, sys
doc = pymupdf.open()
page = doc.new_page(width=1000, height=3000)
for i in range(160):
    page.insert_text((20, 20 + i * 15), f"{i:03d} " + "Long text marker " * 5, fontsize=8)
doc.save(sys.argv[1])
doc.close()
with pymupdf.open(sys.argv[1]) as doc:
    print(json.dumps(doc[0].get_text()))
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(source)],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert process.returncode == 0, process.stderr
    expected = json.loads(process.stdout)
    assert len(expected) > 12000
    inspected = call(office, "office_inspect", {"path": str(source)}, "pdf-inspect")
    assert inspected["status"] == "ok", inspected
    page = call(office, "office_read", {
        "file_id": inspected["file_id"], "target": "page:1", "view": "text",
    }, "pdf-first")
    actual = ""
    for index in range(200):
        assert page["status"] == "ok", page
        assert len(json.dumps(page, ensure_ascii=False)) < 18000
        for item in page["data"]["page_fragments"]:
            assert item["index"] == 1 and item["offset"] == len(actual)
            actual += item["text"]
        if not page["next"]:
            break
        page = call(office, "office_read", page["next"]["arguments"], f"pdf-page-{index}")
    else:
        raise AssertionError("PDF continuation did not terminate")
    assert actual == expected



def test_slide_large_shapes_and_tables_have_complete_continuation(office, tmp_path):
    import os
    import subprocess

    source = tmp_path / "wide-slide.pptx"
    script = '''
import json, sys
from pptx import Presentation
from pptx.util import Inches
deck = Presentation()
slide = deck.slides.add_slide(deck.slide_layouts[6])
shape_texts = []
for i in range(55):
    text = ("first-shape-" * 600) if i == 0 else f"shape-{i}"
    slide.shapes.add_textbox(Inches(0), Inches(0), Inches(1), Inches(1)).text = text
    shape_texts.append(text)
table = slide.shapes.add_table(12, 11, Inches(0), Inches(0), Inches(8), Inches(4)).table
cells = []
for row in range(12):
    values = []
    for col in range(11):
        value = ("deep-cell-" * 30) if (row, col) == (11, 10) else f"r{row}-c{col}"
        table.cell(row, col).text = value
        values.append(value)
    cells.append(values)
deck.save(sys.argv[1])
print(json.dumps({"shapes": shape_texts, "table": cells}))
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(source)],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert process.returncode == 0, process.stderr
    expected = json.loads(process.stdout)
    inspected = call(office, "office_inspect", {"path": str(source)}, "slide-inspect")
    assert inspected["status"] == "ok", inspected
    page = call(office, "office_read", {
        "file_id": inspected["file_id"], "target": "slide:1", "view": "text",
    }, "slide-first")
    actual = {}
    for index in range(200):
        assert page["status"] == "ok", page
        assert len(json.dumps(page, ensure_ascii=False)) < 18000
        for item in page["data"]["slide_fragments"]:
            key = (item["kind"], item.get("shape"), item.get("row"), item.get("column"))
            prior = actual.get(key, "")
            assert item["offset"] == len(prior)
            actual[key] = prior + item["text"]
        if not page["next"]:
            break
        page = call(office, "office_read", page["next"]["arguments"], f"slide-page-{index}")
    else:
        raise AssertionError("Slide continuation did not terminate")
    assert [actual[("shape_text", i, None, None)] for i in range(1, 56)] == expected["shapes"]
    assert [[actual[("table_cell", 56, r, c)] for c in range(1, 12)]
            for r in range(1, 13)] == expected["table"]



def test_word_late_long_paragraph_continues_after_normal_page(office):
    paragraphs = [f"short-{i}" for i in range(31)] + ["long-" * 1100] + ["last"]
    created = call(office, "office_create", {
        "format": "docx", "output": "late-long.docx",
        "content": json.dumps({"sections": paragraphs}),
    }, "late-word-create")
    assert created["status"] == "ok", created
    page = call(office, "office_read", {
        "file_id": created["file_id"], "target": "paragraph:1-33", "view": "text",
    }, "late-word-first")
    actual = {}
    for i in range(20):
        assert page["status"] == "ok", page
        for item in page["data"].get("paragraphs", []):
            actual[item["index"]] = item["text"]
        for item in page["data"].get("paragraph_fragments", []):
            previous = actual.get(item["index"], "")
            assert item["offset"] == len(previous)
            actual[item["index"]] = previous + item["text"]
        if not page["next"]:
            break
        page = call(office, "office_read", page["next"]["arguments"], f"late-word-{i}")
    else:
        raise AssertionError("Word continuation did not terminate")
    assert [actual[i] for i in range(1, 34)] == paragraphs



def test_slide_summary_continues_past_forty(office, tmp_path):
    import os
    import subprocess

    source = tmp_path / "many-slides.pptx"
    script = '''
import sys
from pptx import Presentation
deck = Presentation()
for i in range(41):
    deck.slides.add_slide(deck.slide_layouts[6])
deck.save(sys.argv[1])
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(source)],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert process.returncode == 0, process.stderr
    inspected = call(office, "office_inspect", {"path": str(source)}, "many-slide-inspect")
    assert inspected["status"] == "ok", inspected
    page = call(office, "office_read", {"file_id": inspected["file_id"], "view": "summary"},
                "many-slide-summary")
    seen = [item["index"] for item in page["data"]["structure"]["slides"]]
    for index in range(20):
        if not page["next"]:
            break
        page = call(office, "office_read", page["next"]["arguments"], f"many-slide-page-{index}")
        assert page["status"] == "ok", page
        seen.extend(item["index"] for item in page["data"]["slides"])
    else:
        raise AssertionError("Slide summary continuation did not terminate")
    assert seen == list(range(1, 42))



def test_word_summary_truncation_has_recoverable_continuations(office):
    headings = ["heading-" + ("a" * 200)] + [f"heading-{i}" for i in range(2, 33)]
    sections = [{"heading": heading} for heading in headings]
    sections += [{"table": [[f"table-{i}"]]} for i in range(1, 33)]
    created = call(office, "office_create", {
        "format": "docx", "output": "many-headings.docx",
        "content": json.dumps({"sections": sections}),
    }, "summary-create")
    assert created["status"] == "ok", created
    page = call(office, "office_read", {
        "file_id": created["file_id"], "view": "summary",
    }, "summary-first")
    assert page["status"] == "ok", page
    assert page["data"]["structure"]["headings_truncated"] and page["next"]
    seen_headings = []
    table_next = None
    for index in range(20):
        if not page["next"]:
            break
        page = call(office, "office_read", page["next"]["arguments"], f"heading-page-{index}")
        assert page["status"] == "ok", page
        seen_headings.extend(item["text"] for item in page["data"].get("paragraphs", [])
                             if item["style"].startswith("Heading"))
        table_next = table_next or page["data"].get("tables_next")
    else:
        raise AssertionError("Heading continuation did not terminate")
    assert seen_headings == headings
    assert table_next
    page = call(office, "office_read", table_next["arguments"], "table-list-rest")
    assert page["status"] == "ok", page
    assert [item["index"] for item in page["data"]["table_summaries"]] == [31, 32]



def test_slide_summary_exposes_truncated_title_continuation(office, tmp_path):
    import os
    import subprocess

    source = tmp_path / "long-title.pptx"
    title = "title-" * 80
    script = '''
import sys
from pptx import Presentation
deck = Presentation()
slide = deck.slides.add_slide(deck.slide_layouts[5])
slide.shapes.title.text = "title-" * 80
deck.save(sys.argv[1])
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(source)],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert process.returncode == 0, process.stderr
    inspected = call(office, "office_inspect", {"path": str(source)}, "title-inspect")
    assert inspected["status"] == "ok", inspected
    summary = call(office, "office_read", {"file_id": inspected["file_id"], "view": "summary"}, "title-summary")
    assert summary["data"]["structure"]["title_truncated"] and summary["next"]
    page = call(office, "office_read", summary["next"]["arguments"], "title-details")
    assert page["status"] == "ok", page
    assert page["data"]["slides"][0]["shapes"][0]["text"] == title



def test_large_page_and_slide_counts_allow_targeted_reads(office, tmp_path):
    import os
    import subprocess

    pdf = tmp_path / "many-pages.pdf"
    slides = tmp_path / "many-slides.pptx"
    script = '''
import pymupdf, sys
from pptx import Presentation
from pptx.util import Inches
pdf = pymupdf.open()
for index in range(501):
    page = pdf.new_page()
    if index == 500:
        page.insert_text((30, 40), "last page marker")
pdf.save(sys.argv[1])
pdf.close()
deck = Presentation()
for index in range(201):
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    if index == 200:
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text = "last slide marker"
deck.save(sys.argv[2])
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(pdf), str(slides)],
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert process.returncode == 0, process.stderr
    for path, target, key, marker in (
        (pdf, "page:501", "pages", "last page marker"),
        (slides, "slide:201", "slides", "last slide marker"),
    ):
        inspected = call(office, "office_inspect", {"path": str(path)}, "large-" + key)
        assert inspected["status"] == "ok", inspected
        observed = call(office, "office_read", {
            "file_id": inspected["file_id"], "target": target, "view": "text",
        }, "last-" + key)
        assert observed["status"] == "ok" and marker in json.dumps(observed["data"]), observed



def test_sparse_workbook_with_many_sheets_allows_targeted_reads(office, tmp_path):
    import os
    import subprocess

    source = tmp_path / "sparse-many-sheets.xlsx"
    script = '''
import sys
from openpyxl import Workbook
book = Workbook()
book.active["A1"] = "first"
book.active.cell(500000, 14000, "far")
for i in range(2, 102):
    sheet = book.create_sheet(f"Sheet{i}")
    sheet["A1"] = f"sheet-{i}"
book.save(sys.argv[1])
book.close()
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(source)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert process.returncode == 0, process.stderr
    inspected = call(office, "office_inspect", {"path": str(source)}, "sparse-inspect")
    assert inspected["status"] == "ok", inspected
    for target, expected in (("Sheet!A1", "first"), ("Sheet101!A1", "sheet-101")):
        observed = call(office, "office_read", {
            "file_id": inspected["file_id"], "target": target, "view": "values",
        }, "sparse-" + expected)
        assert observed["status"] == "ok" and observed["data"]["rows"] == [[expected]], observed



def test_large_repetitive_workbook_metadata_allows_targeted_read(office, tmp_path):
    import os
    import subprocess

    source = tmp_path / "large-workbook-metadata.xlsx"
    script = '''
import sys, zipfile
from pathlib import Path
from openpyxl import Workbook
source = Path(sys.argv[1])
book = Workbook()
book.active["A1"] = "metadata marker"
book.save(source)
book.close()
with zipfile.ZipFile(source) as old:
    members = [(item.filename, old.read(item.filename)) for item in old.infolist()]
with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as new:
    for name, raw in members:
        if name == "xl/workbook.xml":
            assert b"</workbook>" in raw
            raw = raw.replace(b"</workbook>", b"<!--" + b"x" * (2 * 1024 * 1024 + 1) + b"--></workbook>")
        new.writestr(name, raw)
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(source)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert process.returncode == 0, process.stderr
    inspected = call(office, "office_inspect", {"path": str(source)}, "large-metadata-inspect")
    assert inspected["status"] == "ok", inspected
    observed = call(office, "office_read", {
        "file_id": inspected["file_id"], "target": "A1", "view": "values",
    }, "large-metadata-read")
    assert observed["status"] == "ok" and observed["data"]["rows"] == [["metadata marker"]], observed



def test_explicit_file_budget_allows_large_pdf(office, tmp_path, monkeypatch):
    import os
    import subprocess
    from tools.office.gateway import OfficeGateway
    from tools.office.runtime import OfficeRuntime

    source = tmp_path / "large-attachment.pdf"
    script = '''
import os, pymupdf, sys
doc = pymupdf.open()
page = doc.new_page()
page.insert_text((30, 40), "large PDF marker")
doc.embfile_add("random.bin", os.urandom(33 * 1024 * 1024))
doc.save(sys.argv[1])
doc.close()
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(source)],
                             capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert process.returncode == 0, process.stderr
    assert source.stat().st_size > 32 * 1024 * 1024
    restricted_options = {**office.runtime.options, "resource_budgets": {"file_bytes": 32 * 1024 * 1024}}
    restricted = OfficeGateway(OfficeRuntime(restricted_options), read_roots=(tmp_path,), write_roots=(tmp_path,),
                               cwd=tmp_path, context=office.context)
    denied = call(restricted, "office_inspect", {"path": str(source)}, "restricted-file-budget")
    assert denied["code"] == "FILE_LIMIT" and "file_bytes" in denied["summary"], denied
    options = {**office.runtime.options, "resource_budgets": {"file_bytes": 40 * 1024 * 1024}}
    configured = OfficeGateway(OfficeRuntime(options), read_roots=(tmp_path,), write_roots=(tmp_path,),
                               cwd=tmp_path, context=office.context)
    accepted = call(configured, "office_inspect", {"path": str(source)}, "larger-file-budget")
    assert accepted["status"] == "ok", accepted
    observed = call(configured, "office_read", {
        "file_id": accepted["file_id"], "target": "page:1", "view": "text",
    }, "large-pdf-read")
    assert observed["status"] == "ok" and "large PDF marker" in json.dumps(observed["data"]), observed
    import tools.office.limits as office_limits
    monkeypatch.setattr(office_limits, "_available_memory", lambda: 8 * 1024**3)
    automatic = OfficeGateway(OfficeRuntime(office.runtime.options), read_roots=(tmp_path,), write_roots=(tmp_path,),
                              cwd=tmp_path, context=office.context)
    auto_result = call(automatic, "office_inspect", {"path": str(source)}, "automatic-file-budget")
    assert auto_result["status"] == "ok", auto_result



def test_materialized_cell_budget_is_configurable(office, tmp_path):
    import os
    import subprocess
    from tools.office.gateway import OfficeGateway
    from tools.office.runtime import OfficeRuntime

    source = tmp_path / "many-cells.xlsx"
    script = '''
import sys
from openpyxl import Workbook
book = Workbook(write_only=True)
sheet = book.create_sheet()
for row in range(1001):
    sheet.append(list(range(200)))
book.save(sys.argv[1])
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(source)],
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert process.returncode == 0, process.stderr
    options = {**office.runtime.options, "resource_budgets": {"materialized_cells": 200_000}}
    restricted = OfficeGateway(OfficeRuntime(options), read_roots=(tmp_path,), write_roots=(tmp_path,),
                               cwd=tmp_path, context=office.context)
    denied = call(restricted, "office_inspect", {"path": str(source)}, "restricted-cell-budget")
    assert denied["code"] == "FILE_LIMIT" and "materialized_cells" in denied["summary"], denied
    options["resource_budgets"] = {"materialized_cells": 250_000}
    configured = OfficeGateway(OfficeRuntime(options), read_roots=(tmp_path,), write_roots=(tmp_path,),
                               cwd=tmp_path, context=office.context)
    accepted = call(configured, "office_inspect", {"path": str(source)}, "larger-cell-budget")
    assert accepted["status"] == "ok", accepted
    observed = call(configured, "office_read", {
        "file_id": accepted["file_id"], "target": "A1:B1", "view": "values",
    }, "many-cells-read")
    assert observed["status"] == "ok" and observed["data"]["rows"] == [[0, 1]], observed



def test_large_word_file_edit_roundtrips_its_media(office, tmp_path):
    import hashlib
    import os
    import subprocess
    from tools.office.gateway import OfficeGateway
    from tools.office.runtime import OfficeRuntime

    source = tmp_path / "large-word.docx"
    script = '''
import os, sys
from pathlib import Path
from PIL import Image
from docx import Document
from docx.shared import Inches
root = Path(sys.argv[1]).parent
image = root / "random.png"
Image.frombytes("RGB", (3400, 3400), os.urandom(3400 * 3400 * 3)).save(image, compress_level=1)
doc = Document()
doc.add_paragraph("original text")
doc.add_picture(str(image), width=Inches(3))
doc.save(sys.argv[1])
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(source)],
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert process.returncode == 0, process.stderr
    assert source.stat().st_size > 32 * 1024 * 1024
    with zipfile.ZipFile(source) as package:
        image_name = next(name for name in package.namelist() if name.startswith("word/media/"))
        original_image = hashlib.sha256(package.read(image_name)).hexdigest()
    options = {**office.runtime.options, "resource_budgets": {
        "file_bytes": 48 * 1024 * 1024, "expanded_bytes": 96 * 1024 * 1024,
    }}
    configured = OfficeGateway(OfficeRuntime(options), read_roots=(tmp_path,), write_roots=(tmp_path,),
                               cwd=tmp_path, context=office.context)
    inspected = call(configured, "office_inspect", {"path": str(source)}, "large-word-inspect")
    assert inspected["status"] == "ok", inspected
    edited = call(configured, "office_edit", {
        "file_id": inspected["file_id"], "action": "append_section", "content": "appended text",
    }, "large-word-edit")
    assert edited["status"] == "ok", edited
    finished = call(configured, "office_finish", {
        "file_id": inspected["file_id"], "output": str(tmp_path / "large-edited.docx"),
    }, "large-word-finish")
    assert finished["status"] == "ok" and finished["checks"]["saved"], finished
    with zipfile.ZipFile(tmp_path / "large-edited.docx") as package:
        assert hashlib.sha256(package.read(image_name)).hexdigest() == original_image
        assert b"appended text" in package.read("word/document.xml")



def test_complex_word_append_and_copy_preserve_original_features(office, tmp_path):
    import hashlib
    import os
    import subprocess

    source = tmp_path / "complex.docx"
    script = '''
import sys
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
doc = Document()
paragraph = doc.add_paragraph()
run = paragraph.add_run("reviewed text")
doc.add_comment(run, text="retain this comment", author="Reviewer", initials="R")
link = OxmlElement("w:hyperlink")
link.set(qn("r:id"), doc.part.relate_to("https://example.invalid/link", RT.HYPERLINK, is_external=True))
link_run = OxmlElement("w:r")
link_text = OxmlElement("w:t")
link_text.text = "link text"
link_run.append(link_text)
link.append(link_run)
paragraph._p.append(link)
inserted = OxmlElement("w:ins")
inserted.set(qn("w:id"), "7")
inserted.set(qn("w:author"), "Reviewer")
revision_run = OxmlElement("w:r")
revision_text = OxmlElement("w:t")
revision_text.text = "tracked text"
revision_run.append(revision_text)
inserted.append(revision_run)
paragraph._p.append(inserted)
doc.save(sys.argv[1])
'''
    process = subprocess.run([os.environ["HASHI_TEST_OFFICE_PYTHON"], "-c", script, str(source)],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert process.returncode == 0, process.stderr
    before_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with zipfile.ZipFile(source) as package:
        original = {info.filename: package.read(info) for info in package.infolist()}
    assert "word/comments.xml" in original
    assert b"w:ins" in original["word/document.xml"]
    assert b"example.invalid" in original["word/_rels/document.xml.rels"]

    inspected = call(office, "office_inspect", {"path": str(source)}, "complex-inspect")
    assert inspected["status"] == "ok", inspected
    file_id = inspected["file_id"]
    edited = call(office, "office_edit", {
        "file_id": file_id, "action": "append_section", "content": "appended & safe | literal\tcolumn",
    }, "complex-append")
    assert edited["status"] == "ok", edited
    saved = call(office, "office_finish", {
        "file_id": file_id, "output": str(tmp_path / "complex-edited.docx"),
    }, "complex-save")
    assert saved["status"] == "ok" and saved["checks"]["saved"], saved
    with zipfile.ZipFile(tmp_path / "complex-edited.docx") as package:
        changed = {info.filename: package.read(info) for info in package.infolist()}
    assert set(changed) == set(original)
    for name in original:
        if name != "word/document.xml":
            assert changed[name] == original[name], name
    insertion = (b'<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                 b'<w:r><w:t xml:space="preserve">appended &amp; safe | literal</w:t>'
                 b'<w:tab/><w:t xml:space="preserve">column</w:t></w:r></w:p>')
    assert changed["word/document.xml"].count(insertion) == 1
    assert changed["word/document.xml"].replace(insertion, b"", 1) == original["word/document.xml"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_digest

    other = call(office, "office_inspect", {"path": str(source)}, "complex-reinspect")
    denied = call(office, "office_edit", {
        "file_id": other["file_id"], "action": "set_text", "target": "paragraph:1", "content": "changed",
    }, "complex-unsupported")
    assert denied["status"] == "error" and denied["code"] in {"EXTERNAL_RESOURCE", "TRACKED_CHANGES", "FIDELITY_UNSUPPORTED"}, denied
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_digest

    clone = call(office, "office_create", {
        "format": "docx", "output": "complex-clone.docx", "template": str(source),
    }, "complex-clone")
    assert clone["status"] == "ok", clone
    completed = call(office, "office_finish", {"file_id": clone["file_id"]}, "complex-clone-save")
    assert completed["status"] == "ok", completed
    assert (tmp_path / "complex-clone.docx").read_bytes() == source.read_bytes()

    signed = tmp_path / "signed.docx"
    with zipfile.ZipFile(source) as package, zipfile.ZipFile(signed, "w") as output:
        for item in package.infolist():
            output.writestr(item, package.read(item))
        output.writestr("_xmlsignatures/sig1.xml", "<Signature/>")
    signed_digest = hashlib.sha256(signed.read_bytes()).hexdigest()
    signed_handle = call(office, "office_inspect", {"path": str(signed)}, "signed-inspect")
    assert signed_handle["status"] == "ok", signed_handle
    signature_denied = call(office, "office_edit", {
        "file_id": signed_handle["file_id"], "action": "append_section", "content": "extra",
    }, "signed-append")
    assert signature_denied["code"] == "SIGNATURE_UNSUPPORTED", signature_denied
    assert hashlib.sha256(signed.read_bytes()).hexdigest() == signed_digest
