from __future__ import annotations

import hashlib
import json

import pytest

from orchestrator.enterprise import (
    EnterpriseAuditLedger,
    IdentityService,
    ObjectStoreAuditAnchorSink,
    ObjectStoreObjectExists,
    create_audit_ledger_anchor,
)


class _MemoryObjectStore:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[dict] = []

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
    ) -> dict:
        self.put_calls.append(
            {
                "bucket": bucket,
                "key": key,
                "content_type": content_type,
                "metadata": metadata,
                "if_none_match": if_none_match,
                "object_lock_mode": object_lock_mode,
                "retain_until": retain_until,
            }
        )
        object_key = (bucket, key)
        if if_none_match == "*" and object_key in self.objects:
            raise ObjectStoreObjectExists(key)
        self.objects[object_key] = body
        return {"etag": hashlib.sha256(body).hexdigest()}

    def get_object(self, *, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]


def _anchor(tmp_path):
    db_path = tmp_path / "state" / "enterprise.sqlite"
    IdentityService.from_path(db_path).create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    ledger.append(event_type="policy", action="file.write", status="denied", context={"path": "a.txt"})
    return create_audit_ledger_anchor(ledger, label="daily/anchor")


def test_object_store_sink_writes_hash_named_anchor_with_lock_metadata(tmp_path):
    anchor = _anchor(tmp_path)
    client = _MemoryObjectStore()
    sink = ObjectStoreAuditAnchorSink(
        bucket="audit-bucket",
        prefix="prod/anchors",
        client=client,
        object_lock_mode="COMPLIANCE",
        retain_until="2030-01-01T00:00:00Z",
        scheme="s3",
    )

    receipt = sink.write_anchor(anchor)

    assert receipt.uri.startswith("s3://audit-bucket/prod/anchors/ORG-001/")
    assert receipt.uri.endswith(f"{anchor.anchor_hash}.json")
    assert receipt.existed is False
    assert sink.verify_receipt(receipt) is True
    call = client.put_calls[-1]
    assert call["content_type"] == "application/json"
    assert call["if_none_match"] == "*"
    assert call["object_lock_mode"] == "COMPLIANCE"
    assert call["retain_until"] == "2030-01-01T00:00:00Z"
    assert call["metadata"]["anchor_hash"] == anchor.anchor_hash
    assert call["metadata"]["content_sha256"] == receipt.content_sha256


def test_object_store_sink_is_idempotent_for_same_anchor(tmp_path):
    anchor = _anchor(tmp_path)
    client = _MemoryObjectStore()
    sink = ObjectStoreAuditAnchorSink(bucket="audit-bucket", client=client)

    first = sink.write_anchor(anchor)
    second = sink.write_anchor(anchor)

    assert first.uri == second.uri
    assert second.existed is True
    assert sink.verify_receipt(second) is True


def test_object_store_sink_rejects_existing_mismatched_anchor_content(tmp_path):
    anchor = _anchor(tmp_path)
    client = _MemoryObjectStore()
    sink = ObjectStoreAuditAnchorSink(bucket="audit-bucket", client=client)
    receipt = sink.write_anchor(anchor)
    _, key = sink._parse_uri(receipt.uri)
    client.objects[("audit-bucket", key)] = b"tampered\n"

    with pytest.raises(ValueError, match="does not match"):
        sink.write_anchor(anchor)
    assert sink.verify_receipt(receipt) is False


def test_object_store_sink_rejects_empty_bucket():
    with pytest.raises(ValueError, match="bucket is required"):
        ObjectStoreAuditAnchorSink(bucket="", client=_MemoryObjectStore())


def test_object_store_sink_accepts_anchor_dict(tmp_path):
    anchor = _anchor(tmp_path)
    client = _MemoryObjectStore()
    sink = ObjectStoreAuditAnchorSink(bucket="audit-bucket", client=client)

    receipt = sink.write_anchor(anchor.to_dict())
    stored = json.loads(next(iter(client.objects.values())).decode("utf-8"))

    assert stored["anchor_hash"] == anchor.anchor_hash
    assert sink.verify_receipt(receipt.to_dict()) is True


def test_object_store_sink_verifies_quoted_receipt_uri(tmp_path):
    anchor = _anchor(tmp_path)
    anchor = create_audit_ledger_anchor(
        EnterpriseAuditLedger.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001"),
        label="daily anchor/東京",
    )
    client = _MemoryObjectStore()
    sink = ObjectStoreAuditAnchorSink(bucket="audit-bucket", client=client, scheme="s3")

    receipt = sink.write_anchor(anchor)

    assert "%E6%9D%B1%E4%BA%AC" in receipt.uri
    assert sink.verify_receipt(receipt) is True
