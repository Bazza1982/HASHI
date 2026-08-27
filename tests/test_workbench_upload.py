from types import SimpleNamespace

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
