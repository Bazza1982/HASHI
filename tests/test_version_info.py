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


def test_failed_git_ancestry_check_is_unknown_not_diverged(tmp_path: Path, monkeypatch) -> None:
    from orchestrator import build_provenance

    source = _checkout(tmp_path)
    original = build_provenance._run_git

    def failed_ancestry(root, *args, **kwargs):
        if args and args[0] == "merge-base":
            return SimpleNamespace(returncode=128)
        return original(root, *args, **kwargs)

    monkeypatch.setattr(build_provenance, "_run_git", failed_ancestry)
    status = inspect_source_checkout(source, running_commit="f" * 40)
    assert status["relation"] == "unknown"


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


def test_packaged_source_requires_matching_known_clean_provenance(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "package"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "hashi-bridge"\nversion = "4.0.0a2"\n', encoding="utf-8"
    )
    commit = "a" * 40
    info = {"release_channel": "portable", "commit": commit, "dirty_at_build": False}
    info_path = source / "BUILD_INFO.json"
    info_path.write_text(json.dumps({"provenance": info}), encoding="utf-8")
    home = tmp_path / "data"
    generation = "sha256:" + "a" * 64
    provenance = {"schema_version": 1, "generation_id": generation,
                  "commit": commit, "dirty_at_build": False}
    _generation_record(home, generation, provenance)
    owner = _owner(source, home, generation)
    monkeypatch.setattr("orchestrator.version_info._remote_fact", lambda *_: {
        "status": "active", "pid": 23,
        "provenance": {"commit": commit, "dirty_at_build": False},
    })

    matching = collect_version_payload(owner)
    assert matching["state"]["code"] == "packaged_release"
    assert matching["product"]["version"] == "4.0.0a2"
    info["commit"] = "b" * 40
    info_path.write_text(json.dumps({"provenance": info}), encoding="utf-8")
    different = collect_version_payload(owner)
    assert different["state"]["code"] == "mixed_generations"
    assert "packaged_source_commit_differs" in different["state"]["reasons"]
    assert "package" in render_version_card(different, locale="en").lower()

    info["commit"] = None
    info_path.write_text(json.dumps({"provenance": info}), encoding="utf-8")
    missing = collect_version_payload(owner)
    assert missing["state"]["code"] == "provenance_unavailable"
    assert "packaged_source_commit_unavailable" in missing["state"]["reasons"]

    info["commit"] = commit
    for dirty in (True, None):
        info["dirty_at_build"] = dirty
        info_path.write_text(json.dumps({"provenance": info}), encoding="utf-8")
        uncertain = collect_version_payload(owner)
        assert uncertain["state"]["code"] == "component_unverified"
        assert ("packaged_source_dirty" if dirty else "packaged_source_status_unknown") in uncertain["state"]["reasons"]


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
    assert matching["state"]["code"] == "component_unverified"
    assert "remote_unavailable" in matching["state"]["reasons"]
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


def test_packaged_unknown_build_status_stays_unknown(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "BUILD_INFO.json").write_text(json.dumps({"provenance": {
        "release_channel": "portable", "commit": "f" * 40,
        "dirty_at_build": None, "change_count_at_build": None,
    }}), encoding="utf-8")

    provenance = capture_build_provenance(package, artifact_kind="hashi-remote")
    assert provenance["dirty_at_build"] is None
    assert provenance["change_count_at_build"] is None


def test_inaccessible_git_checkout_is_not_misreported_as_a_package(tmp_path: Path, monkeypatch) -> None:
    from orchestrator import build_provenance

    source = _checkout(tmp_path)
    (source / "BUILD_INFO.json").write_text(json.dumps({"provenance": {
        "commit": "f" * 40, "dirty_at_build": False,
    }}), encoding="utf-8")
    monkeypatch.setattr(build_provenance, "_git_text", lambda *_: None)
    provenance = capture_build_provenance(source, artifact_kind="hashi-remote")
    checkout = inspect_source_checkout(source, running_commit="f" * 40)
    assert provenance["release_channel"] == "git"
    assert provenance["commit"] is None
    assert provenance["dirty_at_build"] is None
    assert checkout["available"] is True
    assert checkout["reason"] == "source_status_unavailable"
    assert checkout["dirty"] is None
    assert checkout["relation"] == "unknown"


def test_broken_git_link_does_not_fall_back_to_packaged_metadata(tmp_path: Path, monkeypatch) -> None:
    from orchestrator import build_provenance

    source = tmp_path / "checkout"
    source.mkdir()
    (source / "BUILD_INFO.json").write_text(json.dumps({"provenance": {
        "commit": "f" * 40, "dirty_at_build": False,
    }}), encoding="utf-8")
    marker = source / ".git"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == marker or original_is_symlink(path))
    monkeypatch.setattr(build_provenance, "_git_text", lambda *_: None)
    provenance = capture_build_provenance(source, artifact_kind="hashi-remote")
    checkout = inspect_source_checkout(source, running_commit="f" * 40)
    assert provenance["release_channel"] == "git" and provenance["commit"] is None
    assert checkout["available"] is True and checkout["reason"] == "source_status_unavailable"


