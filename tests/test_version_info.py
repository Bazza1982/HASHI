from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from orchestrator.build_provenance import (
    capture_build_provenance,
    inspect_source_checkout,
)
from orchestrator.runtime_version import render_version_all, render_version_card
from orchestrator.version_info import collect_version_payload, generation_provenance
from orchestrator.version_remote import (
    VersionQueryError,
    load_version_cache,
    resolve_instance_entry,
    save_version_cache,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Version Test")
    _git(root, "config", "user.email", "version@example.invalid")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "hashi-bridge"\nversion = "4.0.0a2"\n',
        encoding="utf-8",
    )
    (root / "source.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def _generation_record(
    home: Path,
    generation_id: str,
    provenance: dict | None,
) -> None:
    path = (
        home
        / "state"
        / "function_generations"
        / generation_id.removeprefix("sha256:")
        / "function-generation.json"
    )
    path.parent.mkdir(parents=True)
    payload = {"schema_version": 2, "generation_id": generation_id}
    if provenance is not None:
        payload["provenance"] = provenance
    path.write_text(json.dumps(payload), encoding="utf-8")


def _owner(source: Path, home: Path, generation_id: str):
    worker = SimpleNamespace(
        is_function_worker_proxy=True,
        name="akane",
        worker_pid=202,
        generation_id=generation_id,
        metadata={
            "worker_phase": "ACTIVE",
            "worker_accepting": True,
            "worker_adopted_at": "2026-09-09T08:40:00Z",
        },
    )
    return SimpleNamespace(
        paths=SimpleNamespace(code_root=source, bridge_home=home),
        global_cfg=SimpleNamespace(
            instance_id="HASHI2", display_name="HASHI WSL 2"
        ),
        shared_generation_id=generation_id,
        shared_adopted_at="2026-09-09T08:39:00Z",
        runtime_fingerprint={
            "runtime_id": "runtime-test",
            "python": "3.12.13",
            "platform_abi": "linux-x86_64",
            "core_api": 4,
            "function_api": 7,
            "worker_protocol": 3,
            "generation_schema": 2,
        },
        runtimes=[worker],
    )


def test_git_provenance_is_path_free_and_reports_dirty_and_detached(
    tmp_path: Path,
) -> None:
    source = _checkout(tmp_path)
    clean = capture_build_provenance(
        source,
        artifact_kind="function-generation",
        generation_id="sha256:" + "a" * 64,
        now=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )

    assert clean["product_version"] == "4.0.0a2"
    assert clean["branch"] == "main"
    assert clean["commit"] == _git(source, "rev-parse", "HEAD")
    assert clean["dirty_at_build"] is False
    assert clean["verifiable"] is True
    assert str(source) not in json.dumps(clean)

    (source / "source.txt").write_text("changed\n", encoding="utf-8")
    dirty = capture_build_provenance(source, artifact_kind="function-generation")
    assert dirty["dirty_at_build"] is True
    assert dirty["change_count_at_build"] == 1

    _git(source, "checkout", "--", "source.txt")
    _git(source, "tag", "v4.0.0a2")
    _git(source, "checkout", "--detach", "HEAD")
    detached = capture_build_provenance(source, artifact_kind="function-generation")
    assert detached["branch"] is None
    assert detached["tag"] == "v4.0.0a2"


def test_source_checkout_uses_commit_ancestry_not_timestamps(tmp_path: Path) -> None:
    source = _checkout(tmp_path)
    running = _git(source, "rev-parse", "HEAD")
    (source / "source.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "source.txt")
    _git(source, "commit", "-m", "new source")

    status = inspect_source_checkout(source, running_commit=running)

    assert status["relation"] == "source_ahead"
    assert status["commit"] != running


def test_packaged_provenance_never_invents_a_branch(tmp_path: Path) -> None:
    root = _checkout(tmp_path) / "package"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "hashi-bridge"\nversion = "4.0.0a2"\n',
        encoding="utf-8",
    )
    (root / "BUILD_INFO.json").write_text(
        json.dumps(
            {
                "provenance": {
                    "release_channel": "portable",
                    "commit": "f" * 40,
                    "commit_time": "2026-09-09T01:02:03Z",
                    "build_time": "2026-09-09T02:03:04Z",
                }
            }
        ),
        encoding="utf-8",
    )

    provenance = capture_build_provenance(root, artifact_kind="function-generation")
    source = inspect_source_checkout(root, running_commit="f" * 40)

    assert provenance["release_channel"] == "portable"
    assert provenance["commit"] == "f" * 40
    assert provenance["branch"] is None
    assert source["available"] is False
    assert source["reason"] == "packaged_install"


