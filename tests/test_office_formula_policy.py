"""Real workbook formula preservation and conversion-boundary tests."""
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

OFFICE_ROOT = Path(__file__).resolve().parents[1] / "tools" / "office"


def test_formula_text_survives_read_and_edit_without_external_evaluation(tmp_path):
    python = os.environ.get("HASHI_TEST_FORMULA_PYTHON")
    if not python:
        pytest.skip("set HASHI_TEST_FORMULA_PYTHON to a disposable Office Python")
    script = textwrap.dedent(r"""
        import socket, subprocess, sys
        from pathlib import Path
        from openpyxl import Workbook, load_workbook

        sys.path.insert(0, sys.argv[1])
        import engine
        root = Path(sys.argv[2])
        source = root / "source.xlsx"
        edited = root / "edited.xlsx"
        formulas = [
            "=SUM(Table1[Amount])",
            '=HYPERLINK("https://example.invalid","link")',
            '=IF(A2="a|b",1,0)',
            '=WEBSERVICE("https://example.invalid/data")',
        ]
        book = Workbook()
        for row, value in enumerate(formulas, 1):
            book.active.cell(row, 1, value)
        book.save(source)
        book.close()

        def no_network(*args, **kwargs):
            raise AssertionError("inert Office operations must not access the network")
        socket.socket.connect = no_network
        assert engine.describe(source)["format"] == "excel"
        assert [row[0] for row in engine.read(source, target="A1:A4", view="formulas")["rows"]] == formulas
        result = engine.edit(source, edited, "set_formula", "B1", '=IF(A2="a|b",1,0)', job=root)
        assert result["format"] == "excel"
        reopened = load_workbook(edited, data_only=False)
        assert [reopened.active.cell(row, 1).value for row in range(1, 5)] == formulas
        assert reopened.active["B1"].value == '=IF(A2="a|b",1,0)'
        reopened.close()

        # The Office runtime never evaluates formulas during read/edit.
        # An external converter must not be invoked for a workbook that may
        # perform unapproved external evaluation.
        converter = root / "synthetic-converter.exe"
        converter.write_bytes(b"fixture")
        called = []
        def trap(*args, **kwargs):
            called.append(args)
            raise AssertionError("unsafe converter must not be started")
        subprocess.run = trap
        try:
            engine.convert(edited, root / "output.pdf", "pdf", converter=converter, job=root)
        except engine.OfficeError as exc:
            assert exc.code == "EXTERNAL_FORMULA_EVALUATION"
        else:
            raise AssertionError("external evaluation should require a safe route")
        assert called == []
        assert engine._formula_conversion_risk('=IF(A2="WEBSERVICE(|",1,0)') is None
        assert engine._formula_conversion_risk("=SUM(")
        assert engine._formula_conversion_risk("=A1#")
        assert engine._formula_conversion_risk("=INDIRECT(A1)")
        assert engine._formula_conversion_risk("=cmd|'topic'!A0")

        # Unsupported/malformed text is still readable and cannot corrupt the
        # OOXML container; only converter evaluation remains unavailable.
        malformed = root / "malformed.xlsx"
        book = Workbook()
        book.active["A1"] = "=SUM("
        book.save(malformed)
        book.close()
        assert engine.read(malformed, target="A1", view="formulas")["rows"] == [["=SUM("]]
        try:
            engine.convert(malformed, root / "malformed.pdf", "pdf", converter=converter, job=root)
        except engine.OfficeError as exc:
            assert exc.code == "EXTERNAL_FORMULA_EVALUATION"
        else:
            raise AssertionError("unclassified conversion must not be started")
        assert called == []

        safe = root / "safe.xlsx"
        book = Workbook()
        for row, value in enumerate(formulas[:3], 1):
            book.active.cell(row, 1, value)
        book.save(safe)
        book.close()
        from zipfile import ZipFile
        with_connections = root / "with-connections.xlsx"
        with ZipFile(safe) as original, ZipFile(with_connections, "w") as archive:
            for name in original.namelist():
                archive.writestr(name, original.read(name))
            archive.writestr("xl/connections.xml", b"<connections/>")
        try:
            engine._check_converter_input(with_connections)
        except engine.OfficeError as exc:
            assert exc.code == "EXTERNAL_FORMULA_EVALUATION"
        else:
            raise AssertionError("converter must not refresh unapproved connections")
        from openpyxl.worksheet.formula import ArrayFormula
        array_file = root / "array.xlsx"
        book = Workbook()
        book.active["A1"] = ArrayFormula(ref="A1:A2", text='=WEBSERVICE("https://example.invalid")')
        book.save(array_file)
        book.close()
        try:
            engine._check_converter_input(array_file)
        except engine.OfficeError as exc:
            assert exc.code == "EXTERNAL_FORMULA_EVALUATION"
        else:
            raise AssertionError("unclassified array formulas must not be evaluated")
        from openpyxl.workbook.defined_name import DefinedName
        named_file = root / "defined-name.xlsx"
        book = Workbook()
        book.defined_names.add(DefinedName(name="fetch_data", attr_text='WEBSERVICE("https://example.invalid")'))
        book.save(named_file)
        book.close()
        try:
            engine._check_converter_input(named_file)
        except engine.OfficeError as exc:
            assert exc.code == "EXTERNAL_FORMULA_EVALUATION"
        else:
            raise AssertionError("external named expressions must not be evaluated")
        embedded = root / "embedded.pptx"
        with ZipFile(embedded, "w") as archive:
            archive.writestr("ppt/embeddings/chart.xlsx", edited.read_bytes())
        try:
            engine._check_converter_input(embedded)
        except engine.OfficeError as exc:
            assert exc.code == "EXTERNAL_FORMULA_EVALUATION"
        else:
            raise AssertionError("embedded chart formulas must use the same boundary")
        class ConversionReached(Exception):
            pass
        def safe_converter(*args, **kwargs):
            raise ConversionReached()
        subprocess.run = safe_converter
        try:
            engine.convert(safe, root / "safe.pdf", "pdf", converter=converter, job=root)
        except ConversionReached:
            pass
        else:
            raise AssertionError("ordinary formulas must reach the converter")
        print("formula preservation and conversion boundary passed")
    """)
    result = subprocess.run(
        [python, "-c", script, str(OFFICE_ROOT), str(tmp_path)],
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "formula preservation and conversion boundary passed"



def test_excel_long_cells_and_wide_ranges_have_lossless_pages(tmp_path):
    python = os.environ.get("HASHI_TEST_FORMULA_PYTHON")
    if not python:
        pytest.skip("set HASHI_TEST_FORMULA_PYTHON to a disposable Office Python")
    script = textwrap.dedent(r"""
        import json, sys
        from pathlib import Path
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
        sys.path.insert(0, sys.argv[1])
        import engine
        root = Path(sys.argv[2])
        source = root / "long.xlsx"
        book = Workbook()
        expected = {}
        for row in range(1, 3):
            for col in range(1, 107):
                value = ("first-\u03b1\n" * 1800 if (row, col) == (1, 1)
                         else "cell-%s-%s" % (row, col))
                book.active.cell(row, col, value)
                expected[f"{get_column_letter(col)}{row}"] = value
        book.save(source)
        book.close()
        target = "A1:DB2"
        state = None
        actual = {}
        for step in range(200):
            page = engine.read(source, target=target, view="formulas", continuation=state)
            assert len(json.dumps(page, ensure_ascii=False)) < 15000
            assert page["representation"] == "cell_fragments"
            for fragment in page["cell_fragments"]:
                key, offset, value = fragment["cell"], fragment["offset"], fragment["value"]
                previous = actual.get(key, "")
                assert offset == len(previous), (key, offset, len(previous))
                actual[key] = previous + value
            state = page.get("_continuation")
            if state is None:
                break
        else:
            raise AssertionError("continuation did not terminate")
        assert actual == expected
        print("wide range and long cell reconstructed exactly")
    """)
    result = subprocess.run(
        [python, "-c", script, str(OFFICE_ROOT), str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "wide range and long cell reconstructed exactly"
