"""Original typed HERV3 ports survive the disposable host boundary."""
from types import SimpleNamespace

import pytest

from orchestrator.her_v2.presentation import FinalStyleRequest, FinalStyleResult
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.host_protocol import pack, unpack
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.runtime_ports import HostRuntimePorts, invoke, manifest
from tests.test_permission_pao_fence import setup_run


@pytest.mark.asyncio
async def test_original_ports_are_invoked_without_exposing_other_runtime_members(tmp_path):
    _, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    calls = []

    class Style:
        async def render(self, request):
            calls.append(request)
            return FinalStyleResult(request.event_id, "rewrite", "Original Persona output", "original-owner")

    class Habits:
        async def retrieve(self, *, goal, turn_id):
            calls.append((goal, turn_id))
            return ("Retained habit",)

    runtime = SimpleNamespace(
        final_style=Style(), habits=Habits(), delivery=SimpleNamespace(allow_immediate_response=True),
        workzone_ref=str(tmp_path), private_secret="never-exported", approve=lambda: pytest.fail("not a port"),
    )
    turn = SimpleNamespace(runtime=runtime, binding=binding)

    class Client:
        def call(self, method, arguments):
            assert method == "runtime_port" and arguments["operation"] == "manifest"
            return pack(manifest(runtime))

        async def call_async(self, method, arguments):
            assert method == "runtime_port"
            return await invoke(turn, **arguments)

    ports = HostRuntimePorts(Client())
    assert ports.manifest["allow_immediate_response"] is True
    assert "never-exported" not in repr(ports.manifest)
    request = FinalStyleRequest("event", context.turn_id, "Draft", "Request")
    result = await ports.port("final_style").render(request)
    assert result.text == "Original Persona output" and calls[0] == request
    assert await ports.port("habits").retrieve(goal="task", turn_id=context.turn_id) == ("Retained habit",)
    assert ports.port("required_persona") is None
    with pytest.raises(PermissionDenied):
        await invoke(turn, "approve", {"args": [], "kwargs": {}})
    with pytest.raises(PermissionDenied):
        await ports.port("final_style").render(FinalStyleRequest("event", "other-turn", "Draft", "Request"))
    assert unpack(pack(request)) == request
