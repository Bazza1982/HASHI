from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|cookie)", re.IGNORECASE)
KEY_LIKE_VALUE = re.compile(r"(?i)(?:sk|ds|or)-[A-Za-z0-9_-]{20,}")


class EvidenceCollector:
    def __init__(self, evidence_root: Path, *, forbidden_values: Iterable[str] = ()):
        self.root = Path(evidence_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.forbidden_values = tuple(str(value) for value in forbidden_values if str(value))

    def redact(self, value: Any, *, key: str = "") -> Any:
        if is_dataclass(value):
            value = asdict(value)
        if isinstance(value, dict):
            return {
                str(item_key): (
                    "<redacted>"
                    if SENSITIVE_KEY.search(str(item_key))
                    else self.redact(item_value, key=str(item_key))
                )
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [self.redact(item, key=key) for item in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            redacted = value
            for forbidden in self.forbidden_values:
                redacted = redacted.replace(forbidden, "<redacted-value>")
            redacted = KEY_LIKE_VALUE.sub("<redacted-key-like-value>", redacted)
            return redacted
        return value

    def write_json(self, name: str, payload: Any) -> Path:
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("evidence JSON name must be one simple .json filename")
        path = self.root / name
        temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.redact(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        return path

    def write_text(self, name: str, text: str) -> Path:
        if Path(name).name != name:
            raise ValueError("evidence text name must be one simple filename")
        path = self.root / name
        path.write_text(str(self.redact(text)), encoding="utf-8")
        return path

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def scan(self) -> dict[str, Any]:
        findings: list[dict[str, str]] = []
        for path in sorted(item for item in self.root.rglob("*") if item.is_file()):
            text = path.read_text(encoding="utf-8", errors="replace")
            for forbidden in self.forbidden_values:
                if forbidden and forbidden in text:
                    findings.append({"path": str(path), "kind": "forbidden_value"})
            if KEY_LIKE_VALUE.search(text):
                findings.append({"path": str(path), "kind": "key_like_value"})
        return {"ok": not findings, "finding_count": len(findings), "findings": findings}

    def finalize(self, *, verdict: str, checks: dict[str, Any]) -> Path:
        scan = self.scan()
        artifacts = []
        for path in sorted(item for item in self.root.rglob("*") if item.is_file() and item.name != "verdict.json"):
            artifacts.append(
                {
                    "path": str(path.relative_to(self.root)),
                    "size": path.stat().st_size,
                    "sha256": self.sha256(path),
                }
            )
        payload = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict if scan["ok"] else "INVALID_EVIDENCE",
            "checks": self.redact(checks),
            "redaction_scan": scan,
            "artifacts": artifacts,
        }
        return self.write_json("verdict.json", payload)
