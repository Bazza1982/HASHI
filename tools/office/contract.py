"""Small-model-friendly Office tool contracts. No runtime imports or file I/O.

This is the authoritative model-facing catalogue for HERV3 and CLI. Registry
availability requires an independently qualified Office Runtime; this module
never imports Office dependencies or acquires filesystem authority.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


def _string(description: str, length: int = 2048) -> dict[str, Any]:
    return {"type": "string", "maxLength": length, "description": description}


def _tool(
    name: str, description: str, properties: Mapping[str, dict],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": dict(properties),
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


OFFICE_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    _tool(
        "office_inspect",
        "FIRST for an Excel, Word, PowerPoint or PDF file. Identify content and return a file_id and suggested next step. Supply exactly one of path, source, attachment_id or file_id. An authorized working copy can be reopened from another Session using source=working_copy.",
        {
            "path": _string("Known authorized file path (Windows/WSL paths supported).", 4096),
            "source": {"type": "string", "enum": ["last_attachment", "list_attachments", "list_working_copies", "working_copy"],
                       "description": "List visible attachments or working copies of an authorized Session; reopen a selected working copy."},
            "source_session_id": _string("Session holding the working copies, for list_working_copies or working_copy.", 160),
            "source_file_id": _string("Office file ID in that Session, when source=working_copy.", 160),
            "source_revision": {"type": "integer", "minimum": 1,
                                "description": "Optional exact working-copy revision; without it, use the current committed version."},
            "cursor": _string("Continuation returned by a previous list operation.", 512),
            "attachment_id": _string("Explicit attachment ID from this Session's visible user messages.", 160),
            "file_id": _string("Existing Office file ID to inspect again.", 160),
        },
    ),
    _tool(
        "office_read",
        "Read an Excel range, Word section, or PowerPoint slide. Use file_id from office_inspect and follow next cursor to read complete large results.",
        {
            "file_id": _string("Office file ID returned by office_inspect.", 160),
            "target": _string("Range/location: Sales!A1:D20, section heading, paragraph:1-20, table:1!1:30, table-list:31-40, slide:2, page:1-3.", 512),
            "cursor": _string("Continuation returned by a previous office_read for the same file and version.", 160),
            "view": {"type": "string", "enum": ["summary", "values", "formulas", "text", "structure"],
                     "description": "Default summary; values for cells, text for documents."},
        },
        ("file_id",),
    ),
    _tool(
        "office_create",
        "Create a new XLSX, DOCX or PPTX file from simple content, a data reference or an existing template. Do not write Python scripts.",
        {
            "format": {"type": "string", "enum": ["xlsx", "docx", "pptx"],
                       "description": "Required output document format."},
            "output": _string("New authorized output filename including extension.", 4096),
            "content": _string("CSV or JSON rows for xlsx; Markdown headings, bullets and pipe tables for docx; Markdown outline for pptx. JSON sections/slides support native tables, images and charts. Use source_id for large data.", 16000),
            "source_id": _string("Existing authorized structured data reference.", 160),
            "template": _string("Existing authorized template reference.", 4096),
            "style": {"type": "string", "enum": ["clean", "professional", "academic"],
                      "description": "Optional built-in layout style."},
        },
        ("format", "output"),
    ),
    _tool(
        "office_edit",
        "Modify an inspected Office file. Supply file_id and one simple action. Work on a copy; do not overwrite the original by default.",
        {
            "file_id": _string("Office file ID returned by office_inspect.", 160),
            "action": {"type": "string", "enum": [
                "set_cells", "set_formula", "append_rows", "format_range", "add_chart",
                "replace_text", "append_section", "apply_style", "add_slide",
                "set_text", "add_image", "add_table", "set_table", "update_chart",
                "excel_summary", "excel_reconcile", "excel_clean", "excel_chart",
                "word_report", "word_format", "slides_from_outline", "office_convert",
            ], "description": "Choose one action. Recipes use small JSON content; office_convert content is pdf, csv, docx, xlsx or pptx."},
            "target": _string("Sheet/range, document text or section, or slide location.", 512),
            "content": _string("New text/formula or small JSON value representation.", 16000),
            "source_id": _string("Existing authorized data reference for large inserts.", 160),
        },
        ("file_id", "action"),
    ),
    _tool(
        "office_finish",
        "Save, reopen, validate and optionally preview/deliver the Office file. Report separately what was saved, recalculated, validated and delivered.",
        {
            "file_id": _string("Office file ID returned by a previous Office tool.", 160),
            "output": _string("Authorized final output path or filename.", 4096),
            "preview": {"type": "boolean", "description": "Generate supported preview when true."},
            "deliver": {"type": "boolean",
                        "description": "Deliver to the originating HASHI conversation when true."},
        },
        ("file_id",),
    ),
)

OFFICE_TOOL_NAMES = tuple(
    tool["function"]["name"] for tool in OFFICE_TOOL_SCHEMAS
)
OFFICE_READ_ONLY_TOOLS = frozenset({"office_inspect", "office_read"})


class OfficeInputError(ValueError):
    """Machine-readable argument error that a small model can correct."""

    def __init__(self, code: str, message: str, retry_example: dict | None = None):
        super().__init__(message)
        self.code = code
        self.retry_example = retry_example

    def as_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "error", "code": self.code, "message": str(self),
        }
        if self.retry_example is not None:
            result["retry_example"] = deepcopy(self.retry_example)
        return result


def office_tool_definitions() -> list[dict[str, Any]]:
    """Return independent copies; provider code may annotate the schemas."""
    return deepcopy(list(OFFICE_TOOL_SCHEMAS))


def validate_office_arguments(tool_name: str, arguments: Mapping[str, Any]) -> dict:
    """Shape-check tool input; this does NOT validate access or execute I/O.

    The trusted gateway must still authorize principal, Agent, Workzone,
    file ID and source bytes at execution time, including on repeated calls.
    """
    schema = next(
        (item["function"] for item in OFFICE_TOOL_SCHEMAS
         if item["function"]["name"] == tool_name),
        None,
    )
    if schema is None:
        raise OfficeInputError("UNKNOWN_OFFICE_TOOL", "Unknown Office tool.")
    if not isinstance(arguments, Mapping):
        raise OfficeInputError("INVALID_ARGUMENTS", "Office arguments must be a JSON object.")
    props = schema["parameters"]["properties"]
    unknown = set(arguments).difference(props)
    if unknown:
        raise OfficeInputError(
            "UNKNOWN_FIELD", "Unsupported field: " + ", ".join(sorted(map(str, unknown))),
        )
    for required in schema["parameters"]["required"]:
        if required not in arguments or arguments[required] in ("", None):
            raise OfficeInputError("MISSING_FIELD", "Required field: " + required)
    for key, value in arguments.items():
        spec = props[key]
        expected = spec["type"]
        if expected == "string":
            if not isinstance(value, str) or not value.strip():
                raise OfficeInputError("INVALID_FIELD", key + " must be a nonempty string.")
            if len(value) > spec.get("maxLength", 65536) or "\x00" in value:
                raise OfficeInputError("INVALID_FIELD", key + " is too long or contains NUL.")
            if "enum" in spec and value not in spec["enum"]:
                raise OfficeInputError("INVALID_FIELD", key + " has an unsupported value.")
        elif expected == "boolean" and type(value) is not bool:
            raise OfficeInputError("INVALID_FIELD", key + " must be true or false.")
        elif expected == "integer" and (type(value) is not int or value < spec.get("minimum", 0)):
            raise OfficeInputError("INVALID_FIELD", key + " must be a positive integer.")
    if tool_name == "office_inspect":
        sources = [key for key in ("path", "source", "attachment_id", "file_id") if key in arguments]
        if len(sources) != 1:
            raise OfficeInputError(
                "SOURCE_REQUIRED", "Supply exactly one of path, source, attachment_id or file_id.",
                {"tool": "office_inspect", "arguments": {"source": "last_attachment"}},
            )
    if tool_name == "office_inspect":
        reopen_fields = {"source_session_id", "source_file_id", "source_revision"}
        source = arguments.get("source")
        if source == "working_copy":
            if not {"source_session_id", "source_file_id"}.issubset(arguments):
                raise OfficeInputError("MISSING_FIELD", "A source Session and file ID are required to reopen a working copy.")
        elif source == "list_working_copies":
            if "source_session_id" not in arguments:
                raise OfficeInputError("MISSING_FIELD", "A source Session is required to list working copies.")
            if {"source_file_id", "source_revision"}.intersection(arguments):
                raise OfficeInputError("INVALID_FIELD", "Choose a file only when reopening a working copy.")
        elif reopen_fields.intersection(arguments):
            raise OfficeInputError("INVALID_FIELD", "Working-copy fields require a working-copy source.")
        if "cursor" in arguments and source not in {"list_attachments", "list_working_copies"}:
            raise OfficeInputError("INVALID_FIELD", "cursor is only valid for a list operation.")
    if tool_name == "office_create":
        if not any(key in arguments for key in ("content", "source_id", "template")):
            raise OfficeInputError(
                "CONTENT_REQUIRED", "Supply content, source_id or template."
            )
        extension = "." + str(arguments["format"]).casefold()
        if not str(arguments["output"]).casefold().endswith(extension):
            raise OfficeInputError("FORMAT_MISMATCH", "Output filename must end in " + extension)
    if tool_name == "office_edit" and not any(
        key in arguments for key in ("content", "source_id")
    ):
        raise OfficeInputError("CONTENT_REQUIRED", "Supply content or source_id for the edit.")
    return dict(arguments)
# HASHI-Fingerprint: hfp_51c7a8d0a19d4a20b1eba355755c55ed
