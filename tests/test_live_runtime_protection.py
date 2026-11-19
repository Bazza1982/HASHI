from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from orchestrator.live_runtime_protection import load_live_runtime_policy
from orchestrator.runtime_contract import CORE_HOST_ROLE, RuntimeFingerprint
from tools.registry import ToolRegistry


def _registry(tmp_path: Path) -> tuple[ToolRegistry, dict[str, Path]]:
    bridge_home = tmp_path / "instance"
    code_root = bridge_home / "source"
    runtime_root = bridge_home / ".venv"
    workspace = tmp_path / "workzones" / "agent"
    secrets_path = bridge_home / "secrets.json"
    for path in (code_root / "orchestrator", runtime_root, workspace):
        path.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text("{}", encoding="utf-8")
    (bridge_home / "state" / "instance").mkdir(parents=True)
    (bridge_home / "state" / "instance" / "process.pid").write_text(
        "4242\n", encoding="utf-8"
    )
    global_config = SimpleNamespace(
        project_root=code_root,
        bridge_home=bridge_home,
        secrets_path=secrets_path,
        instance_id="HASHI3",
    )
    registry = ToolRegistry(
        allowed_tools=[
            "file_read",
            "file_write",
            "apply_patch",
            "shell",
            "background_job_start",
            "verification_run",
            "process_kill",
        ],
        access_root=tmp_path,
        access_roots=[tmp_path],
        workspace_dir=workspace,
        secrets={},
        audit_context={
            "global_config": global_config,
            "live_runtime_prefix": str(runtime_root),
        },
    )
    return registry, {
        "bridge_home": bridge_home,
        "code_root": code_root,
        "runtime_root": runtime_root,
        "workspace": workspace,
        "secrets_path": secrets_path,
    }


def _reason(result) -> str:
    assert result is not None
    assert result.is_error is True
    return str((result.details or {}).get("reason") or "")


def test_scheduler_delegation_key_is_protected_from_tool_read_and_write(tmp_path):
    import asyncio
    from orchestrator.workbench_auth import scheduler_delegation_key

    # The instance-private signing key is normally created at bootstrap.
    # Create it before the registry loads the protection policy so the
    # declared targets reflect a booted instance; lazily created paths join
    # the policy on the next load after they exist.
    bridge_home = tmp_path / "instance"
    original = scheduler_delegation_key(bridge_home, create=True)
    registry, paths = _registry(tmp_path)
    registry._allowed.update({"file_search", "log_query"})
    key_path = paths["bridge_home"] / "state" / "scheduler_delegation.json"

    async def exercise():
        read = await registry.execute("file_read", {"path": str(key_path)}, "read-key")
        assert read.is_error and original not in read.output
        log = await registry.execute(
            "log_query", {"path": str(key_path), "terms": [original]}, "search-key"
        )
        assert log.is_error and original not in log.output
        search = await registry.execute(
            "file_search",
            {
                "query": "key",
                "mode": "content",
                "roots": [str(paths["bridge_home"] / "state")],
                "profile": "expanded",
                "include_hidden": True,
            },
            "search-parent",
        )
        assert not search.is_error and original not in search.output
        write = await registry.execute(
            "file_write", {"path": str(key_path), "content": "changed"}, "write-key"
        )
        assert (
            write.is_error
            and scheduler_delegation_key(paths["bridge_home"]) == original
        )

    asyncio.run(exercise())


def test_lazily_created_private_paths_join_policy_only_once_they_exist(tmp_path):
    from orchestrator.workbench_auth import scheduler_delegation_key

    registry, paths = _registry(tmp_path)
    global_config = registry.audit_context["global_config"]
    runtime_prefix = registry.audit_context["live_runtime_prefix"]
    store = paths["bridge_home"] / "state" / ".hashi-tool-output-v1"
    delegation = paths["bridge_home"] / "state" / "scheduler_delegation.json"

    policy = load_live_runtime_policy(global_config, runtime_prefix=runtime_prefix)
    assert policy is not None
    read_paths = {str(path) for path in policy.protected_read_paths}
    write_paths = {str(path) for path in policy.protected_write_paths}
    assert str(store) not in read_paths
    assert str(store) not in write_paths
    assert str(delegation) not in read_paths
    assert str(delegation) not in write_paths

    store.mkdir(parents=True)
    scheduler_delegation_key(paths["bridge_home"], create=True)
    policy = load_live_runtime_policy(global_config, runtime_prefix=runtime_prefix)
    assert policy is not None
    read_paths = {str(path) for path in policy.protected_read_paths}
    write_paths = {str(path) for path in policy.protected_write_paths}
    assert str(store) in read_paths
    assert str(store) in write_paths
    assert str(delegation) in read_paths
    assert str(delegation) in write_paths


