"""Windows file-handle binding must reject path redirection and hard links."""
import os
from pathlib import Path

import pytest

from orchestrator.permissions.models import normalize_scope
from orchestrator.permissions.windows_file_binding import (
    FileBindingError,
    open_bound_create,
    open_bound_read,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows file handles required")


def _scope(path: Path, cwd: Path):
    return normalize_scope("file", str(path), ["read"], cwd=cwd)


def test_opened_file_is_read_through_held_handle(tmp_path):
    source = tmp_path / "report.txt"
    source.write_bytes(b"safe content")
    with open_bound_read(_scope(source, tmp_path)) as bound:
        assert bound.read(max_bytes=100) == b"safe content"
        with pytest.raises(OSError):
            os.replace(source, tmp_path / "moved.txt")
    os.replace(source, tmp_path / "moved.txt")
    assert (tmp_path / "moved.txt").read_bytes() == b"safe content"


def test_read_rejects_hardlinks_to_another_object(tmp_path):
    private = tmp_path / "private"
    project = tmp_path / "project"
    private.mkdir()
    project.mkdir()
    secret = private / "secret.txt"
    secret.write_bytes(b"not for project")
    public = project / "linked.txt"
    os.link(secret, public)
    with pytest.raises(FileBindingError):
        open_bound_read(_scope(public, tmp_path))


def test_approved_path_cannot_follow_a_replaced_directory(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "report.txt").write_bytes(b"project")
    (outside / "report.txt").write_bytes(b"other")
    scope = _scope(project / "report.txt", tmp_path)
    original = tmp_path / "original-project"
    os.replace(project, original)
    try:
        os.symlink(outside, project, target_is_directory=True)
    except OSError:
        pytest.skip("Windows account cannot create disposable test symlinks")
    with pytest.raises(FileBindingError):
        open_bound_read(scope)


def test_approved_file_cannot_be_replaced_with_a_symlink(tmp_path):
    source = tmp_path / "report.txt"
    private = tmp_path / "private.txt"
    source.write_bytes(b"project")
    private.write_bytes(b"private")
    scope = _scope(source, tmp_path)
    source.unlink()
    try:
        os.symlink(private, source)
    except OSError:
        pytest.skip("Windows account cannot create disposable test symlinks")
    with pytest.raises(FileBindingError):
        open_bound_read(scope)


def test_size_limit_and_action_are_enforced(tmp_path):
    source = tmp_path / "large.txt"
    source.write_bytes(b"longer")
    with open_bound_read(_scope(source, tmp_path)) as bound:
        with pytest.raises(FileBindingError):
            bound.read(max_bytes=2)
    write_only = normalize_scope("file", str(source), ["write"], cwd=tmp_path)
    with pytest.raises(FileBindingError):
        open_bound_read(write_only)


def test_parent_directory_cannot_be_renamed_while_file_is_bound(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "report.txt"
    source.write_bytes(b"content")
    with open_bound_read(_scope(source, tmp_path)) as bound:
        with pytest.raises(OSError):
            os.replace(project, tmp_path / "moved-project")
        assert bound.read(max_bytes=100) == b"content"
    os.replace(project, tmp_path / "moved-project")


def test_missing_file_and_alternate_stream_fail_closed(tmp_path):
    missing = tmp_path / "missing.txt"
    with pytest.raises(FileBindingError):
        open_bound_read(_scope(missing, tmp_path))
    source = tmp_path / "report.txt"
    source.write_bytes(b"content")
    alternate = _scope(Path(str(source) + ":private"), tmp_path)
    with pytest.raises(FileBindingError):
        open_bound_read(alternate)


@pytest.mark.asyncio
async def test_authorized_operation_reads_the_bound_windows_file(tmp_path):
    import asyncio
    from hashlib import sha256

    from orchestrator.permissions.authority import PermissionAuthority
    from orchestrator.permissions.execution_gate import PermissionExecutionGate, SupervisedResult
    from orchestrator.permissions.models import ExecutionContext
    from orchestrator.permissions.waiting import PermissionWaiter
    from tests.test_permission_execution_gate import Issuer, find_pending

    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch", backend_id="herv3",
        executor_id="test-windows-reader", policy_revision=1,
    )
    issuer = Issuer()
    authority = PermissionAuthority(tmp_path / "ledger.sqlite", verifier=issuer,
                                    clock=lambda: 1000.0, monotonic=lambda: 1000.0)
    authority.open_task(context)
    source = tmp_path / "report.txt"
    source.write_text("approved file", encoding="utf-8")
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)

    class TestSupervisor:
        # This test supervisor lives in-process; it is not an L2 security service.
        def supports(self, tool_name):
            return tool_name == "file_read"

        async def execute(self, ctx, operation_id, descriptor, arguments):
            del arguments
            with open_bound_read(descriptor.scopes[0]) as bound:
                claim = authority.prepare(ctx, operation_id, descriptor.arguments_digest, descriptor.scopes)
                authority.start(ctx, claim.claim_id)
                result = bound.read(max_bytes=100).decode("utf-8")
            digest = sha256(result.encode("utf-8")).hexdigest()
            authority.finish(ctx, claim.claim_id, outcome="succeeded", result_digest=digest)
            return SupervisedResult(operation_id, descriptor.arguments_digest, "succeeded", digest, result)

    ceiling = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    gate = PermissionExecutionGate(
        authority, waiter, TestSupervisor(), context=context, cwd=tmp_path,
        read_roots=(tmp_path,), requestable_scopes=(ceiling,),
    )
    operation = asyncio.create_task(gate.execute("operation", "file_read", {"path": "report.txt"}, reason="report"))
    request = await find_pending(authority, context)
    assert not operation.done()
    facts = authority.operation_status_for_task(context, "operation")
    issuer.approve(authority, context, request, facts.arguments_digest)
    waiter.notify()
    result = await asyncio.wait_for(operation, 1)
    assert result.state == "succeeded"
    assert result.payload == "approved file"
    assert authority.operation_state("operation") == "succeeded"


