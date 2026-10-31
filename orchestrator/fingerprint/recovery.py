"""Rebuild evidence projections after a crash; never replay business effects."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .execution import observe, reconcile
from .manifest import project
from .models import relative_target
from .service import FingerprintAudit


def repair(audit: FingerprintAudit, root: Path, *, authorization: dict) -> list[dict]:
    root = root.resolve()
    reconcile(audit, root, authorization=authorization)
    events = audit.store.read_events(authorization)
    root_id = "hfr_" + hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:32]
    latest = {}
    for event in events:
        receipt = event.get("payload", {})
        if event["event_type"] not in {"fingerprint_receipt", "fingerprint_reconciled"} or receipt.get("root_id") != root_id:
            continue
        latest[relative_target(receipt["relative_path"])] = {**receipt, "event_id": event["event_id"]}
    results = []
    for relative, receipt in latest.items():
        path = root / relative
        if not path.resolve().is_relative_to(root):
            results.append({"relative_path": relative, "status": "outside_root"})
            continue
        observation = observe(path)
        after = receipt.get("after", {})
        if observation.version.state != after.get("state") or observation.version.sha256 != after.get("sha256"):
            results.append({"relative_path": relative, "status": "version_mismatch"})
            continue
        try:
            projected = {**receipt, "marker_status": "manifest" if observation.version.state == "present" else "deleted"}
            project(root, projected, file_id=observation.file_id)
            audit.append("fingerprint_projection", {
                "operation_id": receipt["operation_id"], "receipt_event_id": receipt["event_id"],
                "marker_status": projected["marker_status"], "projection_status": "projected", "recovered": True,
            }, request_id=receipt["identity"]["request_id"])
            results.append({"relative_path": relative, "status": "projected"})
        except (OSError, ValueError):
            results.append({"relative_path": relative, "status": "pending"})
    return results
