"""Privacy-bounded media failure details for request diagnostics."""
from __future__ import annotations

import re
from typing import Any, Mapping

_TOKEN = re.compile(r"^[A-Za-z0-9_]{1,100}$")
_ATTACHMENT = re.compile(r"^att_[A-Za-z0-9_-]{1,100}$")


def sanitize_media_diagnostic(value: Any, *, attachment_id: Any = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, Any] = {}
    for key in ("phase", "reason", "error_type"):
        candidate = value.get(key)
        if isinstance(candidate, str) and _TOKEN.fullmatch(candidate):
            output[key] = candidate
    if "phase" not in output or "reason" not in output:
        return {}
    for key in ("errno", "winerror"):
        candidate = value.get(key)
        if type(candidate) is int and -(2**31) <= candidate < 2**31:
            output[key] = candidate
    identifier = attachment_id if attachment_id is not None else value.get("attachment_id")
    if isinstance(identifier, str) and _ATTACHMENT.fullmatch(identifier):
        output["attachment_id"] = identifier
    return output
