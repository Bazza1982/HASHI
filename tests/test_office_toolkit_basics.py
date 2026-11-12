"""Focused Office contract and package-preflight tests (no external deps).

Defects guarded: mismatched Office formats, unbounded/untrusted arguments,
wrong small-model tier hints, and malformed/oversized OOXML containers.
"""
from io import BytesIO
from zipfile import ZipFile

import pytest

from tools.office import (
    OFFICE_TOOL_NAMES,
    OfficeInputError,
    office_task_hint,
    office_tool_definitions,
    validate_office_arguments,
)
from tools.office.package_probe import OfficePackageError, inspect_ooxml_package


def test_compact_tools_have_independent_schemas_and_no_runtime_permission():
    names = [schema["function"]["name"] for schema in office_tool_definitions()]
    assert tuple(names) == OFFICE_TOOL_NAMES
    assert names == [
        "office_inspect", "office_read", "office_create",
        "office_edit", "office_finish",
    ]
    definitions = office_tool_definitions()
    definitions[0]["function"]["description"] = "mutated by a provider"
    assert office_tool_definitions()[0]["function"]["description"] != "mutated by a provider"
    assert all(
        item["function"]["parameters"]["additionalProperties"] is False
        for item in office_tool_definitions()
    )


def test_source_and_outcome_arguments_are_bounded_and_correctable():
    assert validate_office_arguments(
        "office_inspect", {"source": "last_attachment"}
    ) == {"source": "last_attachment"}
    assert validate_office_arguments(
        "office_inspect", {"path": r"D:\Finance Files\2026 FY.xlsx"}
    )["path"] == r"D:\Finance Files\2026 FY.xlsx"
    with pytest.raises(OfficeInputError) as exc:
        validate_office_arguments("office_inspect", {})
    assert exc.value.as_result()["retry_example"]["tool"] == "office_inspect"
    with pytest.raises(OfficeInputError) as exc:
        validate_office_arguments("office_inspect", {"path": "a", "source": "last_attachment"})
    assert exc.value.code == "SOURCE_REQUIRED"
    with pytest.raises(OfficeInputError) as exc:
        validate_office_arguments("office_read", {"file_id": "abc", "unexpected": True})
    assert exc.value.code == "UNKNOWN_FIELD"
    with pytest.raises(OfficeInputError):
        validate_office_arguments("office_finish", {"file_id": "abc", "deliver": "true"})


def test_create_requires_real_content_and_correct_extension():
    assert validate_office_arguments(
        "office_create", {"format": "docx", "output": "report.docx", "content": "Hello"}
    )["format"] == "docx"
    with pytest.raises(OfficeInputError) as exc:
        validate_office_arguments(
            "office_create", {"format": "docx", "output": "report.xlsx", "content": "Hello"}
        )
    assert exc.value.code == "FORMAT_MISMATCH"
    with pytest.raises(OfficeInputError) as exc:
        validate_office_arguments("office_create", {"format": "xlsx", "output": "data.xlsx"})
    assert exc.value.code == "CONTENT_REQUIRED"


def test_office_hint_uses_attachment_or_explicit_office_intent():
    hint = office_task_hint("Summarise the uploaded report", ["Q3 Numbers.xlsx"])
    assert hint["activate"] is True
    assert hint["tier"] == "office_basic"
    assert hint["formats"] == ["excel"]
    assert office_task_hint("Summarise a financial trend")["activate"] is False
    assert office_task_hint("Count every word in this sentence")["activate"] is False
    assert office_task_hint("Create a Word document")["formats"] == ["word"]
    assert office_task_hint("制作一个PPT文件")["formats"] == ["slides"]


def _ooxml(**members):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for path, body in members.items():
            archive.writestr(path, body)
    return buffer.getvalue()


def test_ooxml_probe_inspects_excel_without_importing_office_libraries():
    package = _ooxml(**{
        "xl/workbook.xml": (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Sales"/><sheet name="Summary"/></sheets></workbook>'
        )
    })
    result = inspect_ooxml_package(package, filename="FY26.xlsx")
    assert result["format"] == "excel"
    assert result["sheets"] == ["Sales", "Summary"]
    assert result["fully_validated"] is False
    with pytest.raises(OfficePackageError, match="does not match"):
        inspect_ooxml_package(package, filename="FY26.docx")


def test_ooxml_probe_fails_closed_for_bombs_and_corrupt_bytes():
    with pytest.raises(OfficePackageError, match="not an unencrypted"):
        inspect_ooxml_package(b"not a zip")
    huge = _ooxml(**{"xl/workbook.xml": b"A" * (2 * 1024 * 1024 + 1)})
    with pytest.raises(OfficePackageError, match="Workbook metadata exceeds"):
        inspect_ooxml_package(huge)
    forbidden = _ooxml(**{"../xl/workbook.xml": "malicious"})
    with pytest.raises(OfficePackageError, match="Invalid"):
        inspect_ooxml_package(forbidden)
