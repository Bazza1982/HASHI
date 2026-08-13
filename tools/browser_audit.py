from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

_WRITE_LOCK = threading.Lock()
_IMAGE_DATA_PATTERN = re.compile(
    r"(data:image/[a-z0-9.+-]+;base64,)[A-Za-z0-9+/=]*",
    re.IGNORECASE,
)
_SCREENSHOT_DATA_PATTERN = re.compile(
    r"((?:^screenshot:|\[screenshot\]\s+base64:))[A-Za-z0-9+/=]*",
    re.IGNORECASE | re.MULTILINE,
)


def default_audit_path(base_dir: Path | None = None) -> Path:
    root = base_dir or (Path(__file__).resolve().parent.parent / "logs")
    return root / "browser_action_audit.jsonl"


def sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_value(v) for v in value]
    if isinstance(value, str):
        safe = _IMAGE_DATA_PATTERN.sub(r"\1[image-redacted]", value)
        safe = _SCREENSHOT_DATA_PATTERN.sub(r"\1[image-redacted]", safe)
        if len(safe) > 800:
            return safe[:800] + "...[truncated]"
        return safe
    return value


def append_audit_record(record: dict[str, Any], path: Path | None = None) -> Path:
    audit_path = path or default_audit_path()
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload.setdefault("ts", time.time())
    line = json.dumps(sanitize_value(payload), ensure_ascii=False)
    with _WRITE_LOCK:
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return audit_path
