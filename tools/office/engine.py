"""Office Runtime implementation. Imported ONLY by the isolated worker.

All paths are gateway-staged basenames in one invocation directory. Documents
are untrusted data: no macros, external resources, scripts or formula execution.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import posixpath
import subprocess
from copy import copy
from formatting import PRESETS, word_preset, slide_preset, slide_and_shape, add_native_chart, set_table, regions, picture_fit, free_region
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile

from package_probe import inspect_ooxml_package, OfficePackageError, OfficePackageBudgetError
from limits import resolve_budgets

BUDGETS = resolve_budgets()
MAX_BYTES = BUDGETS["file_bytes"]
MAX_CELLS = BUDGETS["materialized_cells"]


def configure_budgets(values=None):
    global BUDGETS, MAX_BYTES, MAX_CELLS
    BUDGETS = resolve_budgets(values)
    MAX_BYTES = BUDGETS["file_bytes"]
    MAX_CELLS = BUDGETS["materialized_cells"]


MAX_RESULT_CELLS = 1000
FORMATS = {"xlsx": "excel", "docx": "word", "pptx": "slides", "pdf": "pdf", "csv": "excel"}
RECIPES = ("excel_summary", "excel_reconcile", "excel_clean", "excel_chart",
           "word_report", "word_format", "slides_from_outline", "office_convert")


class OfficeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def local(job, name):
    if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
        raise OfficeError("INVALID_STAGE", "Expected a gateway-staged file.")
    path = job / name
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise OfficeError("INVALID_STAGE", "Linked staging files are not accepted.")
    return path


def payload(text):
    if not text:
        return None
    if text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise OfficeError("INVALID_CONTENT", "Content starts with JSON but is invalid.") from exc
    return text


def scalar(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)


def read_bytes(path):
    if not path.is_file():
        raise OfficeError("FILE_LIMIT", "File is missing.")
    if path.stat().st_size > MAX_BYTES:
        raise OfficeError("FILE_LIMIT", f"Input {path.stat().st_size} bytes exceeds file_bytes budget {MAX_BYTES}; increase the configured resource budget.")
    return path.read_bytes()


def safe_chart_workbook(archive, name):
    """Allow only inert XLSX parts used by native PowerPoint charts."""
    if not re.fullmatch(r"ppt/embeddings/[^/]+\.xlsx", name, re.I):
        return False
    from defusedxml.ElementTree import fromstring
    referenced = False
    for relname in archive.namelist():
        if not relname.endswith(".rels"):
            continue
        for rel in fromstring(archive.read(relname)):
            base = posixpath.dirname(relname.replace("/_rels/", "/"))
            resolved = posixpath.normpath(posixpath.join(base, rel.attrib.get("Target", "")))
            if resolved != name:
                continue
            if (not re.fullmatch(r"ppt/charts/_rels/[^/]+\.xml\.rels", relname)
                    or not rel.attrib.get("Type", "").endswith("/package")):
                return False
            referenced = True
    if not referenced:
        return False
    raw = archive.read(name)
    nested = inspect_ooxml_package(raw, filename="chart.xlsx", budgets=BUDGETS)
    if nested["macro_parts_present"]:
        return False
    with ZipFile(io.BytesIO(raw)) as book:
        for part in book.namelist():
            if any(s in part.lower() for s in ("embeddings/", "externallinks/", "activex", "vba", "signature")):
                return False
            if part.endswith((".xml", ".rels")):
                xml = book.read(part)
                if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                    return False
                if part.endswith(".rels") and re.search(rb'TargetMode\\s*=\\s*["\\\']External["\\\']', xml, re.I):
                    return False
    import openpyxl
    workbook = openpyxl.load_workbook(io.BytesIO(raw), keep_links=False)
    try:
        dimensions(workbook)
    finally:
        workbook.close()
    return True


def preflight(path, *, editing=False):
    raw = read_bytes(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        if not raw.startswith(b"%PDF-"):
            raise OfficeError("FORMAT_MISMATCH", "Expected a PDF header.")
        import pymupdf
        with pymupdf.open(path) as doc:
            if doc.is_encrypted:
                raise OfficeError("ENCRYPTED_FILE", "Encrypted PDF is unsupported.")
            return {"format": "pdf", "pages": doc.page_count}
    if suffix == ".csv":
        raw.decode("utf-8-sig")
        return {"format": "excel", "csv": True}
    try:
        result = inspect_ooxml_package(raw, filename=path.name, budgets=BUDGETS)
        with ZipFile(io.BytesIO(raw)) as archive:
            for item in archive.infolist():
                name = item.filename
                if item.file_size and item.compress_size == 0:
                    raise OfficeError("INVALID_PACKAGE", "Invalid compressed size.")
                if not name.endswith((".xml", ".rels")):
                    continue
                data = archive.read(item)
                if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                    raise OfficeError("UNSAFE_XML", "DTD/entity declarations are not supported.")
                if editing and name.endswith(".rels") and re.search(
                    rb'TargetMode\s*=\s*["\']External["\']', data, re.I
                ):
                    raise OfficeError("EXTERNAL_RESOURCE", "External relationships require a fidelity-capable workflow.")
                if editing and re.search(rb"<(?:\w+:)?(?:ins|del|moveFrom|moveTo)\b", data):
                    raise OfficeError("TRACKED_CHANGES", "Resolve tracked changes in the source before editing.")
            names = archive.namelist()
            if editing and (result["macro_parts_present"] or suffix in {".xlsm", ".docm", ".pptm"}):
                raise OfficeError("MACRO_UNSUPPORTED", "Macro documents can be inspected but cannot be edited.")
            if editing and any(
                (("embeddings/" in n.lower()) and not safe_chart_workbook(archive, n))
                or any(x in n.lower() for x in (
                    "activex", "externallinks/", "slicer", "vml",
                    "threadedcomment", "pivotcache", "signature", "comments"))
                for n in names):
                raise OfficeError("FIDELITY_UNSUPPORTED", "This document contains objects not safely round-tripped by this runtime.")
        return result
    except OfficePackageBudgetError as exc:
        raise OfficeError("FILE_LIMIT", str(exc)) from exc
    except OfficePackageError as exc:
        raise OfficeError("INVALID_PACKAGE", str(exc)) from exc


def load_book(path, *, values=False):
    import openpyxl
    if path.suffix.lower() == ".csv":
        book = openpyxl.Workbook()
        rows = csv.reader(read_bytes(path).decode("utf-8-sig").splitlines())
        for row in rows:
            book.active.append([number(v) for v in row])
        return book
    return openpyxl.load_workbook(path, data_only=values, keep_links=False, rich_text=True)


def number(value):
    if isinstance(value, str) and re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value.strip()):
        if len(value.replace("-","").replace(".","").lstrip("0")) > 15:
            return value
        return float(value) if "." in value else int(value)
    return value


def dimensions(book):
    # The rectangular extent can be enormous even when only a few cells are
    # present (for example a single formatted cell in the last row). Count
    # materialized cells rather than inventing work for all intervening cells.
    cells = sum(len(sheet._cells) for sheet in book.worksheets)
    if cells > MAX_CELLS:
        raise OfficeError("FILE_LIMIT", f"Workbook has {cells} materialized cells; materialized_cells budget is {MAX_CELLS}. Increase the configured resource budget.")


# Formula text is data during openpyxl reads and edits. An external converter
# can recalculate formulas, so inspect tokens only at that execution boundary.
# Unclassified expressions stay readable/editable but are not sent to a
# converter without a proven isolated evaluation route.
def _formula_conversion_risk(formula):
    from openpyxl.formula import Tokenizer
    from openpyxl.utils import FORMULAE

    try:
        tokens = Tokenizer(formula).items
    except Exception:
        return "unclassified formula syntax"
    stack = []
    external_functions = {
        "WEBSERVICE", "RTD", "DDE", "INDIRECT", "CALL", "EXEC", "RUN",
        "REGISTER.ID", "IMPORTDATA", "IMPORTXML", "IMPORTHTML", "IMPORTFEED",
        "IMPORTRANGE",
    }
    for token in tokens:
        if token.type in {"FUNC", "PAREN", "ARRAY"}:
            if token.subtype == "OPEN":
                stack.append(token.type)
            elif token.subtype == "CLOSE":
                if not stack or stack.pop() != token.type:
                    return "unbalanced formula syntax"
        if token.type == "FUNC" and token.subtype == "OPEN":
            name = token.value[:-1].strip().upper()
            canonical = name.removeprefix("_XLFN.").removeprefix("_XLWS.")
            if canonical in external_functions or canonical.startswith("CUBE"):
                return "formula may evaluate an external resource"
            if canonical not in FORMULAE:
                return "unclassified formula function"
        if token.type == "OPERAND" and token.subtype == "RANGE":
            value = token.value
            if "|" in value or re.search(r"\[[^\]]+\][^!]*!", value):
                return "formula may reference an external resource"
    return "unbalanced formula syntax" if stack else None


def _check_converter_formulas(book):
    def check(value):
        if not isinstance(value, str) or not value:
            reason = "unclassified formula representation"
        else:
            reason = _formula_conversion_risk(value if value.startswith("=") else "=" + value)
        if reason:
            raise OfficeError(
                "EXTERNAL_FORMULA_EVALUATION",
                f"Conversion may evaluate external or unclassified formulas ({reason}); "
                "read and edit remain available without recalculation.",
            )

    dimensions(book)
    name_scopes = [book.defined_names]
    for sheet in book.worksheets:
        name_scopes.append(sheet.defined_names)
        # Openpyxl has already materialized these cells; avoid expanding the
        # entire rectangular extent solely to examine formula expressions.
        for cell in sheet._cells.values():
            if cell.data_type == "f":
                check(cell.value)
    # Named expressions can indirectly refer to external formulas.
    for scope in name_scopes:
        for defined in scope.values():
            check(defined.attr_text)


def _check_converter_connections(archive):
    # Connections and query tables can request refresh independently of cell
    # formulas. They remain readable data; an unisolated converter cannot run
    # them until an explicit, scoped evaluation route is available.
    if any(
        name.casefold() == "xl/connections.xml"
        or name.casefold().startswith((
            "xl/querytables/", "xl/externallinks/", "xl/macrosheets/", "xl/dialogsheets/",
        ))
        for name in archive.namelist()
    ):
        raise OfficeError(
            "EXTERNAL_FORMULA_EVALUATION",
            "Conversion may refresh external workbook connections; "
            "read and edit remain available without recalculation.",
        )


def _check_converter_input(source):
    suffix = source.suffix.lower()
    if suffix in {".xlsx", ".csv"}:
        if suffix == ".xlsx":
            with ZipFile(source) as archive:
                _check_converter_connections(archive)
        book = load_book(source)
        try:
            _check_converter_formulas(book)
        finally:
            book.close()
    elif suffix == ".pptx":
        import openpyxl
        with ZipFile(source) as archive:
            for name in archive.namelist():
                if re.fullmatch(r"ppt/embeddings/[^/]+\.xlsx", name, re.I):
                    raw = archive.read(name)
                    with ZipFile(io.BytesIO(raw)) as nested:
                        _check_converter_connections(nested)
                    embedded = openpyxl.load_workbook(
                        io.BytesIO(raw), keep_links=False, rich_text=True,
                    )
                    try:
                        _check_converter_formulas(embedded)
                    finally:
                        embedded.close()


def describe(path):
    result = preflight(path)
    kind = result["format"]
    if kind == "excel":
        book = load_book(path)
        try:
            dimensions(book)
            result["sheets"] = [{"name": s.title, "rows": s.max_row, "columns": s.max_column,
                                 "charts": len(s._charts), "images": len(s._images)}
                                for s in book]
        finally:
            book.close()
    elif kind == "word":
        from docx import Document
        doc = Document(path)
        paragraphs = doc.paragraphs
        headings = [p.text for p in paragraphs if p.style.name.startswith("Heading")]
        headings_truncated = len(headings) > 30 or any(len(text) > 160 for text in headings[:30])
        result.update(paragraphs=len(paragraphs), tables=len(doc.tables),
                      images=len(doc.inline_shapes), headings=[text[:160] for text in headings[:30]],
                      headings_truncated=headings_truncated)
        if headings_truncated:
            result["truncated"] = True
            result["next_target"] = f"paragraph:1-{len(paragraphs)}"
    elif kind == "slides":
        from pptx import Presentation
        deck = Presentation(path)
        result["slides"] = [{"index": i, "title": s.shapes.title.text[:160] if s.shapes.title else "",
                            "shapes": len(s.shapes),
                            "charts": sum(bool(x.has_chart) for x in s.shapes),
                            "tables": sum(bool(x.has_table) for x in s.shapes)}
                           for i, s in enumerate(deck.slides, 1) if i <= 30]
        result["total_slides"] = len(deck.slides)
        title_truncated = any(slide.shapes.title and len(slide.shapes.title.text) > 160
                              for slide in list(deck.slides)[:30])
        result["title_truncated"] = bool(title_truncated)
        result["truncated"] = len(deck.slides) > 30 or bool(title_truncated)
        result["next_target"] = (f"slide:1-{len(deck.slides)}" if title_truncated else
                                 f"slide:31-{len(deck.slides)}" if len(deck.slides) > 30 else None)
    result["fully_validated"] = True
    result["recipes"] = list(RECIPES)
    result["recalculation"] = "not_performed"
    return result


def sheet_target(book, target, default="A1:T50"):
    from openpyxl.utils.cell import range_boundaries
    if target and "!" in target:
        title, area = target.rsplit("!", 1)
        title = title.strip("'").replace("''", "'")
    else:
        title, area = book.active.title, target or default
    if title not in book.sheetnames:
        raise OfficeError("TARGET_NOT_FOUND", "Sheet not found. Inspect the workbook for actual names.")
    try:
        bounds = range_boundaries(area)
        if any(x is None or x < 1 for x in bounds) or bounds[2] > 16384 or bounds[3] > 1048576:
            raise ValueError()
        if bounds[2] < bounds[0] or bounds[3] < bounds[1]:
            raise ValueError()
    except (ValueError, TypeError):
        raise OfficeError("INVALID_TARGET", "Use a bounded range such as Sheet1!A1:D20.") from None
    return book[title], bounds


def slice_target(target, maximum, prefix):
    if not target:
        return 0, min(maximum, 30)
    match = re.fullmatch(r"(?:(?:" + prefix + r"):?)?(\d+)(?:[-:](\d+))?", target, re.I)
    if not match:
        raise OfficeError("INVALID_TARGET", "Use " + prefix + ":1-10.")
    start, end = int(match[1]) - 1, int(match[2] or match[1])
    if start < 0 or end <= start or start >= maximum :
        raise OfficeError("INVALID_TARGET", "Requested part is absent.")
    return start, min(end, maximum)


def excel_fragment_page(sheet, bounds, continuation):
    """Return bounded row-major cell fragments without discarding any text."""
    from openpyxl.utils import get_column_letter
    left, top, right, bottom = bounds
    if continuation is None:
        row, column, offset = top, left, 0
    else:
        try:
            if continuation.get("kind") != "excel_cells":
                raise ValueError()
            row, column, offset = (continuation[k] for k in ("row", "column", "offset"))
            if (type(row) is not int or type(column) is not int or type(offset) is not int
                    or not top <= row <= bottom or not left <= column <= right or offset < 0):
                raise ValueError()
        except (AttributeError, KeyError, TypeError, ValueError):
            raise OfficeError("INVALID_CURSOR", "Invalid Excel continuation.") from None
    fragments = []
    budget = 0
    while row <= bottom:
        value = scalar(sheet.cell(row, column).value)
        if isinstance(value, str):
            if offset > len(value):
                raise OfficeError("INVALID_CURSOR", "Excel continuation exceeds the cell.")
            part = value[offset:offset + 512]
            complete = offset + len(part) >= len(value)
        else:
            if offset:
                raise OfficeError("INVALID_CURSOR", "Invalid scalar cell continuation.")
            part, complete = value, True
        fragment = {"cell": f"{get_column_letter(column)}{row}", "offset": offset,
                    "value": part, "complete": complete}
        size = len(json.dumps(fragment, ensure_ascii=False)) + 2
        if fragments and (budget + size > 10500 or len(fragments) >= 100):
            break
        fragments.append(fragment)
        budget += size
        if complete:
            offset = 0
            column += 1
            if column > right:
                column, row = left, row + 1
        else:
            offset += len(part)
    next_state = ({"kind": "excel_cells", "row": row, "column": column, "offset": offset}
                  if row <= bottom else None)
    return {"sheet": sheet.title, "representation": "cell_fragments", "cell_fragments": fragments,
            "truncated": next_state is not None, "text_truncated": any(not f["complete"] for f in fragments),
            "next_target": None, "_continuation": next_state}


def word_table_fragment_page(table, table_index, start, end, continuation):
    row, column, offset = start, 0, 0
    if continuation is not None:
        try:
            if continuation.get("kind") != "word_table" or continuation.get("table") != table_index:
                raise ValueError()
            row, column, offset = (continuation[k] for k in ("row", "column", "offset"))
            if (any(type(v) is not int for v in (row, column, offset))
                    or not start <= row < end or column < 0 or offset < 0):
                raise ValueError()
        except (AttributeError, KeyError, TypeError, ValueError):
            raise OfficeError("INVALID_CURSOR", "Invalid table continuation.") from None
    rows = table.rows
    fragments, budget = [], 0
    while row < end:
        cells = rows[row].cells
        if column >= len(cells):
            if offset or (not fragments and column > len(cells)):
                raise OfficeError("INVALID_CURSOR", "Invalid table cell continuation.")
            row, column = row + 1, 0
            continue
        text = cells[column].text
        if offset > len(text):
            raise OfficeError("INVALID_CURSOR", "Table continuation exceeds the cell.")
        part = text[offset:offset + 512]
        complete = offset + len(part) >= len(text)
        fragment = {"row": row + 1, "column": column + 1, "offset": offset,
                    "text": part, "complete": complete}
        size = len(json.dumps(fragment, ensure_ascii=False)) + 2
        if fragments and (budget + size > 10500 or len(fragments) >= 100):
            break
        fragments.append(fragment)
        budget += size
        if complete:
            column, offset = column + 1, 0
        else:
            offset += len(part)
    state = ({"kind": "word_table", "table": table_index, "row": row,
              "column": column, "offset": offset} if row < end else None)
    return {"table": table_index, "representation": "table_fragments", "table_fragments": fragments,
            "truncated": state is not None,
            "text_truncated": any(not f["complete"] for f in fragments),
            "next_target": None, "_continuation": state}


def text_fragment_page(kind, start, end, continuation, get_item, representation):
    """Page complete text items with stable character offsets."""
    index, offset = start, 0
    if continuation is not None:
        try:
            if continuation.get("kind") != kind:
                raise ValueError()
            index, offset = continuation["index"], continuation["offset"]
            if (type(index) is not int or type(offset) is not int
                    or not start <= index < end or offset < 0):
                raise ValueError()
        except (AttributeError, KeyError, TypeError, ValueError):
            raise OfficeError("INVALID_CURSOR", "Invalid text continuation.") from None
    fragments, budget = [], 0
    while index < end:
        text, extra = get_item(index)
        if offset > len(text):
            raise OfficeError("INVALID_CURSOR", "Text continuation exceeds the item.")
        part = text[offset:offset + 512]
        complete = offset + len(part) >= len(text)
        fragment = {"index": index + 1, "offset": offset, "text": part,
                    "complete": complete, **extra}
        size = len(json.dumps(fragment, ensure_ascii=False)) + 2
        if fragments and (budget + size > 10500 or len(fragments) >= 100):
            break
        fragments.append(fragment)
        budget += size
        if complete:
            index, offset = index + 1, 0
        else:
            offset += len(part)
    state = ({"kind": kind, "index": index, "offset": offset} if index < end else None)
    return {"representation": representation, representation: fragments,
            "truncated": state is not None,
            "text_truncated": any(not f["complete"] for f in fragments),
            "next_target": None, "_continuation": state}


def slide_parts(deck, start, end):
    for slide_index in range(start, end):
        slide_no = slide_index + 1
        yield {"slide": slide_no, "kind": "slide"}, ""
        for shape_index, shape in enumerate(deck.slides[slide_index].shapes, 1):
            base = {"slide": slide_no, "shape": shape_index}
            yield {**base, "kind": "shape_name", "type": str(shape.shape_type)}, shape.name
            if shape.has_text_frame:
                yield {**base, "kind": "shape_text"}, shape.text
            if shape.has_table:
                table = shape.table
                yield {**base, "kind": "table", "rows": len(table.rows), "columns": len(table.columns)}, ""
                for row, row_value in enumerate(table.rows, 1):
                    for column, cell in enumerate(row_value.cells, 1):
                        yield {**base, "kind": "table_cell", "row": row, "column": column}, cell.text
            if shape.has_chart:
                chart = shape.chart
                yield {**base, "kind": "chart", "type": str(chart.chart_type)}, ""
                for number, series in enumerate(chart.series, 1):
                    yield {**base, "kind": "chart_series", "series": number}, str(series.name or "")


def slide_fragment_page(deck, start, end, continuation):
    source_start, wanted, offset = start, 0, 0
    if continuation is not None:
        try:
            if continuation.get("kind") != "slide_parts":
                raise ValueError()
            source_start, wanted, offset = (continuation[k] for k in ("start", "part", "offset"))
            if (any(type(v) is not int for v in (source_start, wanted, offset))
                    or not start <= source_start < end or wanted < 0 or offset < 0):
                raise ValueError()
        except (AttributeError, KeyError, TypeError, ValueError):
            raise OfficeError("INVALID_CURSOR", "Invalid slide continuation.") from None
    fragments, budget, found = [], 0, False
    for number, (meta, text) in enumerate(slide_parts(deck, source_start, end)):
        if number < wanted:
            continue
        found = True
        current = offset if number == wanted else 0
        if current > len(text):
            raise OfficeError("INVALID_CURSOR", "Slide continuation exceeds the item.")
        while True:
            part = text[current:current + 512]
            complete = current + len(part) >= len(text)
            fragment = {**meta, "offset": current, "text": part, "complete": complete}
            size = len(json.dumps(fragment, ensure_ascii=False)) + 2
            if fragments and (budget + size > 10500 or len(fragments) >= 100):
                state = {"kind": "slide_parts", "start": source_start, "part": number, "offset": current}
                return {"representation": "slide_fragments", "slide_fragments": fragments,
                        "truncated": True, "text_truncated": any(not f["complete"] for f in fragments),
                        "next_target": None, "_continuation": state}
            fragments.append(fragment)
            budget += size
            if complete:
                break
            current += len(part)
    if continuation is not None and not found:
        raise OfficeError("INVALID_CURSOR", "Slide continuation is past the requested content.")
    return {"representation": "slide_fragments", "slide_fragments": fragments,
            "truncated": False, "text_truncated": False, "next_target": None, "_continuation": None}


def read(path, target="", view="summary", continuation=None):
    meta = describe(path)
    kind = meta["format"]
    if view in {"summary", "structure"} and not target:
        return {"structure": meta, "truncated": bool(meta.get("truncated")), "next_target": meta.get("next_target")}
    if kind == "excel":
        book = load_book(path, values=view == "values")
        try:
            sheet, (left, top, right, bottom) = sheet_target(book, target)
            if continuation is not None or right - left + 1 > 100:
                page = excel_fragment_page(sheet, (left, top, right, bottom), continuation)
                page["formula_values"] = "cached_only" if view == "values" else "formula_text"
                return page
            actual_bottom = min(bottom, top + 199, top + MAX_RESULT_CELLS // (right - left + 1) - 1)
            rows = [[scalar(c.value) for c in row] for row in sheet.iter_rows(
                min_row=top, max_row=actual_bottom, min_col=left, max_col=right)]
            while len(json.dumps(rows, ensure_ascii=False)) > 12000 and len(rows) > 1:
                rows.pop()
            actual_bottom = top + len(rows) - 1
            text_truncated = False
            if len(json.dumps(rows, ensure_ascii=False)) > 12000:
                page = excel_fragment_page(sheet, (left, top, right, bottom), None)
                page["formula_values"] = "cached_only" if view == "values" else "formula_text"
                return page
            from openpyxl.utils import get_column_letter
            next_target = (f"{sheet.title}!{get_column_letter(left)}{actual_bottom+1}:"
                           f"{get_column_letter(right)}{bottom}") if actual_bottom < bottom else None
            return {"sheet": sheet.title, "rows": rows, "row_count": len(rows),
                    "truncated": actual_bottom < bottom or text_truncated, "next_target": next_target,
                    "text_truncated": text_truncated,
                    "formula_values": "cached_only" if view == "values" else "formula_text"}
        finally:
            book.close()
    if kind == "word":
        from docx import Document
        doc = Document(path)
        table_list = re.fullmatch(r"table-list:(\d+)(?:-(\d+))?", target or "", re.I)
        if table_list:
            start = int(table_list[1]) - 1
            scope_end = min(int(table_list[2] or table_list[1]), len(doc.tables))
            if start < 0 or start >= scope_end:
                raise OfficeError("INVALID_TARGET", "Requested table list is absent.")
            end = min(scope_end, start + 30)
            return {"table_summaries": [{"index": i + 1, "rows": len(doc.tables[i].rows),
                                         "columns": len(doc.tables[i].columns)} for i in range(start, end)],
                    "total_tables": len(doc.tables), "truncated": end < scope_end,
                    "next_target": f"table-list:{end+1}-{scope_end}" if end < scope_end else None}
        table_match = re.fullmatch(r"table:(\d+)(?:!(\d+):(\d+))?", target or "", re.I)
        if table_match:
            index = int(table_match[1]) - 1
            if index < 0 or index >= len(doc.tables):
                raise OfficeError("TARGET_NOT_FOUND", "Table not found. Inspect the document for table indexes.")
            table = doc.tables[index]
            start = int(table_match[2] or 1) - 1
            scope_end = min(int(table_match[3]) if table_match[3] else len(table.rows), len(table.rows))
            if start < 0 or start >= scope_end:
                raise OfficeError("INVALID_TARGET", "Requested table rows are absent.")
            end = min(scope_end, start + 30)
            rows = [[c.text for c in row.cells] for row in table.rows[start:end]]
            if (continuation is not None or len(table.columns) > 50
                    or any(len(value) > 500 for row in rows for value in row)
                    or (rows and len(json.dumps(rows[0], ensure_ascii=False)) > 11000)):
                return word_table_fragment_page(table, index + 1, start, scope_end, continuation)
            while len(json.dumps(rows, ensure_ascii=False)) > 11000 and len(rows) > 1:
                rows.pop()
            end = start + len(rows)
            return {"table": index + 1, "start_row": start + 1, "rows": rows,
                    "text_truncated": False, "truncated": end < scope_end,
                    "next_target": f"table:{index+1}!{end+1}:{scope_end}" if end < scope_end else None}
        paragraphs = doc.paragraphs
        if target and not re.fullmatch(r"(?:paragraphs?:?)?\d+(?:[-:]\d+)?", target, re.I):
            matches = [i for i, p in enumerate(paragraphs) if p.text == target]
            if not matches:
                raise OfficeError("TARGET_NOT_FOUND", "Section heading not found.")
            start = matches[0]
            scope_end = next((i for i in range(start + 1, len(paragraphs))
                              if paragraphs[i].style.name.startswith("Heading")), len(paragraphs))
        elif target:
            start, scope_end = slice_target(target, len(paragraphs), "paragraphs?")
        else:
            start, scope_end = 0, len(paragraphs)
        end = min(scope_end, start + 30)
        if continuation is not None or any(len(p.text) > 2000 for p in paragraphs[start:end]):
            return text_fragment_page("word_paragraphs", start, scope_end, continuation,
                                      lambda i: (paragraphs[i].text, {"style": paragraphs[i].style.name}),
                                      "paragraph_fragments")
        while sum(len(p.text) + 100 for p in paragraphs[start:end]) > 11000 and end > start + 1:
            end -= 1
        return {"paragraphs": [{"index": i + 1, "text": p.text, "style": p.style.name}
                                for i, p in enumerate(paragraphs[start:end], start)],
                "text_truncated": False,
                "tables": [{"index":i+1,"rows":len(table.rows),"columns":len(table.columns)} for i,table in enumerate(doc.tables)][:30],
                "total_tables": len(doc.tables), "tables_truncated": len(doc.tables) > 30,
                "tables_next_target": f"table-list:31-{len(doc.tables)}" if len(doc.tables) > 30 else None,
                "truncated": end < scope_end,
                "next_target": f"paragraph:{end+1}-{scope_end}" if end < scope_end else None}
    if kind == "slides":
        from pptx import Presentation
        deck = Presentation(path)
        start, scope_end = slice_target(target, len(deck.slides), "slides?") if target else (0, len(deck.slides))
        if continuation is not None:
            return slide_fragment_page(deck, start, scope_end, continuation)
        end = min(scope_end, start + 10)
        items = []
        for i in range(start, end):
            shapes = []
            for index, shape in enumerate(deck.slides[i].shapes, 1):
                item = {"index": index, "name": shape.name, "type": str(shape.shape_type)}
                if shape.has_text_frame:
                    if len(shape.text) > 1000:
                        return slide_fragment_page(deck, start, scope_end, None)
                    item["text"] = shape.text
                if shape.has_table:
                    table = shape.table
                    values = [[c.text for c in row.cells] for row in table.rows]
                    if (len(table.rows) > 10 or len(table.columns) > 10
                            or any(len(value) > 100 for row in values for value in row)):
                        return slide_fragment_page(deck, start, scope_end, None)
                    item["table"] = {"rows": len(table.rows), "columns": len(table.columns), "values": values}
                if shape.has_chart:
                    series = [s.name for s in shape.chart.series]
                    if len(series) > 20:
                        return slide_fragment_page(deck, start, scope_end, None)
                    item["chart"] = {"type": str(shape.chart.chart_type), "series": series}
                shapes.append(item)
                if len(shapes) > 50:
                    return slide_fragment_page(deck, start, scope_end, None)
            slide = {"index": i + 1, "shapes": shapes, "shapes_truncated": False}
            if len(json.dumps(slide, ensure_ascii=False)) > 11000:
                return slide_fragment_page(deck, start, scope_end, None)
            if items and len(json.dumps(items + [slide], ensure_ascii=False)) > 11000:
                end = i
                break
            items.append(slide)
        return {"slides": items, "truncated": end < scope_end,
                "next_target": f"slide:{end+1}-{scope_end}" if end < scope_end else None}
    import pymupdf
    with pymupdf.open(path) as doc:
        start, scope_end = slice_target(target, doc.page_count, "pages?") if target else (0, doc.page_count)
        end = min(scope_end, start + 3)
        texts = {}
        def page_text(i):
            if i not in texts:
                texts[i] = doc[i].get_text()
            return texts[i]
        if continuation is not None or any(len(page_text(i)) > 6000 for i in range(start, end)):
            return text_fragment_page("pdf_pages", start, scope_end, continuation,
                                      lambda i: (page_text(i), {}), "page_fragments")
        while sum(len(page_text(i)) + 100 for i in range(start, end)) > 11000 and end > start + 1:
            end -= 1
        return {"text_truncated": False, "pages": [{"index": i + 1, "text": page_text(i)} for i in range(start, end)],
                "truncated": end < scope_end,
                "next_target": f"page:{end+1}-{scope_end}" if end < scope_end else None}


def rows_from(content):
    if isinstance(content, list):
        rows = content
    elif isinstance(content, str):
        lines = content.splitlines()
        rows = list(csv.reader(lines, delimiter="\t" if "\t" in content else ","))
        rows = [[number(v) for v in row] for row in rows]
    else:
        raise OfficeError("INVALID_CONTENT", "Supply CSV text or a JSON array of rows.")
    if not all(isinstance(row, list) for row in rows):
        raise OfficeError("INVALID_CONTENT", "Rows must be arrays.")
    cells = sum(len(row) for row in rows)
    if cells > MAX_CELLS:
        raise OfficeError("FILE_LIMIT", f"Input has {cells} cells; materialized_cells budget is {MAX_CELLS}. Increase the configured resource budget.")
    for row in rows:
        if any(isinstance(v, (dict, list)) for v in row):
            raise OfficeError("INVALID_CONTENT", "Cells must be scalar values.")
        if any(isinstance(v, float) and not math.isfinite(v) for v in row):
            raise OfficeError("INVALID_CONTENT", "Cells cannot contain NaN or infinity.")
        if any(isinstance(v, int) and abs(v) > 999999999999999 for v in row):
            raise OfficeError("PRECISION_LIMIT", "Excel numbers have 15 significant digits. Supply long identifiers as quoted text.")
    return rows


def style_book(book, style):
    from openpyxl.styles import Font, PatternFill, Alignment
    color = {"professional": "17365D", "academic": "333333", "clean": "286F6C"}[style]
    for sheet in book:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=color)
            cell.alignment = Alignment(wrap_text=True)
        from openpyxl.utils import get_column_letter
        for col in range(1, min(sheet.max_column, 100) + 1):
            length = max((len(str(sheet.cell(r, col).value or "")) for r in range(1, min(sheet.max_row, 200) + 1)), default=10)
            sheet.column_dimensions[get_column_letter(col)].width = min(50, max(12, length + 2))


def add_word_content(doc, content, job):
    if isinstance(content, dict):
        if content.get("title"):
            doc.add_heading(str(content["title"]), 0)
        sections = content.get("sections", [])
        if not isinstance(sections, list):
            raise OfficeError("INVALID_CONTENT", "sections must be an array.")
        for section in sections[:200]:
            if isinstance(section, str):
                doc.add_paragraph(section)
            elif isinstance(section, dict):
                if section.get("heading"):
                    doc.add_heading(str(section["heading"]), min(3, max(1, int(section.get("level", 1)))))
                if section.get("text"):
                    doc.add_paragraph(str(section["text"]))
                if section.get("table"):
                    rows = rows_from(section["table"])
                    if rows:
                        table = doc.add_table(rows=0, cols=max(map(len, rows)))
                        table.style = "Table Grid"
                        for row in rows:
                            cells = table.add_row().cells
                            for i, value in enumerate(row):
                                cells[i].text = str(value if value is not None else "")
                if section.get("image"):
                    from docx.shared import Inches
                    doc.add_picture(str(local(job, section["image"])), width=Inches(5.5))
        return
    lines = str(content or "").splitlines()
    index = 0
    def cells(line):
        return [s.strip().replace("\\|","|") for s in re.split(r"(?<!\\)\|",line.strip().strip("|"))]
    while index<len(lines):
        line = lines[index]
        if ("|" in line and index+1<len(lines) and
                all(re.fullmatch(r":?-{3,}:?",c) for c in cells(lines[index+1]))):
            rows = [cells(line)]
            index += 2
            while index<len(lines) and "|" in lines[index]:
                rows.append(cells(lines[index]))
                index += 1
            table = doc.add_table(rows=0,cols=max(map(len,rows)))
            table.style = "Table Grid"
            for row_number,row in enumerate(rows):
                row_cells = table.add_row().cells
                for column,text in enumerate(row):
                    row_cells[column].text = text
                    if row_number==0:
                        for run in row_cells[column].paragraphs[0].runs: run.bold=True
            continue
        match = re.match(r"^(#{1,4})\s+(.*)", line)
        if match:
            doc.add_heading(match[2], min(3, len(match[1]) - 1))
        elif line.startswith(("- ", "* ")):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif line:
            doc.add_paragraph(line)
        index += 1


def replace_runs(paragraph, old, new):
    if not old:
        raise OfficeError("INVALID_CONTENT", "Replacement text cannot be empty.")
    runs = paragraph.runs
    text = "".join(r.text for r in runs)
    positions = [m.start() for m in re.finditer(re.escape(old), text)]
    # Work right to left; unaffected runs retain their formatting.
    for start in reversed(positions):
        end = start + len(old)
        offset = 0
        first = True
        for run in runs:
            run_end = offset + len(run.text)
            if offset < end and run_end > start:
                left, right = max(0, start - offset), min(len(run.text), end - offset)
                run.text = run.text[:left] + (new if first else "") + run.text[right:]
                first = False
            offset = run_end
    return len(positions)


def add_slides(deck, content, job):
    from pptx.util import Inches
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    if isinstance(content, dict):
        slides = content.get("slides", [])
    else:
        slides = []
        current = None
        for line in str(content or "").splitlines():
            if line.startswith("#"):
                current = {"title": line.lstrip("# ").strip(), "text": []}
                slides.append(current)
            elif line.strip():
                if current is None:
                    current = {"title": "Overview", "text": []}
                    slides.append(current)
                current["text"].append(line.lstrip("-* ").strip())
    if not slides:
        raise OfficeError("INVALID_CONTENT", "Supply slides or a Markdown outline.")
    for item in slides:
        if not isinstance(item, dict):
            raise OfficeError("INVALID_CONTENT", "Each slide must be an object.")
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = str(item.get("title", ""))
        lines = item.get("text", [])
        if isinstance(lines, str):
            lines = lines.splitlines()
        boxes = regions(deck, [name for name in ("text","table","image","chart") if (lines if name=="text" else item.get(name))])
        body = slide.placeholders[1]
        if "text" in boxes:
            body.left,body.top,body.width,body.height = (Inches(v) for v in boxes["text"])
        frame = body.text_frame
        frame.clear()
        for i, text in enumerate(lines):
            p = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            p.text = str(text)
        if item.get("table"):
            rows = rows_from(item["table"])
            if rows:
                cols = max(map(len, rows))
                table = slide.shapes.add_table(len(rows), cols, *(Inches(v) for v in boxes["table"])).table
                for r, row in enumerate(rows):
                    for c, value in enumerate(row):
                        table.cell(r, c).text = str(value if value is not None else "")
        if item.get("image"):
            picture_fit(slide,local(job,item["image"]),boxes["image"])
        if item.get("chart"):
            chart = item["chart"]
            data = CategoryChartData()
            data.categories = chart.get("categories", [])
            for series in chart.get("series", []):
                data.add_series(str(series["name"]), series["values"])
            if not data.categories or not chart.get("series"):
                raise OfficeError("INVALID_CONTENT", "Chart requires categories and named series.")
            kinds = {"bar": XL_CHART_TYPE.COLUMN_CLUSTERED, "line": XL_CHART_TYPE.LINE, "pie": XL_CHART_TYPE.PIE}
            slide.shapes.add_chart(kinds.get(chart.get("type", "bar"), XL_CHART_TYPE.COLUMN_CLUSTERED),
                                   *(Inches(v) for v in boxes["chart"]), data)


def create(fmt, path, content, *, template=None, source=None, style="professional", job=None):
    if template:
        preflight(template, editing=not (content is None and source is None))
        if template.suffix.lower() != "." + fmt:
            raise OfficeError("FORMAT_MISMATCH", "Template format must match output format.")
    if source:
        exact_copy = content is None and not template and source.suffix.lower() == "." + fmt
        preflight(source, editing=not exact_copy)
    if template and content is None and source is None:
        path.write_bytes(read_bytes(template))
        return describe(path)
    if source and content is None and not template and source.suffix.lower() == "." + fmt:
        path.write_bytes(read_bytes(source))
        return describe(path)
    if fmt == "xlsx":
        import openpyxl
        book = load_book(template) if template else openpyxl.Workbook()
        if source and content is None:
            book = load_book(source)
        else:
            sheets = content.get("sheets", []) if isinstance(content, dict) else [{"name": "Sheet1", "rows": rows_from(content)}]
            if not sheets:
                raise OfficeError("INVALID_CONTENT", "Workbook content requires sheets with rows.")
            if not template:
                book.remove(book.active)
            for item in sheets:
                title = str(item.get("name", "Sheet1"))
                sheet = book[title] if title in book.sheetnames else book.create_sheet(title)
                for row in rows_from(item.get("rows", [])):
                    sheet.append(row)
                if item.get("chart"):
                    chart_book(book, title + "!" + sheet.dimensions, item["chart"])
            dimensions(book)
        style_book(book, style)
        book.save(path)
        book.close()
    elif fmt == "docx":
        from docx import Document
        from docx.shared import Pt
        doc = Document(template) if template else Document()
        if not template:
            word_preset(doc,style)
        if source and content is None:
            content = source_report(source)
        add_word_content(doc, content, job)
        doc.save(path)
    elif fmt == "pptx":
        from pptx import Presentation
        deck = Presentation(template) if template else Presentation()
        if source and content is None:
            content = {"slides": [{"title": "Summary", "text": json.dumps(source_report(source), ensure_ascii=False)[:3000]}]}
        initial_slides = len(deck.slides)
        add_slides(deck, content, job)
        slide_preset(deck, style, start=initial_slides)
        deck.save(path)
    else:
        raise OfficeError("FORMAT_UNSUPPORTED", "Create xlsx, docx or pptx; use office_convert for PDF.")
    if template:
        preserve_objects(template,path)
    return describe(path)


def chart_book(book, target, spec):
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    sheet, (left, top, right, bottom) = sheet_target(book, target)
    if right <= left or bottom <= top:
        raise OfficeError("INVALID_TARGET", "Chart range needs headers, categories and numeric values.")
    kind = spec.get("type", "bar")
    chart_type = {"bar": BarChart, "line": LineChart, "pie": PieChart}.get(kind)
    if chart_type is None:
        raise OfficeError("INVALID_CONTENT", "Chart type must be bar, line or pie.")
    chart = chart_type()
    chart.title = str(spec.get("title", "Chart"))
    chart.add_data(Reference(sheet, min_col=left + 1, max_col=right, min_row=top, max_row=bottom), titles_from_data=True)
    chart.set_categories(Reference(sheet, min_col=left, min_row=top + 1, max_row=bottom))
    # A new chart belongs below occupied data/previous charts by default;
    # putting it far to the right causes native PDF pagination to split it.
    default_anchor = "A" + str(max(sheet.max_row,bottom)+3+16*len(sheet._charts))
    anchor = str(spec.get("anchor", default_anchor))
    from openpyxl.utils.cell import coordinate_from_string
    coordinate_from_string(anchor)
    sheet.add_chart(chart, anchor)


def source_report(source):
    meta = describe(source)
    data = read(source, view="text", target="")
    if meta["format"] == "excel":
        data = read(source, view="formulas", target="A1:T30")
        return {"title": "Data report", "sections": [{"heading": "Data", "table": data["rows"]}]}
    return {"title": "Report", "sections": [{"text": json.dumps(data, ensure_ascii=False)[:12000]}]}


def preserve_objects(before, after):
    if before.suffix.lower() not in {".xlsx", ".docx", ".pptx"} or after.suffix.lower() != before.suffix.lower():
        return
    prefixes = ("xl/charts/", "xl/drawings/", "xl/media/", "word/media/", "ppt/media/", "ppt/charts/", "customXml/")
    with ZipFile(before) as a, ZipFile(after) as b:
        lost = set(a.namelist()) - set(b.namelist()) - {"xl/calcChain.xml", "xl/sharedStrings.xml"}
        if lost:
            raise OfficeError("FIDELITY_LOSS", "Editing would discard existing document parts; original retained.")
        old = {n for n in a.namelist() if n.startswith(prefixes)}
        new = set(b.namelist())
        if old - new:
            raise OfficeError("FIDELITY_LOSS", "Editing would remove existing chart/image parts; original retained.")
        for name in old:
            if ("/media/" in name or name.startswith("customXml/")) and a.read(name) != b.read(name):
                raise OfficeError("FIDELITY_LOSS", "Existing media bytes changed unexpectedly; original retained.")


def append_plain_word(source, output, text):
    """Append simple paragraphs while preserving all existing package parts."""
    import xml.parsers.expat as expat
    from xml.sax.saxutils import escape

    lines = text.splitlines()
    if any(line.startswith(("# ", "## ", "### ", "#### ", "- ", "* ")) for line in lines):
        return False
    for index, line in enumerate(lines[:-1]):
        if "|" in line:
            cells = [part.strip().replace("\\|", "|")
                     for part in re.split(r"(?<!\\)\|", lines[index + 1].strip().strip("|"))]
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                return False
    for line in lines:
        if any(not (char == "\t" or 0x20 <= ord(char) <= 0xD7FF
                    or 0xE000 <= ord(char) <= 0xFFFD
                    or 0x10000 <= ord(char) <= 0x10FFFF) for char in line):
            raise OfficeError("INVALID_CONTENT", "Text contains characters not supported by XML.")

    preflight(source)
    with ZipFile(source) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise OfficeError("INVALID_PACKAGE", "Duplicate package members cannot be safely edited.")
        if any(name.casefold().startswith("_xmlsignatures/")
               or name.casefold().endswith("vbaprojectsignature.bin") for name in names):
            raise OfficeError("SIGNATURE_UNSUPPORTED", "Editing would invalidate the existing digital signature; use a signature-aware route.")
        raw = archive.read("word/document.xml")
        if b"\x00" in raw[:128]:
            return False
        try:
            raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return False
        declaration = re.match(rb'^\s*<\?xml[^?]*encoding\s*=\s*["\x27]([^"\x27]+)', raw, re.I)
        if declaration and declaration.group(1).lower().replace(b"-", b"") != b"utf8":
            return False

        namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        body_name, section_name = namespace + "}body", namespace + "}sectPr"
        parser = expat.ParserCreate(namespace_separator="}")
        stack, body_count, body_end, section_start, section_ended, after_section = [], 0, None, None, False, False
        def start(name, attrs):
            nonlocal body_count, section_start, after_section
            parent = stack[-1] if stack else None
            if name == body_name:
                body_count += 1
            if parent == body_name:
                if section_ended:
                    after_section = True
                if name == section_name:
                    if section_start is not None:
                        raise OfficeError("INVALID_DOCUMENT", "Multiple final section properties cannot be safely edited.")
                    section_start = parser.CurrentByteIndex
            stack.append(name)
        def end(name):
            nonlocal body_end, section_ended
            if name == section_name and len(stack) > 1 and stack[-2] == body_name:
                section_ended = True
            if name == body_name:
                body_end = parser.CurrentByteIndex
            stack.pop()
        def forbidden(*args):
            raise OfficeError("UNSAFE_XML", "DTD/entity declarations are not supported.")
        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.StartDoctypeDeclHandler = forbidden
        parser.EntityDeclHandler = forbidden
        parser.ExternalEntityRefHandler = forbidden
        try:
            parser.Parse(raw, True)
        except expat.ExpatError as exc:
            raise OfficeError("INVALID_DOCUMENT", "Malformed Word document XML.") from exc
        if (body_count != 1 or body_end is None or raw[body_end:body_end + 2] != b"</"
                or after_section):
            raise OfficeError("FIDELITY_UNSUPPORTED", "Word body cannot be safely extended by this route.")
        position = section_start if section_start is not None else body_end
        pieces = []
        for line in lines:
            if not line:
                continue
            runs = []
            for index, fragment in enumerate(line.split("\t")):
                if index:
                    runs.append("<w:tab/>")
                runs.append('<w:t xml:space="preserve">' + escape(fragment) + "</w:t>")
            pieces.append('<w:p xmlns:w="' + namespace + '"><w:r>' + "".join(runs) + "</w:r></w:p>")
        inserted = "".join(pieces).encode("utf-8")
        updated = raw[:position] + inserted + raw[position:]
        with ZipFile(output, "w") as destination:
            destination.comment = archive.comment
            for item in infos:
                destination.writestr(item, updated if item.filename == "word/document.xml" else archive.read(item))
    return True


def edit(source, output, action, target, content, *, other=None, job=None, converter=None):
    if (source.suffix.lower() == ".docx" and action == "append_section"
            and isinstance(content, str) and append_plain_word(source, output, content)):
        return describe(output)
    meta = preflight(source, editing=True)
    kind = meta["format"]
    spec = content if isinstance(content, dict) else {}
    if action == "office_convert":
        return convert(source, output, str(spec.get("format", content)), converter=converter, job=job)
    if kind == "pdf":
        raise OfficeError("PDF_READ_ONLY", "PDF supports inspection, text extraction, rendering and conversion; general editing is unavailable.")
    if kind == "excel":
        book = load_book(source)
        dimensions(book)
        sheet, bounds = sheet_target(book, target)
        left, top, right, bottom = bounds
        if action in {"set_cells", "append_rows"} and content is None and other:
            other_book = load_book(other)
            dimensions(other_book)
            content = [list(row) for row in other_book.active.values]
            other_book.close()
        if action == "set_cells":
            rows = rows_from(content)
            for r, row in enumerate(rows, top):
                for c, value in enumerate(row, left):
                    sheet.cell(r, c, value)
        elif action == "set_formula":
            if not isinstance(content, str) or not content.startswith("=") or (left, top) != (right, bottom):
                raise OfficeError("INVALID_CONTENT", "set_formula needs one cell and formula text starting =.")
            sheet.cell(top, left, content)
        elif action == "append_rows":
            for row in rows_from(content):
                sheet.append(row)
        elif action in {"format_range", "apply_style"}:
            from openpyxl.styles import Font, PatternFill, Alignment
            for row in sheet.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right):
                for cell in row:
                    if "number_format" in spec:
                        cell.number_format = str(spec["number_format"])
                    if any(key in spec for key in ("bold","color","font","size")):
                        font = copy(cell.font)
                        if "bold" in spec: font.bold = bool(spec["bold"])
                        if "color" in spec: font.color = str(spec["color"])
                        if "font" in spec: font.name = str(spec["font"])
                        if "size" in spec: font.sz = float(spec["size"])
                        cell.font = font
                    if "fill" in spec:
                        cell.fill = PatternFill("solid", fgColor=str(spec["fill"]))
                    if "wrap" in spec:
                        cell.alignment = Alignment(wrap_text=bool(spec["wrap"]))
        elif action in {"add_chart", "excel_chart"}:
            chart_book(book, target, spec)
        elif action == "excel_clean":
            rows = list(sheet.iter_rows(values_only=True))
            cleaned, seen = [], set()
            for row in rows:
                row = tuple(v.strip() if isinstance(v, str) else v for v in row)
                if spec.get("drop_blank", True) and all(v in (None, "") for v in row):
                    continue
                key = json.dumps(row, default=str)
                if spec.get("deduplicate", True) and key in seen:
                    continue
                seen.add(key)
                cleaned.append(row)
            sheet.delete_rows(1, sheet.max_row)
            for row in cleaned:
                sheet.append(row)
        elif action == "excel_summary":
            rows = list(sheet.values)
            if not rows:
                raise OfficeError("INVALID_CONTENT", "Summary requires a header row.")
            header = list(rows[0])
            try:
                key_col, value_col = header.index(spec["group_by"]), header.index(spec["value"])
            except (KeyError, ValueError):
                raise OfficeError("INVALID_CONTENT", "Specify actual header names as group_by and value.") from None
            groups = defaultdict(list)
            for row in rows[1:]:
                if isinstance(row[value_col], (int, float)):
                    groups[str(row[key_col])].append(row[value_col])
            name = str(spec.get("sheet", "Summary"))
            if name in book.sheetnames:
                raise OfficeError("TARGET_EXISTS", "Choose a new summary sheet name.")
            summary = book.create_sheet(name)
            aggregation = spec.get("aggregation", "sum")
            if aggregation not in {"sum", "count", "average"}:
                raise OfficeError("INVALID_CONTENT", "aggregation must be sum, count or average.")
            summary.append([str(spec["group_by"]), aggregation + " " + str(spec["value"])])
            for key, values in sorted(groups.items()):
                summary.append([key, sum(values) if aggregation == "sum" else len(values) if aggregation == "count" else sum(values) / len(values)])
        elif action == "excel_reconcile":
            if other is None:
                raise OfficeError("CONTENT_REQUIRED", "Inspect the second workbook and supply source_id.")
            second = load_book(other)
            try:
                first_rows, second_rows = list(sheet.values), list(second.active.values)
                key_name = str(spec.get("key", ""))
                key1, key2 = list(first_rows[0]).index(key_name), list(second_rows[0]).index(key_name)
                a = {str(row[key1]): row for row in first_rows[1:]}
                b = {str(row[key2]): row for row in second_rows[1:]}
                if len(a) != len(first_rows)-1 or len(b) != len(second_rows)-1:
                    raise OfficeError("DUPLICATE_KEY", "Reconciliation keys must be unique.")
                name = str(spec.get("sheet", "Reconciliation"))
                if name in book.sheetnames:
                    raise OfficeError("TARGET_EXISTS", "Choose a new reconciliation sheet name.")
                out = book.create_sheet(name)
                out.append(["key", "status", "left", "right"])
                for key in sorted(a.keys() | b.keys()):
                    status = "only_right" if key not in a else "only_left" if key not in b else "equal" if a[key] == b[key] else "changed"
                    out.append([key, status, json.dumps(a.get(key), default=str, ensure_ascii=False),
                                json.dumps(b.get(key), default=str, ensure_ascii=False)])
            except (ValueError, IndexError):
                raise OfficeError("INVALID_CONTENT", "Both sources need the specified key header.") from None
            finally:
                second.close()
        else:
            raise OfficeError("ACTION_UNSUPPORTED", "Choose an Excel action or recipe.")
        dimensions(book)
        book.save(output)
        book.close()
    elif kind == "word":
        from docx import Document
        from docx.shared import Pt
        doc = Document(source)
        paragraphs = list(doc.paragraphs)
        for table in doc.tables:
            paragraphs.extend(p for row in table.rows for cell in row.cells for p in cell.paragraphs)
        if action == "replace_text":
            old, new = spec.get("find"), spec.get("replace")
            if not isinstance(old, str) or not isinstance(new, str):
                raise OfficeError("INVALID_CONTENT", 'Use {"find":"exact text","replace":"new text"}.')
            matched = sum(replace_runs(p, old, new) for p in paragraphs)
            if not matched:
                raise OfficeError("TARGET_NOT_FOUND", "Exact text was not found; no version was changed.")
        elif action in {"append_section", "word_report"}:
            add_word_content(doc, content if content is not None else source_report(other), job)
        elif action in {"apply_style", "word_format"}:
            preset = content if isinstance(content, str) else spec.get("preset")
            if preset:
                if preset not in PRESETS: raise OfficeError("INVALID_CONTENT","Choose clean, professional or academic.")
                word_preset(doc, preset)
            if spec.get("font"):
                doc.styles["Normal"].font.name = str(spec["font"])
            if spec.get("size"):
                size = float(spec["size"])
                if not 6 <= size <= 72:
                    raise OfficeError("INVALID_CONTENT", "Font size must be 6-72.")
                doc.styles["Normal"].font.size = Pt(size)
            if spec.get("line_spacing"):
                doc.styles["Normal"].paragraph_format.line_spacing = float(spec["line_spacing"])
            if spec.get("heading"):
                for p in paragraphs:
                    if p.text == target:
                        p.style = doc.styles["Heading " + str(min(3, max(1, int(spec["heading"]))))]
        elif action == "add_table":
            rows = rows_from(content)
            table = doc.add_table(rows=len(rows), cols=max(map(len,rows)))
            table.style = "Table Grid"
            set_table(table,rows)
        elif action == "set_table":
            match = re.fullmatch(r"table:(\d+)",target)
            if not match or not 1 <= int(match[1]) <= len(doc.tables):
                raise OfficeError("INVALID_TARGET","Use an existing table:1 index.")
            set_table(doc.tables[int(match[1])-1],rows_from(content))
        elif action == "add_image":
            from docx.shared import Inches
            doc.add_picture(str(local(job,spec["image"])),width=Inches(5.5))
        elif action == "set_text":
            match = re.fullmatch(r"paragraph:(\d+)",target)
            if not match or not 1 <= int(match[1]) <= len(doc.paragraphs):
                raise OfficeError("INVALID_TARGET","Use paragraph:1 with an existing index.")
            doc.paragraphs[int(match[1])-1].text = str(content)
        else:
            raise OfficeError("ACTION_UNSUPPORTED", "Choose a Word action or recipe.")
        doc.save(output)
    elif kind == "slides":
        from pptx import Presentation
        deck = Presentation(source)
        if action in {"add_slide", "slides_from_outline"}:
            initial_slides = len(deck.slides)
            add_slides(deck, content, job)
            slide_preset(deck, str(spec.get("style","professional")), start=initial_slides)
        elif action == "replace_text":
            old, new = spec.get("find"), spec.get("replace")
            if not isinstance(old, str) or not isinstance(new, str):
                raise OfficeError("INVALID_CONTENT", "Supply find and replace strings.")
            count = 0
            for slide in deck.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        count += sum(replace_runs(p, old, new) for p in shape.text_frame.paragraphs)
            if not count:
                raise OfficeError("TARGET_NOT_FOUND", "Text was not found.")
        elif action == "apply_style":
            preset = content if isinstance(content,str) else spec.get("preset")
            if preset:
                if preset not in PRESETS: raise OfficeError("INVALID_CONTENT","Choose clean, professional or academic.")
                slide_preset(deck,preset)
            from pptx.util import Pt
            for slide in deck.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for p in shape.text_frame.paragraphs:
                            for run in p.runs:
                                if spec.get("font"):
                                    run.font.name = str(spec["font"])
                                if spec.get("size"):
                                    run.font.size = Pt(float(spec["size"]))
        elif action in {"set_text","add_image","add_table","set_table","add_chart","update_chart"}:
            from pptx.util import Inches
            slide, shape = slide_and_shape(deck,target,shape_required=action in {"set_text","set_table","update_chart"})
            if action == "set_text":
                if not shape.has_text_frame: raise OfficeError("INVALID_TARGET","Shape has no text frame.")
                shape.text = str(content)
            elif action == "add_image":
                picture_fit(slide,local(job,spec["image"]),free_region(deck,slide,spec.get("position")))
            elif action == "add_table":
                rows = rows_from(content)
                table = slide.shapes.add_table(len(rows),max(map(len,rows)),*(Inches(v) for v in free_region(deck,slide))).table
                set_table(table,rows)
            elif action == "set_table":
                if not shape.has_table: raise OfficeError("INVALID_TARGET","Shape has no table.")
                set_table(shape.table,rows_from(content))
            elif action == "add_chart":
                add_native_chart(slide,spec,free_region(deck,slide,spec.get("position")))
            else:
                if not shape.has_chart: raise OfficeError("INVALID_TARGET","Shape has no chart.")
                from pptx.chart.data import CategoryChartData
                data = CategoryChartData()
                data.categories = spec["categories"]
                for series in spec["series"]: data.add_series(str(series["name"]),series["values"])
                shape.chart.replace_data(data)
        else:
            raise OfficeError("ACTION_UNSUPPORTED", "Choose a PowerPoint action or recipe.")
        deck.save(output)
    preserve_objects(source, output)
    return describe(output)


def convert(source, output, fmt, *, converter=None, job=None):
    preflight(source, editing=True)
    fmt = fmt.lower().lstrip(".")
    if fmt == "csv" and source.suffix.lower() in {".xlsx", ".csv"}:
        book = load_book(source)
        with output.open("w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows(book.active.values)
        book.close()
        return describe(output)
    if fmt == "docx" and source.suffix.lower() == ".pdf":
        import pymupdf
        from docx import Document
        doc = Document()
        with pymupdf.open(source) as pdf:
            for page in pdf:
                doc.add_paragraph(page.get_text())
        doc.save(output)
        meta = describe(output)
        meta["conversion_fidelity"] = "text_only"
        return meta
    if not converter or not Path(converter).is_file():
        raise OfficeError("CONVERTER_UNAVAILABLE", "Configure a qualified LibreOffice executable for Office PDF conversion/recalculation. Originals are unchanged.")
    if fmt not in {"pdf", "xlsx", "docx", "pptx", "html"}:
        raise OfficeError("FORMAT_UNSUPPORTED", "Converter supports pdf, xlsx, docx, pptx or html.")
    _check_converter_input(source)
    conversion_dir = job / "converted"
    conversion_dir.mkdir(exist_ok=True)
    profile = (job / "libreoffice-profile").as_uri()
    command = [str(converter), "-env:UserInstallation=" + profile, "--headless",
               "--nologo", "--nodefault", "--norestore", "--convert-to", fmt,
               "--outdir", str(conversion_dir), str(source)]
    try:
        outcome = subprocess.run(command, capture_output=True, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OfficeError("CONVERSION_TIMEOUT", "Conversion timed out; inspect the operation before retrying.") from exc
    converted = conversion_dir / (source.stem + "." + fmt)
    if outcome.returncode or not converted.is_file():
        raise OfficeError("CONVERSION_FAILED", "Configured converter did not produce the requested file.")
    output.write_bytes(read_bytes(converted))
    if fmt == "html":
        return {"format": "html", "fully_validated": True, "conversion_fidelity": "converter_dependent"}
    result = describe(output)
    result["conversion_fidelity"] = "converter_dependent"
    if fmt == "xlsx":
        result["recalculation"] = "converter_performed"
    return result


def preview(path, output, *, converter=None, job=None):
    if path.suffix.lower() != ".pdf":
        if not converter:
            return {"kind": "unavailable", "reason": "CONVERTER_UNAVAILABLE",
                    "visual": "not_checked"}
        convert(path, output, "pdf", converter=converter, job=job)
        pdf_path = output
    else:
        pdf_path = path
    import pymupdf
    image_path = output.with_suffix(".png")
    with pymupdf.open(pdf_path) as pdf:
        if not pdf.page_count:
            raise OfficeError("INVALID_PDF", "Preview has no pages.")
        page = pdf[0]
        page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).save(image_path)
        return {"kind": "pdf", "path": pdf_path.name, "image": image_path.name,
                "pages": pdf.page_count, "visual": "rendered_not_reviewed"}


def perform(operation, args, job, *, converter=None):
    source = local(job, args["input"]) if args.get("input") else None
    other = local(job, args["source"]) if args.get("source") else None
    template = local(job, args["template"]) if args.get("template") else None
    if operation == "inspect":
        return {"data": describe(source)}
    if operation == "read":
        return {"data": read(source, args.get("target", ""), args.get("view", "summary"), args.get("continuation"))}
    if operation == "create":
        output = local(job, args["output"])
        meta = create(args["format"], output, payload(args.get("content")), template=template,
                      source=other, style=args.get("style", "professional"), job=job)
    elif operation == "edit":
        output = local(job, args["output"])
        meta = edit(source, output, args["action"], args.get("target", ""), payload(args.get("content")),
                    other=other, job=job, converter=converter)
    elif operation == "finish":
        meta = describe(source)
        result = {"data": meta}
        if args.get("preview"):
            result["preview"] = preview(source, job / "preview.pdf", converter=converter, job=job)
        return result
    else:
        raise OfficeError("INVALID_OPERATION", "Unknown Office operation.")
    raw = read_bytes(output)
    return {"data": meta, "output": output.name, "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}
