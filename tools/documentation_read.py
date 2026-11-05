"""Read-only HASHI documentation capability for native and gateway Engines."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

from orchestrator.agent_documentation import documentation_root, read_documentation


async def execute_documentation_read(arguments: dict, *, global_config=None) -> str:
    def configured(name):
        return global_config.get(name) if isinstance(global_config, Mapping) else getattr(global_config, name, None)

    root = configured("documentation_root") or configured("project_root")
    if not root:
        return json.dumps({
            "ok": False, "error": "documentation_unavailable",
            "message": "This backend has no configured HASHI documentation source. Use an authorized repository reader or report the limitation.",
        })
    result = await asyncio.to_thread(read_documentation, arguments, code_root=documentation_root(Path(root)))
    return json.dumps(result, ensure_ascii=False)
# HASHI-Fingerprint: hfp_04df03f4487f4d86afcead7daa2c17ac