def test_new_file_is_installed_only_at_commit(tmp_path):
    target = tmp_path / "report.txt"
    scope = normalize_scope("file", str(target), ["create", "write"], cwd=tmp_path)
    with open_bound_create(scope) as bound:
        assert not target.exists()
        bound.commit("hello 世界".encode("utf-8"))
    assert target.read_text(encoding="utf-8") == "hello 世界"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["report.txt"]


def test_existing_target_and_concurrent_creation_are_never_overwritten(tmp_path, monkeypatch):
    target = tmp_path / "report.txt"
    target.write_bytes(b"existing")
    scope = normalize_scope("file", str(target), ["create", "write"], cwd=tmp_path)
    with open_bound_create(scope) as bound:
        with pytest.raises(FileBindingError):
            bound.commit(b"replacement")
    assert target.read_bytes() == b"existing"
    target.unlink()

    with open_bound_create(scope) as bound:
        rename = bound._api.rename_new

        def compete(*args):
            target.write_bytes(b"competing content")
            return rename(*args)

        monkeypatch.setattr(bound._api, "rename_new", compete)
        with pytest.raises(FileBindingError):
            bound.commit(b"replacement")
    assert target.read_bytes() == b"competing content"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["report.txt"]


def test_create_binding_requires_both_actions_and_locks_parent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "report.txt"
    only_create = normalize_scope("file", str(target), ["create"], cwd=tmp_path)
    with pytest.raises(FileBindingError):
        open_bound_create(only_create)
    scope = normalize_scope("file", str(target), ["create", "write"], cwd=tmp_path)
    with open_bound_create(scope) as bound:
        with pytest.raises(OSError):
            os.replace(project, tmp_path / "moved")
        bound.commit(b"authorized")
    assert target.read_bytes() == b"authorized"


def test_create_binding_rejects_a_directory_swapped_after_approval(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    target = project / "report.txt"
    scope = normalize_scope("file", str(target), ["create", "write"], cwd=tmp_path)
    os.replace(project, tmp_path / "saved")
    try:
        os.symlink(outside, project, target_is_directory=True)
    except OSError:
        pytest.skip("Windows account cannot create disposable test symlinks")
    with pytest.raises(FileBindingError):
        open_bound_create(scope)
    assert not (outside / "report.txt").exists()


def test_new_file_is_atomic_and_supports_unicode_names(tmp_path, monkeypatch):
    target = tmp_path / "报告🎀.txt"
    scope = normalize_scope("file", str(target), ["create", "write"], cwd=tmp_path)
    with open_bound_create(scope) as bound:
        write = bound._api.write_all

        def inspect_before_and_after(*args):
            assert not target.exists()
            write(*args)
            assert not target.exists()

        monkeypatch.setattr(bound._api, "write_all", inspect_before_and_after)
        bound.commit("完整内容".encode("utf-8"))
    assert target.read_text(encoding="utf-8") == "完整内容"


def test_file_creation_failure_cleans_its_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "report.txt"
    scope = normalize_scope("file", str(target), ["create", "write"], cwd=tmp_path)
    with open_bound_create(scope) as bound:
        def fail_write(*args):
            raise OSError("injected write failure")

        monkeypatch.setattr(bound._api, "write_all", fail_write)
        with pytest.raises(OSError, match="injected write failure"):
            bound.commit(b"data")
    assert list(tmp_path.iterdir()) == []


def test_target_symlink_created_after_binding_is_never_followed(tmp_path):
    target = tmp_path / "report.txt"
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"private")
    scope = normalize_scope("file", str(target), ["create", "write"], cwd=tmp_path)
    with open_bound_create(scope) as bound:
        try:
            os.symlink(outside, target)
        except OSError:
            pytest.skip("Windows account cannot create disposable test symlinks")
        with pytest.raises(FileBindingError):
            bound.commit(b"replacement")
    assert target.is_symlink()
    assert outside.read_bytes() == b"private"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["private.txt", "report.txt"]


