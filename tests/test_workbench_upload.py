import base64
import hashlib
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


def test_save_upload_persists_already_buffered_multipart_bytes(tmp_path):
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    runtime = SimpleNamespace(media_dir=tmp_path)
    payload = b"\x89PNG\r\n\x1a\nworkbench-upload"

    local_path, original_name = server._save_upload(
        runtime,
        filename="vision.png",
        payload=payload,
    )

    assert original_name == "vision.png"
    assert local_path.parent == tmp_path
    assert local_path.read_bytes() == payload


def test_tui_attachment_decoder_requires_exact_size_and_digest():
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    payload = b"\x89PNG\r\n\x1a\nactual-content"
    encoded = base64.b64encode(payload).decode("ascii")
    attachment = {
        "filename": "vision.png",
        "media_type": "image/png",
        "content_b64": encoded,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }

    assert server._decode_tui_attachment(attachment) == (
        payload,
        "vision.png",
        "image/png",
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        server._decode_tui_attachment({**attachment, "sha256": "0" * 64})


def test_workzone_attachment_cannot_escape_enabled_root(tmp_path):
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    root = tmp_path / "zone"
    root.mkdir()
    (root / "report.txt").write_text("inside", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    runtime = SimpleNamespace(
        _workzone_state={
            "revision": 1,
            "slots": [
                {"slot_id": "main", "path": str(root), "enabled": True, "available": True}
            ],
        }
    )

    payload, filename, media_type = server._resolve_workzone_attachment(
        runtime, "report.txt"
    )
    assert payload == b"inside"
    assert filename == "report.txt"
    assert media_type == "text/plain"
    with pytest.raises(ValueError, match="relative file reference"):
        server._resolve_workzone_attachment(runtime, "../outside.txt")
