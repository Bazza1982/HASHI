"""Real-file regressions for the Gateway's existing configuration owner."""
from __future__ import annotations

import errno
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import api_gateway_config as gateway
from orchestrator import config_json


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(bridge_home=tmp_path / "instance", project_root=tmp_path / "code")


def _seed(path, value, *, bom=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\r\n").encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)
    return path.read_bytes()


def _saved(path):
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def test_missing_config_has_read_only_defaults_and_explicit_save_contract(cfg):
    assert gateway.load_api_gateway_config(cfg) == {
        "enabled": False, "default_model": gateway.default_api_model(),
        "updated_at": "", "updated_by": "",
    }
    assert not cfg.bridge_home.exists()
    saved = gateway.save_api_gateway_config(cfg, enabled=True, default_model="GPT-5.5", updated_by="test")
    assert saved["enabled"] is True and saved["default_model"] == "gpt-5.5"
    assert set(saved) == {"enabled", "default_model", "updated_at", "updated_by"}
    assert _saved(gateway.config_path_for(cfg)) == saved
    assert gateway.load_api_gateway_config(cfg) == saved
    assert not cfg.project_root.exists()


def test_bom_read_is_non_mutating_and_save_preserves_unowned_fields(cfg):
    path = gateway.config_path_for(cfg)
    extension = {"future": {"values": ["原样", False, 7]}, "enabled": True, "default_model": "gpt-5.5"}
    before = _seed(path, extension, bom=True)
    stamp = path.stat().st_mtime_ns
    assert gateway.load_api_gateway_config(cfg)["enabled"] is True
    assert gateway.load_api_gateway_config(cfg)["default_model"] == "gpt-5.5"
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    result = gateway.save_api_gateway_config(cfg, enabled=False, updated_by="operator")
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw and raw.endswith(b"\n")
    assert _saved(path)["future"] == extension["future"]
    assert _saved(path)["default_model"] == "gpt-5.5"
    assert set(result) == {"enabled", "default_model", "updated_at", "updated_by"}


@pytest.mark.parametrize("bad", [b'{"enabled":', b'[]', b'null', b'\xff', b''])
def test_read_fallback_cannot_be_used_to_overwrite_broken_configuration(cfg, bad):
    path = gateway.config_path_for(cfg)
    path.parent.mkdir(parents=True)
    path.write_bytes(bad)
    assert gateway.load_api_gateway_config(cfg)["enabled"] is False
    with pytest.raises((ValueError, UnicodeError)):
        gateway.save_api_gateway_config(cfg, enabled=True)
    assert path.read_bytes() == bad
    assert not list(path.parent.glob("*.tmp*"))


@pytest.mark.parametrize("bad", [{"enabled": "false"}, {"default_model": ["gpt-5.5"]}])
def test_incompatible_known_fields_are_not_silently_normalized_during_save(cfg, bad):
    path = gateway.config_path_for(cfg)
    before = _seed(path, bad)
    with pytest.raises(ValueError):
        gateway.save_api_gateway_config(cfg, updated_by="test")
    assert path.read_bytes() == before


def test_legacy_bom_migrates_once_and_retains_source_and_extension(cfg):
    legacy = gateway.legacy_state_path_for(cfg)
    before = _seed(legacy, {"enabled": True, "default_model": "grok-4.5", "extension": {"retain": "yes"}}, bom=True)
    assert gateway.migrate_legacy_api_gateway_state(cfg) is True
    path = gateway.config_path_for(cfg)
    assert _saved(path)["extension"] == {"retain": "yes"}
    assert gateway.load_api_gateway_config(cfg)["default_model"] == "grok-4.5"
    assert legacy.read_bytes() == before
    canonical = path.read_bytes()
    legacy.write_bytes(b"broken later")
    assert gateway.migrate_legacy_api_gateway_state(cfg) is False
    assert gateway.load_api_gateway_config(cfg)["enabled"] is True
    assert path.read_bytes() == canonical
    gateway.save_api_gateway_config(cfg, enabled=False)
    assert legacy.read_bytes() == b"broken later"


@pytest.mark.parametrize("bad", [b'{"enabled":', b'[]', b'{"enabled":"false"}'])
def test_bad_legacy_is_not_migrated_or_replaced_by_a_save(cfg, bad):
    legacy = gateway.legacy_state_path_for(cfg)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(bad)
    assert gateway.load_api_gateway_config(cfg)["enabled"] is False
    with pytest.raises(ValueError):
        gateway.save_api_gateway_config(cfg, enabled=True)
    assert not gateway.config_path_for(cfg).exists()
    assert legacy.read_bytes() == bad


@pytest.mark.parametrize("invalid", ["model", "encoding"])
def test_invalid_update_does_not_first_publish_a_legacy_migration(cfg, invalid):
    legacy = gateway.legacy_state_path_for(cfg)
    before = _seed(legacy, {"enabled": False, "default_model": "gpt-5.5"})
    kwargs = {"default_model": "not-an-authorized-model"} if invalid == "model" else {"updated_by": object()}
    with pytest.raises((ValueError, TypeError)):
        gateway.save_api_gateway_config(cfg, **kwargs)
    assert legacy.read_bytes() == before
    assert not gateway.config_path_for(cfg).exists()


def test_unselected_model_value_is_preserved_not_written_back_from_effective_view(cfg):
    path = gateway.config_path_for(cfg)
    _seed(path, {"enabled": False, "default_model": "configured-on-a-newer-generation"})
    assert gateway.load_api_gateway_config(cfg)["default_model"] == gateway.default_api_model()
    saved = gateway.save_api_gateway_config(cfg, enabled=True)
    assert _saved(path)["default_model"] == "configured-on-a-newer-generation"
    assert saved["default_model"] == gateway.default_api_model()


def test_instance_model_opt_ins_and_config_location_remain_local(cfg, tmp_path):
    cfg.config_path = cfg.bridge_home / "agents.json"
    agents = _seed(cfg.config_path, {"agents": [{"allowed_backends": [{
        "engine": "codex-cli", "models": ["local-fixture-model"],
        "model_efforts": {"local-fixture-model": ["high"]},
    }]}]}, bom=True)
    result = gateway.save_api_gateway_config(cfg, default_model="LOCAL-FIXTURE-MODEL")
    assert result["default_model"] == "local-fixture-model"
    assert cfg.config_path.read_bytes() == agents
    other = SimpleNamespace(bridge_home=tmp_path / "other")
    with pytest.raises(ValueError, match="Unknown API model"):
        gateway.save_api_gateway_config(other, default_model="local-fixture-model")
    assert not other.bridge_home.exists()
    assert "local-fixture-model" not in gateway.available_api_models()
    assert not cfg.project_root.exists()


def test_conflict_preserves_another_writers_update_without_retry(cfg, monkeypatch):
    path = gateway.config_path_for(cfg)
    _seed(path, {"enabled": False, "default_model": "gpt-5.5"})
    write = gateway._write_config_atomic
    winners = []

    def interleaved(target, document):
        winner = config_json.read_config_json(target)
        winner["extension"] = "another writer"
        config_json.write_config_json(target, winner)
        winners.append(target.read_bytes())
        return write(target, document)

    with monkeypatch.context() as patch:
        patch.setattr(gateway, "_write_config_atomic", interleaved)
        with pytest.raises(config_json.ConfigConflictError):
            gateway.save_api_gateway_config(cfg, enabled=True)
    assert len(winners) == 1 and path.read_bytes() == winners[0]
    gateway.save_api_gateway_config(cfg, enabled=True)
    assert _saved(path)["extension"] == "another writer"


def test_migration_does_not_overwrite_a_concurrently_created_canonical_file(cfg, monkeypatch):
    before = _seed(gateway.legacy_state_path_for(cfg), {"enabled": True, "default_model": "gpt-5.5"})
    write = gateway._write_config_atomic
    winner = {"enabled": False, "default_model": "grok-4.5", "extension": "winner"}

    def interleaved(path, document):
        _seed(path, winner)
        return write(path, document)

    monkeypatch.setattr(gateway, "_write_config_atomic", interleaved)
    assert gateway.migrate_legacy_api_gateway_state(cfg) is False
    assert _saved(gateway.config_path_for(cfg)) == winner
    assert gateway.load_api_gateway_config(cfg)["enabled"] is False
    assert gateway.legacy_state_path_for(cfg).read_bytes() == before


@pytest.mark.parametrize("boundary", ["privacy", "sync", "replace"])
def test_prepublication_failures_preserve_bytes_and_cleanup_candidates(cfg, monkeypatch, boundary):
    path = gateway.config_path_for(cfg)
    before = _seed(path, {"enabled": False, "default_model": "gpt-5.5"}, bom=True)

    def fail(*args):
        if boundary == "privacy":
            assert args[0].read_bytes() == b""
        raise OSError(errno.ENOSPC if boundary == "sync" else errno.EACCES, "injected publication failure")

    if boundary == "privacy":
        monkeypatch.setattr(config_json, "_protect_candidate", fail)
    else:
        monkeypatch.setattr(config_json.os, "fsync" if boundary == "sync" else "replace", fail)
    with pytest.raises(OSError, match="injected publication failure"):
        gateway.save_api_gateway_config(cfg, enabled=True)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir() if p.name != f".{path.name}.lock") == [path.name]