def test_payload_distinguishes_source_ahead_mixed_and_legacy(
    tmp_path: Path, monkeypatch
) -> None:
    source = _checkout(tmp_path)
    home = tmp_path / "data"
    generation = "sha256:" + "a" * 64
    provenance = capture_build_provenance(
        source,
        artifact_kind="function-generation",
        generation_id=generation,
    )
    _generation_record(home, generation, provenance)
    owner = _owner(source, home, generation)
    monkeypatch.setattr(
        "orchestrator.version_info.read_runtime_claim", lambda _root: None
    )

    matching = collect_version_payload(owner, agent_name="akane")
    assert matching["running"]["commit"] == provenance["commit"]
    assert matching["state"]["code"] == "running_matches_source"
    assert matching["compatibility"]["core_api"] == 4

    (source / "source.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "source.txt")
    _git(source, "commit", "-m", "source ahead")
    ahead = collect_version_payload(owner, agent_name="akane")
    assert ahead["running"]["commit"] == provenance["commit"]
    assert ahead["source"]["commit"] != ahead["running"]["commit"]
    assert ahead["state"]["code"] == "source_ahead"

    owner.runtimes.append(
        SimpleNamespace(
            is_function_worker_proxy=True,
            name="kasumi",
            worker_pid=303,
            generation_id="sha256:" + "b" * 64,
            metadata={"worker_phase": "ACTIVE", "worker_accepting": True},
        )
    )
    mixed = collect_version_payload(owner, agent_name="akane")
    assert mixed["running"]["mixed_workers"] is True
    assert mixed["state"]["code"] == "mixed_generations"

    legacy = "sha256:" + "c" * 64
    _generation_record(home, legacy, None)
    owner.shared_generation_id = legacy
    owner.runtimes = [owner.runtimes[0]]
    owner.runtimes[0].generation_id = legacy
    unavailable = collect_version_payload(owner, agent_name="akane")
    assert generation_provenance(home, legacy) is None
    assert unavailable["running"]["commit"] is None
    assert unavailable["state"]["code"] == "provenance_unavailable"


def test_payload_reports_live_adoption_commit_for_reused_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _checkout(tmp_path)
    home = tmp_path / "data"
    generation_id = "sha256:" + "a" * 64
    artifact_provenance = capture_build_provenance(
        source,
        artifact_kind="function-generation",
        generation_id=generation_id,
    )
    artifact_commit = artifact_provenance["commit"]
    _generation_record(home, generation_id, artifact_provenance)
    owner = _owner(source, home, generation_id)

    (source / "README.md").write_text("documentation only\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "documentation only")
    adopted_commit = _git(source, "rev-parse", "HEAD")
    live_generation = SimpleNamespace(
        receipt=SimpleNamespace(source_commit=adopted_commit)
    )
    owner._startup_artifact = (live_generation, home)
    owner.runtimes[0].client = SimpleNamespace(generation=live_generation)
    monkeypatch.setattr(
        "orchestrator.version_info.read_runtime_claim", lambda _root: None
    )

    payload = collect_version_payload(owner, agent_name="akane")

    assert payload["running"]["commit"] == adopted_commit
    assert payload["running"]["functions"]["source_commit"] == adopted_commit
    assert payload["running"]["worker"]["source_commit"] == adopted_commit
    assert payload["running"]["functions"]["provenance"]["commit"] == artifact_commit
    assert artifact_commit != adopted_commit
    assert payload["source"]["commit"] == adopted_commit
    assert payload["state"] == {
        "code": "running_matches_source",
        "reasons": [],
    }


def test_renderers_localize_and_mark_stale_cache(tmp_path: Path) -> None:
    payload = {
        "product": {"version": "4.0.0a2"},
        "instance": {
            "id": "HASHI2",
            "display_name": "HASHI WSL 2",
            "environment": "wsl",
            "python": "3.12.13",
        },
        "running": {
            "branch": "main",
            "commit": "8" * 40,
            "commit_time": "2026-09-09T04:26:00Z",
            "adopted_at": "2026-09-09T08:42:00Z",
            "functions": {"generation_id": "sha256:" + "a" * 64, "pid": 11},
            "worker": {"generation_id": "sha256:" + "a" * 64, "pid": 12},
            "workers": [],
            "mixed_workers": False,
            "remote": {"status": "active", "pid": 13},
        },
        "source": {
            "available": True,
            "branch": "main",
            "commit": "8" * 40,
            "dirty": False,
        },
        "compatibility": {},
        "state": {"code": "running_matches_source", "reasons": []},
    }

    english = render_version_card(payload, locale="en")
    chinese = render_version_card(payload, locale="zh-CN")
    stale = render_version_all(
        [
            {
                "instance_id": "HASHI2",
                "verification": "stale",
                "payload": payload,
                "verified_at": "2026-09-09T08:20:00Z",
            }
        ],
        locale="en",
        current_instance="HASHI1",
    )

    assert "Running matches source" in english
    assert "正在运行的版本与源码一致" in chinese
    assert "Currently unreachable" in stale
    assert "Current version is unverified" in stale

    save_version_cache(
        tmp_path,
        {"HASHI2": {"verified_at": "now", "payload": payload}},
    )
    assert (
        load_version_cache(tmp_path)["HASHI2"]["payload"]["instance"]["id"]
        == "HASHI2"
    )


def test_instance_resolver_rejects_unknown_and_ambiguous_names() -> None:
    instances = {
        "hashi2": {"instance_id": "HASHI2", "display_name": "Shared"},
        "hashi3": {"instance_id": "HASHI3", "display_name": "Shared"},
    }
    assert resolve_instance_entry(instances, "@hashi2")[0] == "HASHI2"
    for query, expected in (
        ("missing", "unknown_instance"),
        ("Shared", "ambiguous_instance"),
    ):
        try:
            resolve_instance_entry(instances, query)
        except VersionQueryError as exc:
            assert exc.code == expected
        else:
            raise AssertionError(f"{expected} was accepted")


def _role_fixture(role, prefix, digit):
    return {
        "role": role, "runtime_id": prefix + ":sha256:" + digit * 64,
        "core_source_digest": "sha256:" + "c" * 64, "python": "3.12.13",
        "platform_abi": "test-abi", "dependency_digest": "sha256:" + digit * 64,
        "runtime_policy_digest": "sha256:" + "d" * 64,
        "executable": "private-runtime-location/python.exe",
        "environment_prefix": "private-runtime-location",
    }


def test_version_roles_keep_core_fixed_through_functions_observation_changes(tmp_path, monkeypatch):
    source = _checkout(tmp_path)
    owner = _owner(source, tmp_path / "data", "sha256:" + "e" * 64)
    owner.kernel_pid = 101
    owner.core_runtime_fingerprint = _role_fixture("core-host", "cr", "a")
    monkeypatch.setattr("orchestrator.version_info.read_runtime_claim", lambda _root: None)
    observations = []
    for digit in ("1", "2", "1"):
        owner.runtime_fingerprint = _role_fixture("functions", "fr", digit)
        payload = collect_version_payload(owner, agent_name="akane")
        roles = payload["runtime_roles"]
        assert roles["core_host"]["pid"] == 101
        assert roles["core_host"]["runtime_id"] == "cr:sha256:" + "a" * 64
        assert roles["core_host"]["core_source_digest"] == "sha256:" + "c" * 64
        assert roles["functions"]["runtime_id"] == "fr:sha256:" + digit * 64
        assert payload["running"]["functions"]["generation_id"] == "sha256:" + "e" * 64
        assert "private-runtime-location" not in json.dumps(roles)
        card = render_version_card(payload, locale="en", full=True)
        assert roles["core_host"]["runtime_id"] in card
        assert roles["functions"]["runtime_id"] in card
        assert "Core runtime" in card and "Functions runtime" in card
        observations.append(roles)
    assert observations[0]["core_host"] == observations[1]["core_host"] == observations[2]["core_host"]
    assert observations[0]["functions"]["runtime_id"] == observations[2]["functions"]["runtime_id"]
    assert observations[0]["functions"]["runtime_id"] != observations[1]["functions"]["runtime_id"]


def test_version_roles_never_infer_missing_core_from_functions_or_current_checkout(tmp_path, monkeypatch):
    owner = _owner(_checkout(tmp_path), tmp_path / "data", "sha256:" + "e" * 64)
    owner.runtime_fingerprint = _role_fixture("functions", "fr", "1")
    monkeypatch.setattr("orchestrator.version_info.read_runtime_claim", lambda _root: None)
    for claimed_core in (None, {}, owner.runtime_fingerprint):
        owner.core_runtime_fingerprint = claimed_core
        roles = collect_version_payload(owner)["runtime_roles"]
        assert roles["core_host"] is None
        assert roles["functions"]["runtime_id"].startswith("fr:")


def test_agent_version_uses_private_topology_core_and_its_own_functions_runtime(tmp_path, monkeypatch):
    owner = _owner(_checkout(tmp_path), tmp_path / "data", "sha256:" + "e" * 64)
    del owner.runtime_fingerprint
    owner._topology = {
        "kernel_pid": 101,
        "core_runtime": _role_fixture("core-host", "cr", "a"),
        "runtime": _role_fixture("functions", "fr", "1"),
    }
    agent = SimpleNamespace(
        orchestrator=owner, name="akane",
        _runtime_fingerprint=_role_fixture("functions", "fr", "2"),
    )
    monkeypatch.setattr("orchestrator.version_info.read_runtime_claim", lambda _root: None)
    roles = collect_version_payload(agent)["runtime_roles"]
    assert roles["core_host"]["pid"] == 101
    assert roles["core_host"]["runtime_id"].startswith("cr:")
    assert roles["functions"]["pid"] == 202
    assert roles["functions"]["runtime_id"] == "fr:sha256:" + "2" * 64
# HASHI-Fingerprint: hfp_41d651eeacfd48d49e7dbb2882a29b18


def test_version_roles_derive_ids_from_canonical_objects_and_serialized_topology(tmp_path, monkeypatch):
    from dataclasses import replace
    from orchestrator.runtime_contract import CORE_HOST_ROLE, enforce_runtime_contract

    functions = enforce_runtime_contract(Path(__file__).resolve().parents[1])
    core = replace(functions, role=CORE_HOST_ROLE, installation_digest="sha256:" + "a" * 64)
    owner = _owner(_checkout(tmp_path), tmp_path / "data", "sha256:" + "e" * 64)
    owner.kernel_pid = 101
    owner.runtime_fingerprint = functions
    owner.core_runtime_fingerprint = core
    monkeypatch.setattr("orchestrator.version_info.read_runtime_claim", lambda _root: None)
    direct = collect_version_payload(owner)
    assert direct["runtime_roles"]["core_host"]["runtime_id"] == core.runtime_id
    assert direct["runtime_roles"]["functions"]["runtime_id"] == functions.runtime_id
    assert direct["compatibility"]["runtime_id"] == functions.runtime_id

    del owner.kernel_pid
    del owner.core_runtime_fingerprint
    del owner.runtime_fingerprint
    owner._topology = {
        "kernel_pid": 101, "core_runtime": core.to_dict(), "runtime": functions.to_dict(),
    }
    serialized = collect_version_payload(owner)
    assert serialized["runtime_roles"]["core_host"] == direct["runtime_roles"]["core_host"]
    assert serialized["runtime_roles"]["functions"] == direct["runtime_roles"]["functions"]
    assert serialized["compatibility"]["runtime_id"] == functions.runtime_id
