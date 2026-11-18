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
    with pytest.raises(ValueError, match="valid file reference"):
        server._resolve_workzone_attachment(runtime, "../outside.txt")



def test_tui_empty_document_is_valid_only_with_explicit_content_and_integrity():
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    attachment = {
        "filename": "empty.txt", "media_type": "text/plain",
        "content_b64": "", "size_bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(),
    }
    assert server._decode_tui_attachment(attachment) == (b"", "empty.txt", "text/plain")
    with pytest.raises(ValueError, match="encoding"):
        server._decode_tui_attachment({key: value for key, value in attachment.items() if key != "content_b64"})
    with pytest.raises(ValueError, match="digest mismatch"):
        server._decode_tui_attachment({**attachment, "sha256": "0" * 64})
    with pytest.raises(ValueError, match="size mismatch"):
        server._decode_tui_attachment({**attachment, "size_bytes": 1})



def test_workzone_absolute_root_and_larger_file_preserve_scope(tmp_path, monkeypatch):
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    root = tmp_path / "zone"
    root.mkdir()
    payload = b"x" * (26 * 1024 * 1024)
    inside = root / "large.txt"
    inside.write_bytes(payload)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"private")
    runtime = SimpleNamespace(_workzone_state={"revision": 1, "slots": [{
        "slot_id": "main", "path": str(root), "enabled": True, "available": True,
    }]})
    actual, filename, _ = server._resolve_workzone_attachment(runtime, str(inside))
    assert actual == payload and filename == "large.txt"
    with pytest.raises(ValueError, match="enabled Workzone"):
        server._resolve_workzone_attachment(runtime, str(outside))
    monkeypatch.setattr("orchestrator.workbench_api.MAX_SESSION_ATTACHMENT_BYTES", 4)
    with pytest.raises(ValueError, match="configured Session attachment limit"):
        server._resolve_workzone_attachment(runtime, str(inside))


def test_workzone_file_replacement_with_symlink_is_not_followed(tmp_path, monkeypatch):
    from orchestrator import session_attachment_authorization as binding
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    root = tmp_path / "zone"
    root.mkdir()
    inside = root / "report.txt"
    inside.write_bytes(b"allowed")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"private")
    probe = root / "probe"
    try:
        probe.symlink_to(outside)
        probe.unlink()
    except OSError:
        pytest.skip("creating a test symlink is unavailable")
    runtime = SimpleNamespace(_workzone_state={"revision": 1, "slots": [{
        "slot_id": "main", "path": str(root), "enabled": True, "available": True,
    }]})
    original = binding._open_no_symlinks
    def race(path, flags):
        inside.rename(root / "old.txt")
        inside.symlink_to(outside)
        return original(path, flags)
    monkeypatch.setattr(binding, "_open_no_symlinks", race)
    with pytest.raises(ValueError, match="read safely"):
        server._resolve_workzone_attachment(runtime, str(inside))
