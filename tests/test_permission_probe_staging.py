"""A disposable probe stage copies only its runtime and cleans only itself."""
import json
from pathlib import Path
import sys

import pytest

from scripts.permissions.windows_probe_staging import DisposableProbeStage, StageError


class FakeACL:
    def __init__(self):
        self.applied = []
        self.closed = []

    def hold(self, path):
        return object()

    def close(self, handle):
        self.closed.append(handle)

    def apply(self, path, sid, *, writable):
        self.applied.append((Path(path), sid, writable))


@pytest.fixture
def stage_fixture(tmp_path):
    parent = tmp_path / "public"
    source = tmp_path / "runtime"
    parent.mkdir()
    source.mkdir()
    (source / "python.exe").write_bytes(b"trusted interpreter")
    (source / f"python{sys.version_info.major}{sys.version_info.minor}.dll").write_bytes(b"runtime")
    (source / "Lib").mkdir()
    (source / "Lib" / "os.py").write_bytes(b"stdlib")
    (source / "Lib" / "site-packages").mkdir()
    (source / "Lib" / "site-packages" / "secret.py").write_bytes(b"not staged")
    (source / "DLLs").mkdir()
    (source / "DLLs" / "_test.pyd").write_bytes(b"extension")
    probe = tmp_path / "probe.py"
    probe.write_text("probe", encoding="utf-8")
    acl = FakeACL()
    sid = "S-1-5-21-1-2-3-1101"
    stage = DisposableProbeStage(parent, sid, source, probe, acl)
    return parent, source, probe, acl, stage


def test_stage_contains_only_verified_runtime_and_owned_scratch(stage_fixture):
    parent, source, probe, acl, stage = stage_fixture
    with stage:
        assert (stage.python / "python.exe").read_bytes() == (source / "python.exe").read_bytes()
        assert (stage.python / "Lib" / "os.py").read_bytes() == b"stdlib"
        assert not (stage.python / "Lib" / "site-packages").exists()
        assert (stage.root / "probe.py").read_bytes() == probe.read_bytes()
        assert stage.scratch.is_dir()
        assert acl.applied == [(stage.root, stage.sid, False), (stage.scratch, stage.sid, True)]
        assert json.loads((stage.root / ".hashi-test-stage.json").read_text()) == {
            "id": stage.identifier, "sid": stage.sid,
        }
    assert list(parent.iterdir()) == []
    assert len(acl.closed) == 1


def test_stage_refuses_cleanup_when_ownership_record_changed(stage_fixture):
    parent, _, _, _, stage = stage_fixture
    stage.__enter__()
    marker = stage.root / ".hashi-test-stage.json"
    marker.write_text('{"id": "other"}', encoding="utf-8")
    with pytest.raises(StageError):
        stage.cleanup()
    assert stage.root.exists()
    marker.write_text(json.dumps({"id": stage.identifier, "sid": stage.sid}), encoding="utf-8")
    stage.cleanup()
    assert list(parent.iterdir()) == []


def test_staging_failure_cleans_its_own_partial_tree(stage_fixture):
    parent, source, _, acl, stage = stage_fixture
    (source / "python.exe").unlink()
    with pytest.raises(FileNotFoundError):
        with stage:
            pass
    assert list(parent.iterdir()) == []
    assert len(acl.closed) == 1


def test_acl_failure_only_removes_its_empty_directory(stage_fixture):
    parent, _, _, acl, stage = stage_fixture

    def fail(path, sid, *, writable):
        raise StageError("injected ACL failure")

    acl.apply = fail
    with pytest.raises(StageError):
        stage.__enter__()
    assert list(parent.iterdir()) == []
    assert len(acl.closed) == 1


def test_source_reparse_point_is_never_copied(stage_fixture, monkeypatch):
    parent, source, _, _, stage = stage_fixture
    from scripts.permissions import windows_probe_staging

    check = windows_probe_staging._reparse
    monkeypatch.setattr(
        windows_probe_staging, "_reparse",
        lambda path: path == source / "Lib" / "os.py" or check(path),
    )
    with pytest.raises(StageError, match="reparse"):
        stage.__enter__()
    assert list(parent.iterdir()) == []


def test_unrelated_existing_directory_is_never_reused(stage_fixture):
    parent, _, _, _, stage = stage_fixture
    stage.root.mkdir()
    (stage.root / "important.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        stage.__enter__()
    assert (stage.root / "important.txt").read_text(encoding="utf-8") == "keep"


def test_invalid_account_sid_is_rejected(stage_fixture):
    parent, source, probe, acl, _ = stage_fixture
    with pytest.raises(StageError):
        DisposableProbeStage(parent, "S-1-5-32-544", source, probe, acl)
