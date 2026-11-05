"""Dependency-free metadata for the scoped read-only documentation capability."""

DOCUMENTATION_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "documentation_read",
        "description": (
            "Read HASHI documentation through scoped Markdown links, sections and "
            "revision-bound continuation. Use outline for section discovery. "
            "Only approved documentation roots are readable; no writes or secret access."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["read", "outline"], "default": "read"},
                "path": {"type": "string", "description": "Repository-relative document or link from from_path.", "default": "docs/AGENT_FYI.md"},
                "from_path": {"type": "string", "description": "Source document advertising a relative link; omit for a repository-relative document id."},
                "reference": {"type": "string", "maxLength": 4010, "description": "Saved hashi-doc reference; use instead of path/from_path/section/revision to reread after compaction or handoff."},
                "section": {"type": "string", "description": "Heading or anchor; omit for the complete document."},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "max_chars": {"type": "integer", "minimum": 256, "maximum": 8000, "default": 4000},
                "revision": {"type": "string", "description": "Source SHA-256 from continuation; required after offset zero."},
            },
            "additionalProperties": False,
        },
    },
}
# HASHI-Fingerprint: hfp_33b5a7ae4d2c49b49b5576f96a74fc76