def test_unavailable_git_status_never_claims_a_clean_checkout(tmp_path: Path, monkeypatch) -> None:
    from orchestrator import build_provenance

    source = _checkout(tmp_path)
    original = build_provenance._git_text

    def timed_out_status(root, *args):
        if args and args[0] == "status":
            return None
        return original(root, *args)

    monkeypatch.setattr(build_provenance, "_git_text", timed_out_status)
    provenance = capture_build_provenance(source, artifact_kind="hashi-remote")
    checkout = inspect_source_checkout(source, running_commit=provenance["commit"])
    assert provenance["dirty_at_build"] is None
    assert provenance["change_count_at_build"] is None
    assert checkout["dirty"] is None
    assert checkout["change_count"] is None

    home = tmp_path / "data"
    generation = "sha256:" + "a" * 64
    function_provenance = capture_build_provenance(
        source, artifact_kind="function-generation", generation_id=generation
    )
    _generation_record(home, generation, function_provenance)
    owner = _owner(source, home, generation)
    monkeypatch.setattr("orchestrator.version_info._remote_fact",
                        lambda *_: {"status": "active", "pid": 23, "provenance": provenance})
    payload = collect_version_payload(owner, agent_name="akane")
    assert payload["state"]["code"] == "component_unverified"
    assert "source_status_unavailable" in payload["state"]["reasons"]
    assert "remote_build_status_unknown" in payload["state"]["reasons"]
    assert "function_worker_build_status_unknown" in payload["state"]["reasons"]
    rendered = render_version_card(payload, locale="en").lower()
    assert "unknown" in rendered
    assert "source has uncommitted changes" not in rendered


def test_version_names_remote_mismatch_and_reports_unavailable_remote(
    tmp_path: Path, monkeypatch
) -> None:
    source = _checkout(tmp_path)
    home = tmp_path / "data"
    generation = "sha256:" + "a" * 64
    provenance = capture_build_provenance(
        source, artifact_kind="function-generation", generation_id=generation
    )
    _generation_record(home, generation, provenance)
    owner = _owner(source, home, generation)
    target_commit = provenance["commit"]
    remote_commit = "b" * 40
    remote = {
        "status": "active",
        "pid": 23,
        "provenance": {"commit": remote_commit, "dirty_at_build": False},
    }
    monkeypatch.setattr("orchestrator.version_info._remote_fact", lambda *_: remote)

    mismatch = collect_version_payload(owner, agent_name="akane")
    assert mismatch["state"]["code"] == "mixed_generations"
    assert "remote_commit_differs" in mismatch["state"]["reasons"]
    rendered = render_version_card(mismatch, locale="en")
    assert "Remote" in rendered
    assert remote_commit[:12] in rendered and target_commit[:12] in rendered
    assert "/remote off" in rendered and "/remote on" in rendered
    assert "disconnect" in rendered.lower()

    remote["provenance"]["commit"] = target_commit
    matching = collect_version_payload(owner, agent_name="akane")
    assert matching["state"]["code"] == "running_matches_source"

    remote["provenance"]["dirty_at_build"] = True
    modified_remote = collect_version_payload(owner, agent_name="akane")
    assert modified_remote["state"]["code"] == "component_unverified"
    assert "remote_dirty_at_build" in modified_remote["state"]["reasons"]
    assert "uncommitted" in render_version_card(modified_remote, locale="en").lower()
    remote["provenance"]["dirty_at_build"] = False
    assert collect_version_payload(owner, agent_name="akane")["state"]["code"] == "running_matches_source"

    remote.clear()
    remote["status"] = "offline"
    unavailable = collect_version_payload(owner, agent_name="akane")
    assert unavailable["state"]["code"] == "component_unverified"
    assert "remote_unavailable" in unavailable["state"]["reasons"]
    assert "Remote" in render_version_card(unavailable, locale="en")


