"""Offline script scope and Worker entry behavior; no OS sandbox is claimed."""
import base64
from dataclasses import asdict
from hashlib import sha256
import sys

import pytest

from orchestrator.permissions.windows_capabilities import PYTHON_RUNTIME_CAPABILITIES

from orchestrator.permissions.descriptors import describe_tool
from orchestrator.permissions.script_protocol import validate
from orchestrator.permissions.script_runner import unpack_plan
from tests.test_permission_runner_records import environment


def test_two_script_operations_do_not_share_the_host_reservation_or_each_other(tmp_path):
    binding, records, _, profiles = environment(tmp_path)
    host = records.reserve(binding)
    one = records.reserve(binding, operation_id="script-one")
    two = records.reserve(binding, operation_id="script-two")
    assert len({host["profile"], one["profile"], two["profile"]}) == 3
    assert len({host["scratch"], one["scratch"], two["scratch"]}) == 3
    records.retire(binding, operation_id="script-one")
    assert profiles == [one["profile"]] and len(records.pending()) == 2
    records.recover_all()
    assert not records.pending()
    with pytest.raises(ValueError, match="cannot be created again"):
        records.reserve(binding, operation_id="script-one")


def test_script_descriptor_and_snapshots_bind_exact_read_only_inputs(tmp_path):
    arguments = {"script": "print('ok')", "inputs": [{"path": str(tmp_path / "input.txt"), "name": "input.txt"}],
                 "outputs": ["result.json"]}
    descriptor = describe_tool("run_script", arguments, cwd=tmp_path, read_roots=(tmp_path,))
    assert any(scope.kind == "capability" and scope.resource == "process.python.isolated"
               and scope.actions == ("execute",) for scope in descriptor.scopes)
    assert [scope.actions for scope in descriptor.scopes if scope.kind == "file"] == [("read",)]
    blob = b"exact input snapshot"
    plan = {"arguments": arguments, "cwd": str(tmp_path), "descriptor": asdict(descriptor),
            "snapshots": [{"name": "input.txt", "data": base64.b64encode(blob).decode(),
                           "sha256": sha256(blob).hexdigest()}]}
    _, received, decoded = unpack_plan(plan)
    assert received == descriptor and decoded == [("input.txt", blob)]
    plan["snapshots"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        unpack_plan(plan)
    for bad in (
        {**arguments, "outputs": ["../business.txt"]},
        {**arguments, "inputs": [{"path": "input.txt", "name": "C:secret"}]},
        {**arguments, "outputs": ["NUL.txt"]},
        {**arguments, "network": ["https://other.example"]},
        {**arguments, "script": "invalid python ("},
    ):
        with pytest.raises(ValueError):
            validate(bad)


def test_fixed_worker_captures_only_its_script_output_and_restores_process_streams(tmp_path, monkeypatch):
    from orchestrator.permissions.script_worker import main

    (tmp_path / "inputs").mkdir()
    (tmp_path / "outputs").mkdir()
    (tmp_path / "inputs/input.txt").write_text("snapshot", encoding="utf-8")
    (tmp_path / "task.py").write_text(
        "from pathlib import Path\n"
        "value=Path('inputs/input.txt').read_text(encoding='utf-8')\n"
        "Path('outputs/result.txt').write_text(value.upper(),encoding='utf-8')\n"
        "print('finished')\n", encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["script_worker.py", str(tmp_path)])
    before = sys.stdout, sys.stderr
    main()
    assert (sys.stdout, sys.stderr) == before
    assert (tmp_path / "stdout.txt").read_text(encoding="utf-8") == "finished\n"
    assert (tmp_path / "outputs/result.txt").read_text(encoding="utf-8") == "SNAPSHOT"
    assert (tmp_path / "inputs/input.txt").read_text(encoding="utf-8") == "snapshot"


@pytest.mark.skipif(sys.platform != "win32", reason="native held input handle")
@pytest.mark.parametrize("observation_changes", [
    {}, {"elevated": True},
    {"capability_sids": ("S-1-15-3-1",)},
    {"capability_count": 2, "capability_sids": (*PYTHON_RUNTIME_CAPABILITIES, "S-1-15-3-1")},
    {"capability_count": 0, "capability_sids": ()},
    {"capability_count": True},
])
def test_fixed_safety_helper_only_snapshots_inputs_and_never_executes_python(tmp_path, monkeypatch, observation_changes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from orchestrator.permissions.models import RuntimeExecutionBinding, normalize_scope
    from orchestrator.permissions.script_helper import ScriptNotStarted, run
    from orchestrator.permissions.windows_file_binding import open_bound_read
    from orchestrator.permissions.windows_process import inheritable_handle_copy
    from orchestrator.permissions.wire import encode
    from tests.test_permission_pao_fence import setup_run

    _, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    source = tmp_path / "input.txt"
    source.write_bytes(b"original input")
    arguments = {"script": "raise RuntimeError('must run only inside the isolated Runner')",
                 "inputs": [{"path": str(source), "name": "input.txt"}], "outputs": []}
    descriptor = describe_tool("run_script", arguments, cwd=tmp_path, read_roots=(tmp_path,))
    public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    calls = []
    payload = '{"state":"completed","exit_code":0,"outputs":[]}'
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def call(self, method, values, **kwargs):
            calls.append(method)
            if method == "prepare_script":
                snapshot = values["plan"]["snapshots"][0]
                assert base64.b64decode(snapshot["data"]) == b"original input"
                assert snapshot["sha256"] == sha256(b"original input").hexdigest()
                return {"user_sid": "S-1-5-21-2", "lpac": True, "elevated": False,
                        "atomic_job": True, "capability_count": 1, "capability_sids": PYTHON_RUNTIME_CAPABILITIES, "parent_job_count": 2,
                        "appcontainer_sid": "S-1-15-2-123", **observation_changes}
            if method == "run_script":
                return {"outcome": "succeeded", "payload": payload,
                        "result_digest": sha256(payload.encode()).hexdigest()}
            assert method == "stop_script"
            return {"stopped": True}
    monkeypatch.setattr("orchestrator.permissions.script_helper.PermissionPipeClient", Client)
    plan = {"arguments": arguments, "cwd": str(tmp_path), "descriptor": asdict(descriptor),
            "binding": encode(binding), "operation_id": "operation", "parent_job_name": "reserved-helper",
            "runner": {"endpoint": "private-test-endpoint", "public_key": base64.b64encode(public).decode(),
                       "root_job_name": "reserved-root", "runner_sid": "S-1-5-21-2"},
            "input_indices": [0], "result": str(tmp_path / "result.json")}
    with open_bound_read(normalize_scope("file", str(source), ["read"], cwd=tmp_path)) as bound:
        _, transfer = inheritable_handle_copy(bound._handles[-1])
        if observation_changes:
            with pytest.raises(ScriptNotStarted):
                run(plan, [transfer])
            assert calls == ["prepare_script", "stop_script"]
        else:
            result = run(plan, [transfer])
            assert base64.b64decode(result["data"]).decode() == payload
            assert calls == ["prepare_script", "run_script", "stop_script"]
    assert source.read_bytes() == b"original input"