def test_committed_durability_error_is_not_silently_retried_or_rolled_back(cfg, monkeypatch):
    path = gateway.config_path_for(cfg)
    _seed(path, {"enabled": False, "default_model": "gpt-5.5"})
    syncs = []

    def fail(parent):
        syncs.append(parent)
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(config_json, "_sync_directory", fail)
    with pytest.raises(config_json.ConfigDurabilityError) as error:
        gateway.save_api_gateway_config(cfg, enabled=True)
    assert error.value.committed is True and len(syncs) == 1
    assert _saved(path)["enabled"] is True
    assert gateway.load_api_gateway_config(cfg)["enabled"] is True


def _competing_save(home, name, barrier, results):
    cfg = SimpleNamespace(bridge_home=Path(home))
    write = gateway._write_config_atomic

    def before_publication(path, document):
        barrier.wait(timeout=15)
        return write(path, document)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(gateway, "_write_config_atomic", before_publication)
        try:
            gateway.save_api_gateway_config(cfg, enabled=True, updated_by=name)
        except config_json.ConfigConflictError:
            results.put((name, "conflict"))
        except Exception as exc:
            results.put((name, type(exc).__name__))
        else:
            results.put((name, "saved"))


@pytest.mark.parametrize("initial", ["existing", "missing"])
def test_spawned_writers_cannot_both_publish_one_revision(cfg, initial):
    path = gateway.config_path_for(cfg)
    if initial == "existing":
        _seed(path, {"enabled": False, "default_model": "gpt-5.5"})
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_competing_save, args=(str(cfg.bridge_home), name, barrier, results))
                 for name in ("writer-a", "writer-b")]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(status for _, status in outcomes) == ["conflict", "saved"]
        winner = next(name for name, status in outcomes if status == "saved")
        assert _saved(path)["updated_by"] == winner
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        results.close()
        results.join_thread()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "already_running", "stop"])
