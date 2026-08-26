import json
import subprocess

import pytest

from tools.session_release_gate import qualification_receipt, verify_package


def test_qualification_receipt_is_generated_from_complete_capture(tmp_path):
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            {
                "hashi_revision": "a" * 40,
                "client_revision": "b" * 40,
                "client_profile": "qualification",
                "deployment_lock_sha256": "c" * 64,
                "compatibility_record": "session-v1-candidate",
                "session_id": "session_1",
                "run_id": "run_1",
                "request_id": "request_1",
                "event_consumer_id": "consumer_1",
                "acknowledged_sequence": 25,
                "provider_envelope_sha256": "d" * 64,
                "history_messages": 24,
                "required_history_messages": 20,
                "current_request_occurrences": 1,
                "cross_session_sentinel_occurrences": 0,
                "terminal_state": "completed",
            }
        ),
        encoding="utf-8",
    )
    receipt = qualification_receipt(capture, tmp_path / "receipt.json")
    assert receipt["result"] == "passed"


def test_qualification_refuses_missing_external_identity(tmp_path):
    capture = tmp_path / "capture.json"
    capture.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="client_revision"):
        qualification_receipt(capture, tmp_path / "receipt.json")


def test_release_package_verifier_rejects_unexpected_members(tmp_path):
    package = tmp_path / "bad.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(package), "--files-from", "/dev/null"], check=True
    )
    with pytest.raises(RuntimeError, match="members"):
        verify_package(package)
