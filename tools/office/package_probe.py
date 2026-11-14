"""Bounded, read-only OOXML container preflight; not a full Office validator.

The caller must already have authorized the input bytes. This module never
opens host paths and never extracts archive members to disk.
"""
from __future__ import annotations

from io import BytesIO
import re
from typing import Any
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


class OfficePackageError(ValueError):
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
_MAX_WORKBOOK_XML = 2 * 1024 * 1024


def inspect_ooxml_package(data: bytes, *, filename: str = "") -> dict[str, Any]:
    """Classify XLSX/DOCX/PPTX bytes and report basic structure.

    Metadata-only preflight. Does not promise formula recalculation, safe
    macros, lossless editing, a valid full package, or rendering fidelity.
    """
    if not isinstance(data, bytes) or not data:
        raise OfficePackageError("Expected nonempty OOXML bytes.")
    if len(data) > _MAX_ARCHIVE_BYTES:
        raise OfficePackageError("Compressed Office file exceeds preflight limit.")
    if not data.startswith(b"PK"):
        raise OfficePackageError("File is not an unencrypted OOXML ZIP package.")
    try:
        with ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ENTRIES:
                raise OfficePackageError("OOXML archive has too many entries.")
            if sum(info.file_size for info in infos) > _MAX_EXPANDED_BYTES:
                raise OfficePackageError("OOXML archive exceeds expanded-size limit.")
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
                if workbook_info.file_size > _MAX_WORKBOOK_XML:
                    raise OfficePackageError("Workbook metadata exceeds preflight limit.")
                raw_xml = archive.read("xl/workbook.xml")
                if b"<!DOCTYPE" in raw_xml.upper():
                    raise OfficePackageError("XML DOCTYPE is not accepted.")
                try:
                    root = ET.fromstring(raw_xml)
                except ET.ParseError as exc:
                    raise OfficePackageError("Malformed workbook metadata.") from exc
                result["sheets"] = [
                    str(sheet.attrib.get("name", ""))
                    for sheet in root.iter(_SHEET_TAG)
                ][:1000]
            elif kind == "slides":
                result["slide_parts"] = sum(
                    bool(_SLIDE_ENTRY.match(name)) for name in names
                )
            return result
    except BadZipFile as exc:
        raise OfficePackageError("Corrupt OOXML ZIP container.") from exc
