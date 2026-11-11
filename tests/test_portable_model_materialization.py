import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def builder(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "portable_model_builder_test", root / "packaging/portable_windows/build.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def linked_cache(tmp_path, *, snapshots=1):
    cache = tmp_path / "runtime/media/model-cache"
    repo = cache / "models--Example--model"
    blob = repo / "blobs" / ("a" * 40)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"frozen model bytes")
    links = []
    for index in range(snapshots):
        link = repo / "snapshots" / (str(index) * 40) / "model.bin"
        link.parent.mkdir(parents=True)
        link.symlink_to(blob)
        links.append(link)
    return blob, links


@pytest.mark.parametrize("snapshots", [1, 2])
def test_model_cache_materializes_all_references_before_retiring_blob(builder, tmp_path, snapshots):
    blob, links = linked_cache(tmp_path, snapshots=snapshots)
    assert builder.materialize_model_cache(tmp_path) == snapshots
    assert not blob.exists()
    for link in links:
        assert not link.is_symlink()
        assert link.read_bytes() == b"frozen model bytes"


def test_model_cache_rejects_escape_without_changing_external_data(builder, tmp_path):
    outside = tmp_path / "durable.bin"
    outside.write_bytes(b"preserve durable bytes")
    link = tmp_path / "runtime/media/model-cache/models--Example--model/snapshots/rev/model.bin"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    with pytest.raises(RuntimeError, match="escapes"):
        builder.materialize_model_cache(tmp_path)
    assert link.is_symlink()
    assert outside.read_bytes() == b"preserve durable bytes"


def test_model_cache_cannot_change_a_qualified_or_published_image(builder, tmp_path):
    blob, links = linked_cache(tmp_path)
    marker = tmp_path / "runtime/role-capsule.json"
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unqualified"):
        builder.materialize_model_cache(tmp_path)
    assert blob.read_bytes() == b"frozen model bytes"
    assert links[0].is_symlink()
# HASHI-Fingerprint: hfp_7ae9fe4c42c844e7b35d52ebcc65153e