def test_version_requires_live_matching_remote_health(tmp_path: Path, monkeypatch) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    source = _checkout(tmp_path)
    home = tmp_path / "data"
    generation = "sha256:" + "a" * 64
    provenance = capture_build_provenance(
        source, artifact_kind="function-generation", generation_id=generation
    )
    _generation_record(home, generation, provenance)
    owner = _owner(source, home, generation)
    claim = {
        "instance_id": "HASHI2", "root": str(home.resolve()), "pid": 23,
        "started_at": 123456.0,
        "provenance": {"commit": provenance["commit"], "dirty_at_build": False},
    }
    response = {"ok": True, "trusted_view": True, "status": "ready",
                "instance": {"instance_id": "HASHI2", "runtime_claim": dict(claim)}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    claim["port"] = server.server_port
    response["instance"]["runtime_claim"]["port"] = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("orchestrator.version_info.read_runtime_claim", lambda _root: claim)
    monkeypatch.setattr("orchestrator.version_info.pid_is_alive", lambda _pid: True)
    monkeypatch.setattr("orchestrator.remote_lifecycle.load_settings",
                        lambda _root: SimpleNamespace(use_tls=False))
    try:
        matching = collect_version_payload(owner, agent_name="akane")
        assert matching["state"]["code"] == "running_matches_source"
        assert matching["running"]["remote"]["status"] == "active"

        response["instance"]["runtime_claim"]["pid"] = 99
        wrong_process = collect_version_payload(owner, agent_name="akane")
        assert wrong_process["state"]["code"] == "component_unverified"
        assert "remote_identity_unverified" in wrong_process["state"]["reasons"]
        assert "进程身份" in render_version_card(wrong_process, locale="zh-CN")

        response["instance"]["runtime_claim"]["pid"] = 23
        response["trusted_view"] = False
        untrusted = collect_version_payload(owner, agent_name="akane")
        assert untrusted["state"]["code"] == "component_unverified"
        assert "remote_identity_unverified" in untrusted["state"]["reasons"]

        response["trusted_view"] = True
        monkeypatch.chdir(home)
        claim["root"] = ""
        response["instance"]["runtime_claim"]["root"] = ""
        missing_root = collect_version_payload(owner, agent_name="akane")
        assert missing_root["state"]["code"] == "component_unverified"
        assert "remote_identity_unverified" in missing_root["state"]["reasons"]
        claim["root"] = str(home.resolve())
        response["instance"]["runtime_claim"]["root"] = str(home.resolve())

        response["instance"]["runtime_claim"]["provenance"] = {"commit": "b" * 40}
        wrong_provenance = collect_version_payload(owner, agent_name="akane")
        assert wrong_provenance["state"]["code"] == "component_unverified"
        assert "remote_identity_unverified" in wrong_provenance["state"]["reasons"]

        response["instance"]["runtime_claim"]["provenance"] = dict(claim["provenance"])
        response["status"] = "degraded"
        degraded = collect_version_payload(owner, agent_name="akane")
        assert degraded["state"]["code"] == "component_unverified"
        assert "remote_degraded" in degraded["state"]["reasons"]
        assert "状态异常" in render_version_card(degraded, locale="zh-CN")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    unreachable = collect_version_payload(owner, agent_name="akane")
    assert unreachable["state"]["code"] == "component_unverified"
    assert "remote_unreachable" in unreachable["state"]["reasons"]
    assert "本机服务无响应" in render_version_card(unreachable, locale="zh-CN")


def test_dirty_function_generation_is_not_reported_as_exact_match(tmp_path: Path, monkeypatch) -> None:
    source = _checkout(tmp_path)
    home = tmp_path / "data"
    generation = "sha256:" + "a" * 64
    provenance = capture_build_provenance(
        source, artifact_kind="function-generation", generation_id=generation
    )
    provenance["dirty_at_build"] = True
    _generation_record(home, generation, provenance)
    owner = _owner(source, home, generation)
    remote = {"status": "active", "pid": 23,
              "provenance": {"commit": provenance["commit"], "dirty_at_build": False}}
    monkeypatch.setattr("orchestrator.version_info._remote_fact", lambda *_: remote)

    payload = collect_version_payload(owner, agent_name="akane")
    assert payload["state"]["code"] == "component_unverified"
    assert "function_worker_dirty_at_build" in payload["state"]["reasons"]
    assert "uncommitted" in render_version_card(payload, locale="en").lower()


def test_old_worker_does_not_make_matching_remote_appear_old(tmp_path: Path, monkeypatch) -> None:
    source = _checkout(tmp_path)
    home = tmp_path / "data"
    generation = "sha256:" + "a" * 64
    provenance = capture_build_provenance(
        source, artifact_kind="function-generation", generation_id=generation
    )
    _generation_record(home, generation, provenance)
    owner = _owner(source, home, generation)
    owner.runtimes[0].metadata["source_commit"] = "b" * 40
    remote = {"status": "active", "pid": 23, "provenance": {"commit": provenance["commit"]}}
    monkeypatch.setattr("orchestrator.version_info._remote_fact", lambda *_: remote)

    payload = collect_version_payload(owner, agent_name="akane")
    assert payload["state"]["code"] == "mixed_generations"
    assert "function_worker_commits_differ" in payload["state"]["reasons"]
    assert "remote_commit_differs" not in payload["state"]["reasons"]
    assert "Remote source:" not in render_version_card(payload, locale="en")


def test_unknown_functions_identity_is_not_inferred_from_a_worker(tmp_path: Path, monkeypatch) -> None:
    source = _checkout(tmp_path)
    home = tmp_path / "data"
    generation = "sha256:" + "c" * 64
    _generation_record(home, generation, None)
    owner = _owner(source, home, generation)
    commit = _git(source, "rev-parse", "HEAD")
    owner.runtimes[0].metadata["source_commit"] = commit
    monkeypatch.setattr("orchestrator.version_info._remote_fact",
                        lambda *_: {"status": "active", "pid": 23, "provenance": {"commit": commit}})

    payload = collect_version_payload(owner, agent_name="akane")
    assert payload["running"]["functions"]["source_commit"] is None
    assert payload["state"]["code"] == "component_unverified"
    assert "function_worker_identity_unavailable" in payload["state"]["reasons"]


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
        "code": "component_unverified",
        "reasons": ["remote_unavailable"],
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
