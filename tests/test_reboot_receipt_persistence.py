from __future__ import annotations

import json

import pytest

from orchestrator import reboot_receipts


def test_receipt_write_retries_transient_windows_replace_denial(
    tmp_path, monkeypatch
):
    path = tmp_path / "reboot-receipts.json"
    payload = {"schema": reboot_receipts.RECEIPT_SCHEMA_VERSION, "records": []}
    real_write_record = reboot_receipts.write_record
    attempts = []

    def persist(target, value):
        attempts.append(target)
        if len(attempts) < 3:
            raise PermissionError("replace temporarily denied")
        real_write_record(target, value)

    monkeypatch.setattr(reboot_receipts, "write_record", persist)
    monkeypatch.setattr(
        reboot_receipts, "_transient_windows_replace_denial", lambda _exc: True
    )
    monkeypatch.setattr(reboot_receipts, "_WINDOWS_REPLACE_RETRY_DELAYS", (0, 0))

    reboot_receipts._write_receipt_record(path, payload)

    assert len(attempts) == 3
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_receipt_write_does_not_retry_other_permission_error(
    tmp_path, monkeypatch
):
    path = tmp_path / "reboot-receipts.json"
    attempts = []

    def persist(_target, _value):
        attempts.append(True)
        raise PermissionError("access is permanently denied")

    monkeypatch.setattr(reboot_receipts, "write_record", persist)
    monkeypatch.setattr(
        reboot_receipts, "_transient_windows_replace_denial", lambda _exc: False
    )

    with pytest.raises(PermissionError, match="permanently denied"):
        reboot_receipts._write_receipt_record(path, {"schema": 5, "records": []})

    assert len(attempts) == 1
    assert not path.exists()
