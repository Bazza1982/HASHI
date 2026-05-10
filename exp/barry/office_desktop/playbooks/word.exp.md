# Word EXP

## Intent

Produce polished Word reports on Barry's HASHI Windows desktop with structured
headings, tables, page numbers, and reliable export behavior.

## Context

Known to apply to Microsoft Word on the HASHI Windows desktop with
`use_computer`, `windows_helper`, and optional Office object automation.

## Procedure

1. On HASHI1, use the docx-first route for automated Word output:
   create or copy the `.docx` before Word starts, then use Word only to open,
   visually validate, export PDF, and close it. The reusable local tool is
   `python3 tools/word_docx_first.py`.
2. Use a template or object-level document construction for consistent layout.
3. Keep report hierarchy explicit with title, headings, summary, body sections,
   tables, and footer.
4. For real UI validation, open the file in Word, capture the editing view, and
   verify content is visible.
5. For PDF output, use File > Export > Create PDF/XPS rather than Save As with a
   `.pdf` filename.
6. Validate the saved `.docx` structurally when possible: headings, table XML,
   footer, and generated file size.

## Evidence to keep

- final `.docx`
- final `.pdf` when requested
- screenshot of Word editing view
- validation notes or report JSON

## Recovery

- If text entry does not land in the document, retry with clipboard paste.
- If a `.pdf.docx` appears, discard it and export through Word's PDF/XPS flow.
- If table conversion through UI is unreliable, create the table through object
  automation and then validate in Word UI.
- On HASHI1, do not use `Documents.Add() -> SaveAs2()` as the creation path.
  Post-migration testing showed it hangs at `SaveAs2`. Use docx-first instead:
  generate/copy a `.docx`, then run `tools/word_docx_first.py export`.

## Scope limit

Do not assume the same key paths work on non-Windows Word or a different Office
language pack without revalidation.