async def test_service_flags_and_handles_are_unchanged_when_save_fails(cfg, monkeypatch, operation):
    from unittest.mock import AsyncMock
    from orchestrator.service_manager import ServiceManager

    cfg.api_gateway_port = 43172
    initial_enabled = operation == "stop"
    handle = None if operation == "start" else object()
    kernel = SimpleNamespace(paths=cfg, global_cfg=cfg, api_gateway=handle,
                             enable_api_gateway=initial_enabled, secrets={})
    manager = ServiceManager(kernel)
    manager.start_api_gateway = AsyncMock()
    manager.stop_api_gateway = AsyncMock()
    path = gateway.config_path_for(cfg)
    before = _seed(path, {"enabled": initial_enabled, "default_model": "gpt-5.5"})

    def fail(*_args):
        raise PermissionError("injected sharing violation")

    monkeypatch.setattr(config_json.os, "replace", fail)
    with pytest.raises(PermissionError, match="injected sharing violation"):
        await manager.set_api_gateway_enabled(operation != "stop")
    assert path.read_bytes() == before
    assert kernel.enable_api_gateway is initial_enabled
    assert kernel.api_gateway is handle
    manager.start_api_gateway.assert_not_awaited()
    manager.stop_api_gateway.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "already_running", "stop"])
async def test_service_success_keeps_existing_contract_and_saves_before_effects(cfg, operation):
    from orchestrator.service_manager import ServiceManager

    cfg.api_gateway_port = 43172
    handle = object()
    kernel = SimpleNamespace(paths=cfg, global_cfg=cfg,
                             api_gateway=None if operation == "start" else handle,
                             enable_api_gateway=operation == "stop", secrets={})
    manager = ServiceManager(kernel)
    events = []
    path = gateway.config_path_for(cfg)
    _seed(path, {"enabled": operation == "stop", "default_model": "gpt-5.5"})

    async def start(_cfg, _secrets):
        assert _saved(path)["enabled"] is True
        assert kernel.enable_api_gateway is True
        events.append("start")
        kernel.api_gateway = handle

    async def stop(*, timeout):
        assert _saved(path)["enabled"] is False
        assert kernel.enable_api_gateway is False
        events.append("stop")
        kernel.api_gateway = None
        return True

    # Observe the service boundary; no provider/server is started by this test.
    manager.start_api_gateway = start
    manager.stop_api_gateway = stop
    ok, message = await manager.set_api_gateway_enabled(operation != "stop")
    assert ok is True and isinstance(message, str) and message
    assert events == ([] if operation == "already_running" else [operation])
    assert _saved(path)["enabled"] is (operation != "stop")