def test_runtime_protection_cache_key_notices_same_stat_selector_change(
    tmp_path,
    monkeypatch,
):
    import orchestrator.runtime_protection_targets as targets

    prefix = tmp_path / "runtime" / "core"
    prefix.mkdir(parents=True)
    selector = prefix.parent / "hashi-runtime-release.json"
    selector.write_text('{"release":1}', encoding="utf-8")
    core = RuntimeFingerprint(
        implementation="cpython",
        python="3.13.0",
        python_minor="3.13",
        cache_tag="cpython-313",
        platform_abi="test",
        platform="win32",
        machine="AMD64",
        pointer_bits=64,
        executable=str(prefix / "python.exe"),
        environment_prefix=str(prefix),
        runtime_policy_digest="sha256:test",
        dependency_digest="sha256:test",
        core_source_digest="sha256:test",
        core_api=1,
        function_api=1,
        worker_model="process",
        worker_protocol=1,
        generation_schema=1,
        role=CORE_HOST_ROLE,
    )
    monkeypatch.setattr(targets, "current_process_runtime", lambda: None)

    before_stat = selector.stat()
    before = targets.protection_context_key(core)
    selector.write_text('{"release":2}', encoding="utf-8")
    os.utime(selector, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    after = targets.protection_context_key(core)

    assert before[2][:2] == after[2][:2]
    assert before[2][2] != after[2][2]


def test_cold_runtime_failure_keeps_safe_reads_but_cannot_claim_complete_boundary(
    tmp_path, monkeypatch
):
    import asyncio
    import orchestrator.live_runtime_protection as protection
    from orchestrator.runtime_contract import RuntimeContractError

    registry, paths = _registry(tmp_path)
    registry.audit_context.update(
        {
            "agent_name": "rika",
            "instance_id": "HASHI6",
            "request_id": "request-runtime-failure",
            "hashi_session_id": "session-runtime-failure",
            "hashi_run_id": "run-runtime-failure",
        }
    )
    note = paths["workspace"] / "cold.txt"
    note.write_text("cold-read-evidence", encoding="utf-8")

    def unavailable(*args, **kwargs):
        raise RuntimeContractError("fixture qualification failed")

    monkeypatch.setattr(protection, "admitted_protection_targets", unavailable)

    async def exercise():
        result = await registry.execute("file_read", {"path": str(note)}, "cold-read")
        assert not result.is_error and "cold-read-evidence" in result.output
        assert result.details["runtime_protection"]["protection_complete"] is False
        denied = await registry.execute(
            "file_read", {"path": str(paths["secrets_path"])}, "secret"
        )
        assert denied.is_error
        destination = paths["workspace"] / "unverified.txt"
        denied = await registry.execute(
            "file_write", {"path": str(destination), "content": "unsafe"}, "write"
        )
        assert denied.is_error
        assert denied.details["tool_executed"] is False
        diagnostic = denied.details["runtime_protection"]
        assert diagnostic["execution_role"] == "functions"
        assert diagnostic["qualification_scope"] == "core_and_functions"
        assert diagnostic["instance_id"] == "HASHI6"
        assert diagnostic["agent_id"] == "rika"
        assert diagnostic["request_id"] == "request-runtime-failure"
        assert diagnostic["session_id"] == "session-runtime-failure"
        assert diagnostic["run_id"] == "run-runtime-failure"
        assert diagnostic["recovery"] == "controlled_installation_maintenance"
        assert not destination.exists()
        for path in (
            paths["runtime_root"] / "Lib/stdlib.py",
            paths["code_root"] / "main.py",
        ):
            denied = await registry.execute(
                "file_write", {"path": str(path), "content": "unsafe"}, "protected"
            )
            assert denied.is_error and not path.exists()

    asyncio.run(exercise())


def test_runtime_drift_keeps_workspace_access_and_prior_protection(
    tmp_path, monkeypatch
):
    import asyncio
    import orchestrator.live_runtime_protection as protection
    from orchestrator.runtime_contract import RuntimeContractError

    registry, paths = _registry(tmp_path)
    business = paths["workspace"] / "notes.txt"
    business.write_text("business-evidence", encoding="utf-8")

    async def exercise():
        before = await registry.execute("file_read", {"path": str(business)}, "before")
        assert not before.is_error
        assert "business-evidence" in before.output

        def broken(*args, **kwargs):
            raise RuntimeContractError(
                "Runtime installation file closure changed: unlisted=['Lib/__pycache__/sample.pyc']"
            )

        monkeypatch.setattr(protection, "admitted_protection_targets", broken)
        policy_path = (
            paths["bridge_home"] / "state/platform/live-runtime-protection.json"
        )
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text('{"schema":1}', encoding="utf-8")
        observed = await registry.execute("file_read", {"path": str(business)}, "after")
        assert not observed.is_error, observed.output
        assert "business-evidence" in observed.output

        note = paths["workspace"] / "result.txt"
        written = await registry.execute(
            "file_write", {"path": str(note), "content": "permitted"}, "write"
        )
        assert not written.is_error, written.output
        assert note.read_text(encoding="utf-8") == "permitted"
        core = paths["code_root"] / "main.py"
        denied = await registry.execute(
            "file_write", {"path": str(core), "content": "unsafe"}, "core"
        )
        assert denied.is_error
        assert not core.exists()

    asyncio.run(exercise())


def test_loader_failure_reuses_only_verified_unchanged_snapshot(tmp_path, monkeypatch):
    import asyncio
    import orchestrator.live_runtime_protection as protection
    from orchestrator.runtime_contract import RuntimeContractError

    registry, paths = _registry(tmp_path)
    registry._allowed.add("tool_catalog")
    policy_path = paths["bridge_home"] / "state/platform/live-runtime-protection.json"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text('{"schema":1}', encoding="utf-8")
    real_loader = protection.load_live_runtime_policy
    note = paths["workspace"] / "read.txt"
    note.write_text("available", encoding="utf-8")

    async def exercise():
        first = await registry.execute("file_read", {"path": str(note)}, "first")
        assert not first.is_error
        saved_stat = policy_path.stat()

        def unavailable(*_args, **_kwargs):
            raise RuntimeContractError("fixture loader failure")

        monkeypatch.setattr(protection, "load_live_runtime_policy", unavailable)
        # Trigger a reload without changing the policy bytes or runtime binding.
        os.utime(
            policy_path,
            ns=(saved_stat.st_atime_ns, saved_stat.st_mtime_ns + 1_000_000_000),
        )
        resumed = await registry.execute("file_read", {"path": str(note)}, "read")
        assert not resumed.is_error and "available" in resumed.output
        assert resumed.details["runtime_protection"]["protection_complete"] is False
        assert (
            resumed.details["runtime_protection"]["qualification"]
            == "previous_verified_snapshot"
        )
        target = paths["workspace"] / "report.txt"
        written = await registry.execute(
            "file_write", {"path": str(target), "content": "report"}, "write"
        )
        assert not written.is_error and target.read_text() == "report"
        assert written.details["runtime_protection"]["protection_complete"] is False
        secret = await registry.execute(
            "file_read", {"path": str(paths["secrets_path"])}, "secret"
        )
        assert secret.is_error
        core = paths["code_root"] / "main.py"
        blocked = await registry.execute(
            "file_write", {"path": str(core), "content": "change"}, "core"
        )
        assert blocked.is_error and not core.exists()
        alias = paths["workspace"] / "secret-alias.txt"
        os.link(paths["secrets_path"], alias)
        try:
            hidden = await registry.execute(
                "file_read", {"path": str(alias)}, "alias-read"
            )
            assert (
                hidden.is_error
                and hidden.details["control_disposition"] == "unavailable"
            )
            denied = await registry.execute(
                "file_write", {"path": str(alias), "content": "change"}, "alias-write"
            )
            assert denied.is_error and paths["secrets_path"].read_text() == "{}"
        finally:
            alias.unlink()
        old_prefix = registry.audit_context["live_runtime_prefix"]
        registry.audit_context["live_runtime_prefix"] = str(
            paths["workspace"] / "different-runtime"
        )
        blocked = await registry.execute(
            "file_read", {"path": str(note)}, "changed-runtime"
        )
        assert (
            blocked.is_error and blocked.details["control_disposition"] == "unavailable"
        )
        registry.audit_context["live_runtime_prefix"] = old_prefix

        # Even equal length and timestamp cannot hide a changed protection policy.
        changed_stat = policy_path.stat()
        policy_path.write_text('{"schema":2}', encoding="utf-8")
        os.utime(policy_path, ns=(changed_stat.st_atime_ns, changed_stat.st_mtime_ns))
        blocked = await registry.execute("file_read", {"path": str(note)}, "unknown")
        assert blocked.is_error and blocked.details["tool_executed"] is False
        assert blocked.details["control_disposition"] == "unavailable"
        catalogue = await registry.execute("tool_catalog", {}, "catalogue")
        assert not catalogue.is_error
        assert catalogue.details["runtime_protection"]["protection_complete"] is False

        policy_path.write_text('{"schema":1}', encoding="utf-8")
        monkeypatch.setattr(protection, "load_live_runtime_policy", real_loader)
        recovered = await registry.execute(
            "file_read", {"path": str(note)}, "recovered"
        )
        assert not recovered.is_error and "available" in recovered.output
        assert "runtime_protection" not in (recovered.details or {})

    asyncio.run(exercise())


def test_policy_bytes_changed_without_stat_change_invalidate_the_registry_cache(
    tmp_path,
):
    import asyncio

    registry, paths = _registry(tmp_path)
    policy_path = paths["bridge_home"] / "state/platform/live-runtime-protection.json"
    policy_path.parent.mkdir(parents=True)
    first, second = (
        paths["workspace"] / "secret-a.txt",
        paths["workspace"] / "secret-b.txt",
    )
    first.write_text("first")
    second.write_text("second")

    def content(path):
        return json.dumps({"schema": 1, "protected_read_paths": [str(path)]})

    policy_path.write_text(content(first), encoding="utf-8")

    async def exercise():
        before = await registry.execute("file_read", {"path": str(second)}, "before")
        assert not before.is_error and before.output.endswith("second")
        saved = policy_path.stat()
        old_size = len(policy_path.read_bytes())
        policy_path.write_text(content(second), encoding="utf-8")
        assert len(policy_path.read_bytes()) == old_size
        os.utime(policy_path, ns=(saved.st_atime_ns, saved.st_mtime_ns))
        after = await registry.execute("file_read", {"path": str(second)}, "after")
        assert after.is_error

    asyncio.run(exercise())


def test_loader_failure_without_previous_snapshot_keeps_unrelated_catalog_available(
    tmp_path, monkeypatch
):
    import asyncio
    import orchestrator.live_runtime_protection as protection
    from orchestrator.runtime_contract import RuntimeContractError

    registry, paths = _registry(tmp_path)
    registry._allowed.update({"tool_catalog", "web_fetch"})
    note = paths["workspace"] / "note.txt"
    note.write_text("private fixture", encoding="utf-8")

    def unavailable(*_args, **_kwargs):
        raise RuntimeContractError("fixture loader failure")

    monkeypatch.setattr(protection, "load_live_runtime_policy", unavailable)

    async def exercise():
        catalogue = await registry.execute("tool_catalog", {}, "catalogue")
        assert not catalogue.is_error
        assert catalogue.details["runtime_protection"]["protection_complete"] is False

        async def fake_fetch(name, _arguments, **_kwargs):
            assert name == "web_fetch"
            return "synthetic public result"

        monkeypatch.setattr(registry, "_dispatch_observed", fake_fetch)
        fetched = await registry.execute(
            "web_fetch", {"url": "https://example.test"}, "fetch"
        )
        assert not fetched.is_error and fetched.output == "synthetic public result"
        assert fetched.details["runtime_protection"]["protection_complete"] is False
        read = await registry.execute("file_read", {"path": str(note)}, "unknown")
        assert read.is_error and read.details["tool_executed"] is False
        assert read.details["control_disposition"] == "unavailable"

    asyncio.run(exercise())


def test_live_runtime_gate_denies_core_and_secret_but_allows_workzone(tmp_path):
    registry, paths = _registry(tmp_path)

    denied = registry.evaluate_admission(
        "file_write",
        {"path": str(paths["code_root"] / "main.py"), "content": "changed"},
    )
    assert _reason(denied) == "live_runtime_protection"

    denied = registry.evaluate_admission(
        "file_read", {"path": str(paths["secrets_path"])}
    )
    assert _reason(denied) == "live_runtime_protection"

    denied = registry.evaluate_admission(
        "file_write",
        {
            "path": str(paths["runtime_root"] / "Lib" / "site-packages" / "x.py"),
            "content": "changed",
        },
    )
    assert _reason(denied) == "live_runtime_protection"

    allowed = registry.evaluate_admission(
        "file_write",
        {"path": str(paths["workspace"] / "notes.md"), "content": "ok"},
    )
    assert allowed is None


def test_live_runtime_gate_allows_read_only_core_inspection_but_denies_shell_write(
    tmp_path,
):
    registry, paths = _registry(tmp_path)
    core_path = paths["code_root"] / "main.py"

    assert (
        registry.evaluate_admission("shell", {"command": f'git diff -- "{core_path}"'})
        is None
    )
    denied = registry.evaluate_admission(
        "shell", {"command": f'Set-Content -LiteralPath "{core_path}" -Value changed'}
    )
    assert _reason(denied) == "live_runtime_protection"


def test_live_runtime_gate_denies_live_install_but_allows_explicit_dev_environment(
    tmp_path,
):
    registry, paths = _registry(tmp_path)
    live_python = paths["runtime_root"] / "Scripts" / "python.exe"
    dev_python = paths["workspace"] / ".venv-dev" / "Scripts" / "python.exe"

    for command in (
        "python -m pip install example",
        "pip install example",
        f'"{live_python}" -m pip install example',
    ):
        denied = registry.evaluate_admission("shell", {"command": command})
        assert _reason(denied) == "live_runtime_protection"

    assert (
        registry.evaluate_admission(
            "shell", {"command": f'"{dev_python}" -m pip install example'}
        )
        is None
    )
    denied = registry.evaluate_admission(
        "verification_run",
        {"argv": [str(live_python), "-m", "pip", "install", "example"]},
    )
    assert _reason(denied) == "live_runtime_protection"


def test_live_runtime_gate_blocks_raw_current_instance_control_only(tmp_path):
    registry, _paths = _registry(tmp_path)

    denied = registry.evaluate_admission(
        "shell", {"command": "Restart-Service -Name HASHI3"}
    )
    assert _reason(denied) == "live_runtime_protection"
    assert (
        registry.evaluate_admission("shell", {"command": "Get-Service -Name HASHI3"})
        is None
    )
    assert (
        registry.evaluate_admission(
            "shell", {"command": "Restart-Service -Name PostgreSQL"}
        )
        is None
    )

    denied = registry.evaluate_admission("process_kill", {"pid": 4242})
    assert _reason(denied) == "live_runtime_protection"
    assert registry.evaluate_admission("process_kill", {"pid": 4343}) is None


def test_live_runtime_policy_extends_exact_paths_without_widening_workzone(tmp_path):
    registry, paths = _registry(tmp_path)
    policy_path = (
        paths["bridge_home"] / "state" / "platform" / "live-runtime-protection.json"
    )
    service_config = paths["bridge_home"] / "platform" / "hashi3-service.xml"
    restart_key = paths["bridge_home"] / "private" / "restart.key"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "protected_write_paths": [str(service_config)],
                "protected_read_paths": [str(restart_key)],
                "service_targets": ["HASHI3-Lifeline"],
            }
        ),
        encoding="utf-8",
    )

    denied = registry.evaluate_admission(
        "file_write", {"path": str(service_config), "content": "changed"}
    )
    assert _reason(denied) == "live_runtime_protection"
    denied = registry.evaluate_admission("file_read", {"path": str(restart_key)})
    assert _reason(denied) == "live_runtime_protection"
    denied = registry.evaluate_admission(
        "shell", {"command": "sc.exe stop HASHI3-Lifeline"}
    )
    assert _reason(denied) == "live_runtime_protection"
    denied = registry.evaluate_admission(
        "file_write", {"path": str(policy_path), "content": "{}"}
    )
    assert _reason(denied) == "live_runtime_protection"
    assert (
        registry.evaluate_admission(
            "file_write",
            {"path": str(paths["workspace"] / "result.json"), "content": "{}"},
        )
        is None
    )


