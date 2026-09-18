from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import orchestrator.runtime_release as runtime_release
from orchestrator.runtime_contract import RuntimeFingerprint


ROOT = Path(__file__).resolve().parents[1]


class _Manifest:
    def to_dict(self) -> dict:
        return {
            "schema_version": 2,
            "generation_id": "sha256:" + "a" * 64,
            "entries": [],
            "assets": [],
        }


def _payload(bridge_home: Path) -> dict:
    runtime = RuntimeFingerprint(
        implementation="cpython",
        python="3.12.13",
        python_minor="3.12",
        cache_tag="cpython-312",
        platform_abi="test-abi",
        platform="test-platform",
        machine="test-machine",
        pointer_bits=64,
        executable="test-python",
        environment_prefix="test-prefix",
        runtime_policy_digest="sha256:" + "1" * 64,
        dependency_digest="sha256:" + "2" * 64,
        core_source_digest="sha256:" + "3" * 64,
        core_api=3,
        function_api=3,
        worker_model="per-agent-process",
        worker_protocol=1,
        generation_schema=2,
    )
    return {
        "code_root": str(ROOT),
        "bridge_home": str(bridge_home),
        "runtime": runtime.to_dict(),
        "entrypoint": "orchestrator.runtime_app_host:run_runtime_app",
    }


def test_cold_release_uses_last_verified_generation_when_candidate_fails(
    tmp_path,
    monkeypatch,
):
    artifact = tmp_path / "bridge" / "state" / "function_generations" / ("a" * 64)
    generation = SimpleNamespace(manifest=_Manifest())
    candidate_error = RuntimeError("new candidate is unavailable")
    monkeypatch.setattr(
        runtime_release,
        "probe_function_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(candidate_error),
    )
    monkeypatch.setattr(
        runtime_release,
        "load_bootable_generation_cache",
        lambda *_args, **_kwargs: (generation, artifact),
    )

    release = runtime_release.qualify_release(_payload(tmp_path / "bridge"))

    assert release["generation_root"] == str(artifact)
    assert release["manifest"] == generation.manifest.to_dict()


def test_cold_release_saves_new_verified_generation_for_future_boots(
    tmp_path,
    monkeypatch,
):
    bridge_home = tmp_path / "bridge"
    artifact = bridge_home / "state" / "function_generations" / ("a" * 64)
    generation = SimpleNamespace(manifest=_Manifest())
    persisted = []
    monkeypatch.setattr(
        runtime_release,
        "probe_function_generation",
        lambda *_args, **_kwargs: generation,
    )
    monkeypatch.setattr(
        runtime_release,
        "materialize_generation_artifact",
        lambda *_args, **_kwargs: artifact,
    )
    monkeypatch.setattr(
        runtime_release,
        "persist_qualified_generation_cache",
        lambda *args: persisted.append(args),
    )

    release = runtime_release.qualify_release(_payload(bridge_home))

    assert release["generation_root"] == str(artifact)
    assert persisted == [(bridge_home.resolve(), generation, artifact)]


def test_cold_release_fails_only_without_candidate_or_verified_fallback(
    tmp_path,
    monkeypatch,
):
    candidate_error = RuntimeError("required Function code is unavailable")
    monkeypatch.setattr(
        runtime_release,
        "probe_function_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(candidate_error),
    )
    monkeypatch.setattr(
        runtime_release,
        "load_bootable_generation_cache",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="required Function code"):
        runtime_release.qualify_release(_payload(tmp_path / "bridge"))
