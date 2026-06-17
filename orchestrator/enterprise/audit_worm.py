from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.enterprise.audit_anchor import AuditLedgerAnchor, load_audit_ledger_anchor


@dataclass(frozen=True)
class AuditAnchorReceipt:
    anchor_hash: str
    uri: str
    bytes_written: int
    content_sha256: str
    existed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_hash": self.anchor_hash,
            "uri": self.uri,
            "bytes_written": self.bytes_written,
            "content_sha256": self.content_sha256,
            "existed": self.existed,
        }


class FilesystemAuditAnchorSink:
    """Append-only filesystem sink for audit anchor manifests.

    This is a local WORM-style adapter: it never overwrites an existing anchor
    file, writes anchor content under an anchor-hash filename, and marks files
    read-only after creation. Hard WORM guarantees require a backing filesystem
    or object store that enforces immutability.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_anchor(self, anchor: AuditLedgerAnchor | dict[str, Any]) -> AuditAnchorReceipt:
        anchor = load_audit_ledger_anchor(anchor) if isinstance(anchor, dict) else anchor
        data = json.dumps(anchor.to_dict(), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        content_hash = hashlib.sha256(data).hexdigest()
        path = self._anchor_path(anchor)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError:
            existing = path.read_bytes()
            existing_hash = hashlib.sha256(existing).hexdigest()
            if existing_hash != content_hash:
                raise ValueError("existing audit anchor object does not match receipt content")
            return AuditAnchorReceipt(
                anchor_hash=anchor.anchor_hash,
                uri=path.as_uri(),
                bytes_written=len(existing),
                content_sha256=existing_hash,
                existed=True,
            )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o444)
        return AuditAnchorReceipt(
            anchor_hash=anchor.anchor_hash,
            uri=path.as_uri(),
            bytes_written=len(data),
            content_sha256=content_hash,
            existed=False,
        )

    def verify_receipt(self, receipt: AuditAnchorReceipt | dict[str, Any]) -> bool:
        if isinstance(receipt, dict):
            receipt = AuditAnchorReceipt(
                anchor_hash=str(receipt["anchor_hash"]),
                uri=str(receipt["uri"]),
                bytes_written=int(receipt["bytes_written"]),
                content_sha256=str(receipt["content_sha256"]),
                existed=bool(receipt.get("existed", False)),
            )
        path = Path(receipt.uri.removeprefix("file://"))
        if not path.exists() or path.stat().st_size != receipt.bytes_written:
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == receipt.content_sha256

    def _anchor_path(self, anchor: AuditLedgerAnchor) -> Path:
        org = _safe_path_part(anchor.org_id)
        label = _safe_path_part(anchor.label or "anchor")
        filename = f"{anchor.chain_end_index:020d}-{label}-{anchor.anchor_hash}.json"
        return self.root / org / filename


def _safe_path_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value or "").strip())
    return cleaned or "unknown"
