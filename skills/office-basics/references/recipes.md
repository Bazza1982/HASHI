# Office recipes

Use the five registered tools; these recipes are actions, not extra tools.
Inspect inputs first. Reuse returned IDs and actual sheet/section/slide names.
Finish saves a new file, structurally checks it and optionally publishes it.
Keep saved, recalculated, rendered and delivered facts separate.

## Excel

Creation accepts CSV text or JSON:
```json
{"sheets":[{"name":"Sales","rows":[["Team","Amount"],["A",10],["B",20]]}]}
```

Edit content is a JSON string unless a plain formula/text is shown:

| Action | Target | Content |
|---|---|---|
| set_cells | Sales!B2:C3 | [[10,20],[30,40]] |
| set_formula | Sales!D2 | =SUM(B2:C2) |
| append_rows | Sales!A1 | [["C",30]] |
| format_range | Sales!B2:B20 | {"number_format":"#,##0.00","bold":true,"fill":"E8F0FE"} |
| excel_clean | Sales!A1 | {"deduplicate":true,"drop_blank":true} |
| excel_summary | Sales!A1 | {"group_by":"Team","value":"Amount","aggregation":"sum","sheet":"Summary"} |
| excel_reconcile | Summary!A1 | {"key":"Team","sheet":"Reconciliation"} |
| excel_chart | Summary!A1:B10 | {"type":"bar","title":"Totals","anchor":"D2"} |

Reconcile also requires source_id from the second inspected workbook.
Summary supports sum, count and average. Reconciliation refuses duplicate keys.
Cleaning trims text, removes empty rows and optionally removes duplicate rows.
Charts support bar, line and pie, including native editable chart parts.

openpyxl stores formula text. To recalculate with a qualified converter:
office_edit action=office_convert, content=xlsx. Verify cached numeric values with
office_read view=values. Without a converter, report CONVERTER_UNAVAILABLE.
Long numeric identifiers should be quoted text: Excel numbers have 15 digits.

## Word reports and templates

Create from Markdown or structured content:
```json
{"title":"Monthly report","sections":[
  {"heading":"Summary","text":"Sales increased."},
  {"heading":"Results","table":[["Team","Amount"],["A",10]]},
  {"image":"authorized-chart.png"}
]}
```

Use template with an existing DOCX/XLSX/PPTX path. Template-only creation
preserves the file; supplied sections/slides append to its existing content.
style is clean, professional or academic.

word_report appends Markdown/sections, or uses source_id data.
word_format/apply_style accepts {"font":"Calibri","size":11,"line_spacing":1.15}
or a preset string such as academic.
replace_text accepts {"find":"exact phrase","replace":"new phrase"} and retains
unaffected runs. Missing text is an error, not a successful no-op.
add_table takes JSON rows; set_table uses target=table:1.
add_image accepts {"image":"authorized-image.png"}.
set_text uses target=paragraph:1 and replaces that paragraph deliberately.

## PowerPoint

slides_from_outline/add_slide accepts a Markdown outline or:
```json
{"slides":[{"title":"Sales","text":["Highlights","Next steps"],
 "chart":{"type":"bar","categories":["A","B"],
          "series":[{"name":"Sales","values":[10,20]}]}}]}
```

Slides may contain table (JSON rows) and image (authorized PNG/JPEG path).
Use target=slide:1 for add_chart/add_table/add_image.
Use target=slide:1/shape:3 for set_text, set_table or update_chart.
update_chart content uses categories and named series as above.
apply_style supports preset strings or {"font":"Calibri","size":20}.
Templates retain their existing theme and objects; new slides use its layouts.

## PDF, conversion and delivery

PDF is read/render/convert capability, not general PDF editing or OCR.
Read a page range with target=page:1-3, view=text.
office_convert supports Office -> PDF with a qualified LibreOffice runtime,
XLSX -> UTF-8 CSV (active sheet), and PDF -> DOCX text extraction. PDF-to-DOCX
does not preserve layout. Complex conversion fidelity depends on the renderer.

office_finish preview=true creates PDF/PNG previews where supported. Rendered
does not mean visually reviewed. deliver=true uses the originating Session's
existing HASHI publication service; queued/accepted is not delivered.

Macros are never executed. Macros, revisions, comments, external resources,
unsupported objects, corrupt/encrypted/oversized files and fidelity loss fail
conservatively. An error retains originals and the previous working version.
Do not bypass a rejection with a Python script or a package installation.
