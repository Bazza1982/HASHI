"""One rendering boundary for normal and crash-recovered fixed helper results."""
from __future__ import annotations

import base64

from orchestrator.permissions.windows_file_supervisor import _render_read
from orchestrator.permissions.network_worker import NETWORK_TOOLS


def render_helper_result(descriptor, arguments, result):
    data = base64.b64decode(result["data"], validate=True)
    if result.get("outcome") == "failed":
        if len(data) > 16384:
            raise ValueError("fixed helper error is oversized")
        return data.decode("utf-8", errors="strict")
    if descriptor.tool_name == "file_read":
        return _render_read(descriptor.scopes[0].resource, data, max(1, arguments.get("offset", 1)),
                            max(1, min(500, arguments.get("limit", 500))))
    if descriptor.tool_name == "file_write":
        return f"OK: wrote {len(arguments['content'])} characters to {descriptor.scopes[0].resource}"
    if descriptor.tool_name not in ({"file_list", "file_search", "apply_patch"} | NETWORK_TOOLS) or len(data) > 1024 * 1024:
        raise ValueError("invalid fixed helper result")
    return data.decode("utf-8", errors="strict")
