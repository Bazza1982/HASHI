import hashlib
import io
from types import SimpleNamespace

import pytest
from scripts import provision_paddle_ocr_models as models


def manifest(data):
    return {
        "source": {"huggingface_revisions": {"fixture": "a" * 40}},
        "models": [
            {
                "name": "fixture",
                "files": [
                    {
                        "path": "inference.pdiparams",
                        "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ],
            }
        ],
    }


def test_official_mirror_bytes_are_verified_before_replacing_a_model(
    tmp_path, monkeypatch
):
    target = tmp_path / "fixture"
    target.mkdir()
    (target / "inference.pdiparams").write_bytes(b"old")
    monkeypatch.setattr(
        models.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"wrong")
    )
    with pytest.raises(ValueError):
        models.provision_huggingface(tmp_path, manifest(b"valid"))
    assert (target / "inference.pdiparams").read_bytes() == b"old"
    assert not list(target.glob("*.partial"))
    monkeypatch.setattr(
        models.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"valid")
    )
    models.provision_huggingface(tmp_path, manifest(b"valid"))
    assert (target / "inference.pdiparams").read_bytes() == b"valid"


def test_slow_model_stream_has_a_total_deadline_and_cannot_publish_partial_bytes(
    tmp_path, monkeypatch
):
    times = iter([0.0, 241.0])
    monkeypatch.setattr(models, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(
        models.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"valid")
    )
    with pytest.raises(TimeoutError):
        models.provision_huggingface(tmp_path, manifest(b"valid"))
    assert not (tmp_path / "fixture" / "inference.pdiparams").exists()
    assert not list(tmp_path.rglob("*.partial"))
