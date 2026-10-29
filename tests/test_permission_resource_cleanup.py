"""Resource cleanup retains ownership and does not block the service event loop."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from orchestrator.permissions.file_process_supervisor import WindowsFileProcessSupervisor


def supervisor(process):
    owner = WindowsFileProcessSupervisor.__new__(WindowsFileProcessSupervisor)
    owner._active, owner._closed, owner._tasks = {}, False, set()
    owner._authority = SimpleNamespace(confirm_resource_stopped=lambda *a, **k: None)
    owner._stop_receipt = lambda *args: "observed-stop"
    resource = {"lock": asyncio.Lock(), "process": process, "bound": None, "transfer": None,
                "claim": None, "context": object(), "descriptor": object()}
    owner._active["operation"] = resource
    return owner, resource


@pytest.mark.asyncio
async def test_failed_native_stop_retains_the_owned_resource_for_retry():
    class Process:
        fail = True

        def stop(self, **kwargs):
            if self.fail:
                raise OSError("stop remains unknown")

    process = Process()
    owner, resource = supervisor(process)
    with pytest.raises(OSError):
        await owner._cleanup("operation", resource)
    assert owner._active["operation"] is resource
    process.fail = False
    await owner._cleanup("operation", resource)
    assert not owner._active


@pytest.mark.asyncio
async def test_native_wait_runs_off_the_service_loop_and_two_cleanup_owners_share_it():
    release, entered = threading.Event(), threading.Event()

    class Process:
        def stop(self, **kwargs):
            entered.set()
            assert release.wait(2)

    owner, resource = supervisor(Process())
    first = asyncio.create_task(owner._cleanup("operation", resource))
    second = asyncio.create_task(owner._cleanup("operation", resource))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        assert entered.is_set()
        # Reaching this line while native stop is waiting proves the FC/PAO
        # event loop remains responsive. Both callers own one cleanup record.
        assert not first.done() and not second.done()
    finally:
        release.set()
    await asyncio.gather(first, second)
    assert not owner._active


@pytest.mark.asyncio
async def test_a_closed_transfer_handle_is_not_closed_again_by_a_second_owner():
    closed = []

    class Kernel:
        def CloseHandle(self, handle):
            closed.append(handle)
            return len(closed) == 1

    process = SimpleNamespace(stop=lambda **kwargs: None)
    owner, resource = supervisor(process)
    resource.update(transfer=123, transfer_kernel=Kernel())
    await owner._cleanup("operation", resource)
    await owner._cleanup("operation", resource)
    assert closed == [123]


@pytest.mark.asyncio
async def test_parked_stop_proof_is_not_issued_until_every_owned_handle_is_closed():
    events = []
    process = SimpleNamespace(stop=lambda **kwargs: events.append("job"),
                              observation=SimpleNamespace(process_id=123))
    owner, resource = supervisor(process)
    resource.update(bound=SimpleNamespace(close=lambda: events.append("bound")),
                    transfer=123, transfer_kernel=SimpleNamespace(CloseHandle=lambda h: events.append("transfer") or True),
                    context=object(), claim=SimpleNamespace(claim_id="claim"),
                    descriptor=SimpleNamespace(arguments_digest="a" * 64))
    owner._stop_receipt = lambda *args: events.append("proof") or "proof"
    owner._authority.confirm_resource_stopped = lambda *a, **k: events.append("commit")
    await owner._cleanup("operation", resource)
    assert events == ["job", "bound", "transfer", "proof", "commit"]



@pytest.mark.asyncio
async def test_completed_file_path_signs_stop_only_after_parent_handles_close(tmp_path, monkeypatch):
    """Real ledger/result store and temp write; injected kernel, no OS claim."""
    import base64
    from hashlib import sha256
    import json
    from pathlib import Path
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from orchestrator.permissions import file_process_supervisor as module
    from orchestrator.permissions.authority import PermissionAuthority
    from orchestrator.permissions.descriptors import describe_tool
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.pao_run_fence import PaoRunFence
    from orchestrator.permissions.receipt_issuer import SupervisorReceiptIssuer
    from orchestrator.permissions.result_store import EncryptedResultStore
    from orchestrator.permissions.signed_receipts import SignedReceiptVerifier
    from tests.test_permission_pao_fence import setup_run

    store, _, accepted, fence, context = setup_run(tmp_path)
    key = Ed25519PrivateKey.generate()
    observer = SupervisorReceiptIssuer(key_id=context.executor_id, signing_key=key)
    approval_key = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    ceiling = normalize_scope("file", str(tmp_path), ("create", "write"), cwd=tmp_path)
    authority = PermissionAuthority(
        tmp_path / "permissions.db",
        verifier=SignedReceiptVerifier({"approval": approval_key},
                                       trusted_supervisor_keys={observer.key_id: observer.public_key}),
        baseline_resolver=lambda ctx: (ceiling,),
    )
    authority.open_task(context)
    guard = PaoRunFence(store, context=context, request_id=accepted.request_id,
                        fencing_token=fence, worker_id="INSTANCE:agent")
    authority.register_runtime_context(context, run_guard=guard)
    private = tmp_path / "private"
    private.mkdir()
    entry = private / "fixed-helper.py"
    entry.write_text("# injected test launcher does not execute this file")
    events = []

    class Bound:
        _handles = [17]

        def close(self):
            events.append("bound")

    class Kernel:
        def CloseHandle(self, handle):
            events.append("transfer")
            return True

    class Process:
        observation = SimpleNamespace(process_id=123)
        plan = None

        def start(self):
            self.started = True

        def poll(self):
            plan = self.plan
            (tmp_path / "report.txt").write_bytes(base64.b64decode(plan["data"]))
            Path(plan["result"]).write_text(json.dumps({"outcome": "succeeded", "data": ""}))
            return True

        def exit_code(self):
            return 0

        def stop(self, **kwargs):
            events.append("job")

    def launch(**arguments):
        process = Process()
        process.plan = json.loads(Path(arguments["arguments"][-2]).read_bytes())
        assert arguments["inherited_handles"] == (18,)
        return process

    monkeypatch.setattr(module, "open_bound_write", lambda scope: Bound())
    monkeypatch.setattr(module, "inheritable_handle_copy", lambda handle: (Kernel(), 18))
    monkeypatch.setattr(module, "launch_suspended", launch)
    owner = WindowsFileProcessSupervisor.__new__(WindowsFileProcessSupervisor)
    owner._authority, owner._guard = authority, guard
    owner._store = EncryptedResultStore(private / "results.db", key=bytes(range(32)))
    owner._cwd, owner._roots = tmp_path, (tmp_path,)
    owner._executable, owner._entry = entry, entry
    owner._executable_digest = owner._entry_digest = sha256(entry.read_bytes()).hexdigest()
    owner._private, owner._environment, owner._sid = private, {}, "S-1-5-21-1"
    owner._capacity, owner._active, owner._closed, owner._tasks = asyncio.Semaphore(1), {}, False, set()
    owner._validate = lambda *args: True

    def stop_receipt(ctx, claim_id, evidence):
        assert events[-2:] == ["bound", "transfer"]
        events.append("proof")
        return observer.issue_stop(ctx, claim_id, evidence)

    owner._stop_receipt = stop_receipt
    arguments = {"path": "report.txt", "content": "saved once"}
    descriptor = describe_tool("file_write", arguments, cwd=tmp_path, read_roots=(tmp_path,))
    authority.request_access(context, descriptor.scopes, purpose="Write report", operation_id="operation",
                              arguments_digest=descriptor.arguments_digest)
    result = await owner.execute(context, "operation", descriptor, arguments)
    assert result.outcome == "succeeded"
    assert (tmp_path / "report.txt").read_text() == "saved once"
    assert events[-1] == "proof" and not owner._active
    operation = authority.operation_status_for_task(context, "operation")
    assert operation.resource_state == "stopped"