def test_malformed_optional_policy_does_not_disable_derived_core_protection(tmp_path):
    registry, paths = _registry(tmp_path)
    policy_path = (
        paths["bridge_home"] / "state" / "platform" / "live-runtime-protection.json"
    )
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(
        json.dumps({"schema": 1, "protected_write_paths": ["relative/path"]}),
        encoding="utf-8",
    )

    policy = load_live_runtime_policy(
        registry.audit_context["global_config"],
        runtime_prefix=registry.audit_context["live_runtime_prefix"],
    )
    assert policy is not None
    assert policy.configuration_error
    denied = registry.evaluate_admission(
        "file_write",
        {"path": str(paths["code_root"] / "main.py"), "content": "changed"},
    )
    assert _reason(denied) == "live_runtime_protection"
    assert (
        registry.evaluate_admission(
            "file_write",
            {"path": str(paths["workspace"] / "safe.txt"), "content": "ok"},
        )
        is None
    )


# HASHI-Fingerprint: hfp_bd21f58119e14009837bbd0fcec8cc4f


def test_run_output_storage_is_protected_from_generic_tools(tmp_path):
    import asyncio
    from tools.tool_output_store import run_binding

    registry, paths = _registry(tmp_path)
    registry._allowed.update(
        {"file_search", "log_query", "file_list", "workspace_inspect"}
    )
    context = {
        "agent_name": "lily",
        "instance_id": "HASHI3",
        "owner_id": "user:one",
        "hashi_session_id": "session-one",
        "hashi_run_id": "run-one",
        "request_id": "request-one",
    }
    registry.audit_context.update(context)
    secret = "run-private-output-" + "A" * 20_000
    reference = registry._tool_output_store.retain(
        run_binding(registry.audit_context), "file_read", secret
    )
    store_root = paths["bridge_home"] / "state" / ".hashi-tool-output-v1"
    data_path = next(store_root.rglob("*.data"))

    async def exercise():
        own = await registry.execute(
            "tool_output_read", {"reference": reference, "offset": 0}
        )
        assert not own.is_error and "run-private-output" in own.output
        other = await registry.execute_with_audit_context(
            "tool_output_read",
            {"reference": reference, "offset": 0},
            audit_context={"owner_id": "user:other"},
        )
        assert other.is_error
        direct = await registry.execute("file_read", {"path": str(data_path)})
        assert direct.is_error and secret not in direct.output
        change = await registry.execute(
            "file_write", {"path": str(data_path), "content": "changed"}
        )
        assert change.is_error
        scan = await registry.execute(
            "file_search",
            {
                "query": "run-private-output",
                "mode": "content",
                "roots": [str(store_root.parent)],
                "profile": "expanded",
                "include_hidden": True,
            },
        )
        assert not scan.is_error
        assert json.loads(scan.output)["data"]["matches"] == []

    asyncio.run(exercise())
