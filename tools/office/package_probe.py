"""Bounded, read-only OOXML container preflight; not a full Office validator.

The caller must already have authorized the input bytes. This module never
opens host paths and never extracts archive members to disk.
"""
from __future__ import annotations

from io import BytesIO
import re
from typing import Any
import xml.parsers.expat as expat
from zipfile import BadZipFile, ZipFile


class OfficePackageError(ValueError):
    pass


class OfficePackageBudgetError(OfficePackageError):
    pass


_SUPPORTED_SUFFIXES = {
    ".xlsx": "excel", ".xlsm": "excel",
    ".docx": "word", ".docm": "word",
    ".pptx": "slides", ".pptm": "slides",
}
_SLIDE_ENTRY = re.compile(r"^ppt/slides/slide[1-9][0-9]*\.xml$")
_SHEET_TAG = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_EXPANDED_BYTES = 128 * 1024 * 1024
_MAX_ENTRIES = 2048


def inspect_ooxml_package(data: bytes, *, filename: str = "", budgets: dict | None = None) -> dict[str, Any]:
    """Classify XLSX/DOCX/PPTX bytes and report basic structure.

    Metadata-only preflight. Does not promise formula recalculation, safe
    macros, lossless editing, a valid full package, or rendering fidelity.
    """
    limits = {"file_bytes": _MAX_ARCHIVE_BYTES, "expanded_bytes": _MAX_EXPANDED_BYTES,
              "archive_entries": _MAX_ENTRIES}
    if budgets is not None:
        for key in limits:
            value = budgets.get(key, limits[key])
            if type(value) is not int or value <= 0:
                raise ValueError("Invalid Office resource budget: " + key)
            limits[key] = value
    if not isinstance(data, bytes) or not data:
        raise OfficePackageError("Expected nonempty OOXML bytes.")
    if len(data) > limits["file_bytes"]:
        raise OfficePackageBudgetError(f"Compressed Office input {len(data)} bytes exceeds file_bytes budget {limits['file_bytes']}; increase the configured resource budget.")
    if not data.startswith(b"PK"):
        raise OfficePackageError("File is not an unencrypted OOXML ZIP package.")
    try:
        with ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > limits["archive_entries"]:
                raise OfficePackageBudgetError(f"OOXML entries {len(infos)} exceed archive_entries budget {limits['archive_entries']}; increase the configured resource budget.")
            expanded = sum(info.file_size for info in infos)
            if expanded > limits["expanded_bytes"]:
                raise OfficePackageBudgetError(f"OOXML expanded-size {expanded} bytes exceeds expanded_bytes budget {limits['expanded_bytes']}; increase the configured resource budget.")
            names = set()
            for info in infos:
                name = info.filename.replace("\\", "/")
                parts = name.split("/")
                if name.startswith("/") or any(p == ".." for p in parts):
                    raise OfficePackageError("Invalid OOXML archive member path.")
                if info.flag_bits & 1:
                    raise OfficePackageError("Encrypted OOXML members are unsupported.")
                names.add(name)
            markers = {
                "excel": "xl/workbook.xml",
                "word": "word/document.xml",
                "slides": "ppt/presentation.xml",
            }
            found = [kind for kind, marker in markers.items() if marker in names]
            if len(found) != 1:
                raise OfficePackageError("Not a recognized single-format Office package.")
            kind = found[0]
            expected = None
            lower_name = str(filename).strip().casefold()
            for suffix, label in _SUPPORTED_SUFFIXES.items():
                if lower_name.endswith(suffix):
                    expected = label
                    break
            if expected is not None and expected != kind:
                raise OfficePackageError("Filename extension does not match Office contents.")
            result: dict[str, Any] = {
                "format": kind,
                "container_checked": True,
                "fully_validated": False,
                "macro_parts_present": any(
                    name.casefold().endswith("vbaproject.bin") for name in names
                ),
                "entry_count": len(infos),
            }
            if kind == "excel":
                workbook_info = archive.getinfo("xl/workbook.xml")
                sheets = []
                sheet_count = 0
                parser = expat.ParserCreate(namespace_separator="}")
                def element(name, attrs):
                    nonlocal sheet_count
                    if name == _SHEET_TAG[1:]:
                        sheet_count += 1
                        if len(sheets) < 1000:
                            sheets.append(str(attrs.get("name", "")))
                def forbidden(*args):
                    raise OfficePackageError("XML DTD and entity declarations are not accepted.")
                parser.StartElementHandler = element
                parser.StartDoctypeDeclHandler = forbidden
                parser.EntityDeclHandler = forbidden
                parser.ExternalEntityRefHandler = forbidden
                try:
                    with archive.open(workbook_info) as source:
                        while chunk := source.read(64 * 1024):
                            parser.Parse(chunk, False)
                        parser.Parse(b"", True)
                except expat.ExpatError as exc:
                    raise OfficePackageError("Malformed workbook metadata.") from exc
                result["sheets"] = sheets
                result["sheet_count"] = sheet_count
                result["sheets_truncated"] = sheet_count > len(sheets)
            elif kind == "slides":
                result["slide_parts"] = sum(
                    bool(_SLIDE_ENTRY.match(name)) for name in names
                )
            return result
    except BadZipFile as exc:
        raise OfficePackageError("Corrupt OOXML ZIP container.") from exc
