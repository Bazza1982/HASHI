"""Third-party checks, minimal export and actual CLI subprocess."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from orchestrator.fingerprint.export import export_bundle
from orchestrator.fingerprint.verify import inspect, verify
from tests.test_fingerprint_execution import registry_at


@pytest.mark.asyncio
async def test_verifier_detects_external_edits_broken_links_and_unsigned_scope(tmp_path):
    registry, store, work = registry_at(tmp_path)
    await registry.execute("file_write", {"path": "data.json", "content": "{}"}, tool_call_id="call")
    result = verify(work)
    assert result["status"] == "consistent" and result["authentication"] == "unsigned"
    assert result["coverage_complete"] is False
    (work / "data.json").write_text("external")
    assert inspect(work / "data.json")["version"] == "mismatch"
    manifest_path = work / ".hashi-fingerprint/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["receipts"].clear()
    manifest_path.write_text(json.dumps(manifest))
    assert verify(work)["files"][0]["evidence_link"] == "corrupt"


@pytest.mark.asyncio
async def test_export_omits_content_secrets_raw_arguments_and_local_paths_by_default(tmp_path):
    registry, store, work = registry_at(tmp_path)
    secret = "SECRET_MUST_NOT_BE_EXPORTED"
    await registry.execute("file_write", {"path": "settings.json", "content": secret}, tool_call_id="call")
    output = tmp_path / "evidence.zip"
    report = export_bundle(work, output)
    assert report["content_included"] is False
    with zipfile.ZipFile(output) as archive:
        data = b"\n".join(archive.read(name) for name in archive.namelist())
        assert secret.encode() not in data
        assert str(tmp_path).encode() not in data
        assert b'"arguments"' not in data and b'"file_token"' not in data
        assert "settings.json" not in archive.namelist()
    with pytest.raises(FileExistsError):
        export_bundle(work, output)


@pytest.mark.asyncio
async def test_selected_file_export_can_be_verified_without_hashi_private_audit(tmp_path):
    registry, store, work = registry_at(tmp_path)
    await registry.execute("file_write", {"path": "data.json", "content": "{}"}, tool_call_id="call")
    output = tmp_path / "selected.zip"
    export_bundle(work, output, relative="data.json", include_content=True)
    delivered = tmp_path / "delivered"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(delivered)
    assert verify(delivered)["status"] == "consistent"
    # An isolated file plus companion can also be checked without its project manifest.
    (delivered / ".hashi-fingerprint/manifest.json").unlink()
    assert inspect(delivered / "data.json")["version"] == "match"
    script = Path(__file__).resolve().parents[1] / "hashi.py"
    cli = subprocess.run([sys.executable, str(script), "fingerprint", "inspect", str(delivered / "data.json")],
        capture_output=True, text=True, timeout=20)
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout)["version"] == "match"


@pytest.mark.asyncio
async def test_export_never_follows_escaping_manifest_entries(tmp_path):
    registry, store, work = registry_at(tmp_path)
    await registry.execute("file_write", {"path": "data.json", "content": "{}"}, tool_call_id="call")
    path = work / ".hashi-fingerprint/manifest.json"
    data = json.loads(path.read_bytes())
    data["artifacts"]["../outside"] = data["artifacts"].pop("data.json")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        export_bundle(work, tmp_path / "bad.zip", include_content=True)
    assert not (tmp_path / "bad.zip").exists()


@pytest.mark.parametrize("field,value", [
    ("identity", {}), ("schema", 2), ("operation_id", ""),
    ("tool_call_id", {"raw_parameters": "private"}), ("before", None),
    ("event_id", "C:\\private\\evidence"),
    ("started_at", {"raw_arguments": "secret"}), ("action", ["private"]),
    ("prepared_event_id", {"private_path": "secret"}),
])
@pytest.mark.asyncio
async def test_corrupt_receipt_never_claims_completeness_or_reexports_private_fields(tmp_path, field, value):
    registry, store, work = registry_at(tmp_path)
    await registry.execute("file_write", {"path": "data.json", "content": "{}"}, tool_call_id="call")
    path = work / ".hashi-fingerprint/manifest.json"
    data = json.loads(path.read_bytes())
    event_id = data["artifacts"]["data.json"]["latest_receipt"]
    data["receipts"][event_id][field] = value
    path.write_text(json.dumps(data))
    assert verify(work)["files"][0]["evidence_link"] == "corrupt"
    with pytest.raises(ValueError):
        export_bundle(work, tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


@pytest.mark.asyncio
async def test_malformed_index_and_zip_namespace_collisions_are_rejected(tmp_path):
    registry, store, work = registry_at(tmp_path)
    await registry.execute("file_write", {"path": "data.json", "content": "{}"}, tool_call_id="call")
    path = work / ".hashi-fingerprint/manifest.json"
    data = json.loads(path.read_bytes())
    original = json.loads(path.read_bytes())
    data["artifacts"]["data.json"] = None
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        verify(work)
    data = json.loads(json.dumps(original))
    data["artifacts"]["data.json"]["receipts"] = []
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        export_bundle(work, tmp_path / "empty-history.zip")
    assert not (tmp_path / "empty-history.zip").exists()
    data = original
    data["artifacts"]["FINGERPRINT_README.txt"] = data["artifacts"].pop("data.json")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        export_bundle(work, tmp_path / "collision.zip", include_content=True)
    assert not (tmp_path / "collision.zip").exists()