def test_handle_cleanup_failure_is_reported_even_after_creation(tmp_path, monkeypatch):
    target = tmp_path / "report.txt"
    scope = normalize_scope("file", str(target), ["create", "write"], cwd=tmp_path)
    with open_bound_create(scope) as bound:
        close = bound._api.kernel.CloseHandle
        first = True

        def close_then_report_failure(handle):
            nonlocal first
            actual = close(handle)
            if first:
                first = False
                return False
            return actual

        monkeypatch.setattr(bound._api.kernel, "CloseHandle", close_then_report_failure)
        with pytest.raises(FileBindingError, match="cleanup"):
            bound.commit(b"saved")
    assert target.read_bytes() == b"saved"


@pytest.mark.asyncio
async def test_authorized_operation_creates_the_bound_windows_file(tmp_path):
    import asyncio
    from hashlib import sha256

    from orchestrator.permissions.authority import PermissionAuthority
    from orchestrator.permissions.execution_gate import PermissionExecutionGate, SupervisedResult
    from orchestrator.permissions.models import ExecutionContext
    from orchestrator.permissions.waiting import PermissionWaiter
    from tests.test_permission_execution_gate import Issuer, find_pending

    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch", backend_id="herv3",
        executor_id="test-windows-creator", policy_revision=1,
    )
    issuer = Issuer()
    authority = PermissionAuthority(tmp_path / "ledger.sqlite", verifier=issuer,
                                    clock=lambda: 1000.0, monotonic=lambda: 1000.0)
    authority.open_task(context)
    target = tmp_path / "report.txt"
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)

    class TestSupervisor:
        # This test supervisor lives in-process; it is not an L2 security service.
        def supports(self, tool_name):
            return tool_name == "file_write"

        async def execute(self, ctx, operation_id, descriptor, arguments):
            with open_bound_create(descriptor.scopes[0]) as bound:
                claim = authority.prepare(ctx, operation_id, descriptor.arguments_digest, descriptor.scopes)
                authority.start(ctx, claim.claim_id)
                bound.commit(arguments["content"].encode("utf-8"))
            output = "created"
            digest = sha256(output.encode("utf-8")).hexdigest()
            authority.finish(ctx, claim.claim_id, outcome="succeeded", result_digest=digest)
            return SupervisedResult(operation_id, descriptor.arguments_digest, "succeeded", digest, output)

    ceiling = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    gate = PermissionExecutionGate(
        authority, waiter, TestSupervisor(), context=context, cwd=tmp_path,
        read_roots=(tmp_path,), requestable_scopes=(ceiling,),
    )
    operation = asyncio.create_task(gate.execute(
        "operation", "file_write", {"path": "report.txt", "content": "approved"}, reason="report",
    ))
    request = await find_pending(authority, context)
    assert not target.exists()
    assert not operation.done()
    facts = authority.operation_status_for_task(context, "operation")
    issuer.approve(authority, context, request, facts.arguments_digest)
    waiter.notify()
    result = await asyncio.wait_for(operation, 1)
    assert result.state == "succeeded"
    assert target.read_text(encoding="utf-8") == "approved"
    assert authority.operation_state("operation") == "succeeded"


def test_create_size_limit_and_single_use_are_fail_closed(tmp_path):
    target = tmp_path / "empty.txt"
    scope = normalize_scope("file", str(target), ["create", "write"], cwd=tmp_path)
    with open_bound_create(scope) as bound:
        with pytest.raises(FileBindingError, match="size"):
            bound.commit(b"x" * (1024 * 1024 + 1))
        assert not target.exists()
        bound.commit(b"")
        with pytest.raises(FileBindingError, match="already used"):
            bound.commit(b"repeat")
    assert target.read_bytes() == b""
