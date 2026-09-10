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
