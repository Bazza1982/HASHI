from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, unquote

from orchestrator.enterprise.audit_anchor import AuditLedgerAnchor, load_audit_ledger_anchor
from orchestrator.enterprise.audit_worm import AuditAnchorReceipt


class ObjectStoreObjectExists(Exception):
    """Raised by object-store clients when an immutable object already exists."""


class ObjectStoreAuditClient(Protocol):
    def put_object(
        self,
        *,
        bucket: str,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        if_none_match: str = "*",
        object_lock_mode: str | None = None,
        retain_until: str | None = None,
    ) -> dict[str, Any]:
        ...

    def get_object(self, *, bucket: str, key: str) -> bytes:
        ...


@dataclass(frozen=True)
class ObjectStoreAuditAnchorSink:
    """Object-store WORM-style sink for audit anchor manifests.

    The sink is intentionally SDK-neutral. Production deployments can adapt it
    to S3 Object Lock, GCS retention policies, Azure immutable blob storage, or
    another object store. HASHI enforces no-overwrite semantics at the client
    boundary; hard immutability must be provided by the backing object store.
    """

    bucket: str
    client: ObjectStoreAuditClient
    prefix: str = "hashi/audit-anchors"
    object_lock_mode: str | None = None
    retain_until: str | None = None
    scheme: str = "object"

    def __post_init__(self) -> None:
        if not str(self.bucket or "").strip():
            raise ValueError("bucket is required")

    def write_anchor(self, anchor: AuditLedgerAnchor | dict[str, Any]) -> AuditAnchorReceipt:
        anchor = load_audit_ledger_anchor(anchor) if isinstance(anchor, dict) else anchor
        body = _anchor_body(anchor)
        content_hash = hashlib.sha256(body).hexdigest()
        key = self._anchor_key(anchor)
        metadata = {
            "anchor_hash": anchor.anchor_hash,
            "content_sha256": content_hash,
            "org_id": anchor.org_id,
            "chain_end_index": str(anchor.chain_end_index),
            "schema_version": str(anchor.schema_version),
        }
        try:
            self.client.put_object(
                bucket=self.bucket,
                key=key,
                body=body,
                content_type="application/json",
                metadata=metadata,
                if_none_match="*",
                object_lock_mode=self.object_lock_mode,
                retain_until=self.retain_until,
            )
            existed = False
            stored_body = body
        except ObjectStoreObjectExists:
            stored_body = self.client.get_object(bucket=self.bucket, key=key)
            stored_hash = hashlib.sha256(stored_body).hexdigest()
            if stored_hash != content_hash:
                raise ValueError("existing audit anchor object does not match receipt content")
            existed = True

        return AuditAnchorReceipt(
            anchor_hash=anchor.anchor_hash,
            uri=self._uri(key),
            bytes_written=len(stored_body),
            content_sha256=hashlib.sha256(stored_body).hexdigest(),
            existed=existed,
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
        bucket, key = self._parse_uri(receipt.uri)
        if bucket != self.bucket:
            return False
        try:
            body = self.client.get_object(bucket=bucket, key=key)
        except Exception:
            return False
        if len(body) != receipt.bytes_written:
            return False
        return hashlib.sha256(body).hexdigest() == receipt.content_sha256

    def _anchor_key(self, anchor: AuditLedgerAnchor) -> str:
        parts = [_safe_key_part(part) for part in [self.prefix, anchor.org_id] if str(part or "").strip()]
        label = _safe_key_part(anchor.label or "anchor")
        filename = f"{anchor.chain_end_index:020d}-{label}-{anchor.anchor_hash}.json"
        return "/".join([*parts, filename])

    def _uri(self, key: str) -> str:
        quoted_key = "/".join(quote(part, safe="") for part in key.split("/"))
        return f"{self.scheme}://{quote(self.bucket, safe='')}/{quoted_key}"

    def _parse_uri(self, uri: str) -> tuple[str, str]:
        prefix = f"{self.scheme}://"
        if not uri.startswith(prefix):
            raise ValueError("receipt URI scheme mismatch")
        rest = uri.removeprefix(prefix)
        bucket, sep, key = rest.partition("/")
        if not sep or not bucket or not key:
            raise ValueError("receipt URI is missing bucket or key")
        return unquote(bucket), "/".join(unquote(part) for part in key.split("/"))


def _anchor_body(anchor: AuditLedgerAnchor) -> bytes:
    return json.dumps(anchor.to_dict(), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def _safe_key_part(value: str) -> str:
    text = str(value or "").strip().strip("/")
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", ".", "/"} else "_" for ch in text)
    cleaned = "/".join(part for part in cleaned.split("/") if part not in {"", ".", ".."})
    return cleaned or "unknown"
