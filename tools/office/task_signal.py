"""Deterministic Office-task hint for small-model tool-tier selection.

A hint changes only the tool *presentation*; it never enables a permission,
grants host paths, selects a Python interpreter, or invokes the model.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any

_EXTENSION_FORMAT = {
    ".xlsx": "excel", ".xlsm": "excel", ".csv": "excel",
    ".docx": "word", ".docm": "word",
    ".pptx": "slides", ".pptm": "slides",
    ".pdf": "pdf",
}
_PATTERN_FORMATS = (
    ("excel", re.compile(r"\b(?:excel|xlsx|xlsm|csv|spreadsheet|workbook)\b|电子表格|工作簿|Excel表格", re.I)),
    ("word", re.compile(r"\b(?:docx|docm|word document|microsoft word)\b|Word文档|文档排版", re.I)),
    ("slides", re.compile(r"\b(?:pptx|pptm|powerpoint|slide deck)\b|幻灯片|演示文稿|PPT文件", re.I)),
    ("pdf", re.compile(r"\bpdf\b|PDF文件", re.I)),
)


def detect_office_format(filename: str) -> str | None:
    """Identify an Office-related filename, including Windows paths."""
    if not isinstance(filename, str):
        return None
    clean = filename.strip().casefold().replace("\\", "/")
    for suffix, kind in _EXTENSION_FORMAT.items():
        if clean.endswith(suffix):
            return kind
    return None


def office_task_hint(
    user_text: str, attachments: Iterable[str | Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Provide a conservative, no-LLM tier hint for HERV3/CLI adapters.

    Attachment hints are based on trusted attachment metadata, not content.
    The actual Office gateway MUST still inspect bytes and enforce authority.
    """
    formats: set[str] = set()
    source = []
    for item in attachments:
        if isinstance(item, str):
            name = item
        elif isinstance(item, Mapping):
            name = str(item.get("filename") or item.get("name") or "")
        else:
            continue
        kind = detect_office_format(name)
        if kind:
            formats.add(kind)
            if "attachment" not in source:
                source.append("attachment")
    request = str(user_text or "")
    for kind, pattern in _PATTERN_FORMATS:
        if pattern.search(request):
            formats.add(kind)
            if "request" not in source:
                source.append("request")
    return {
        "activate": bool(formats),
        "tier": "office_basic" if formats else None,
        "formats": sorted(formats),
        "source": source,
    }
