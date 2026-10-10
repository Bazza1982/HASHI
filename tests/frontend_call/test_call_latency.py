import asyncio
import base64
from datetime import datetime, timezone

from test_call_service import setup, start, wav


async def test_turn_uses_fresh_camera_result_without_waiting_for_next_frame(tmp_path):
    await _assert_observation_wakes_turn(tmp_path, previous_failure=False)


async def test_recovering_camera_waits_for_fresh_result_then_wakes_turn(tmp_path):
    await _assert_observation_wakes_turn(tmp_path, previous_failure=True)


async def _assert_observation_wakes_turn(tmp_path, previous_failure):
    service, ports, adapters, base, clock = setup(tmp_path)
    info = service.config.context("owner", "agent-a")
    info["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save("owner", "agent-a", info["revision"], info["profile"])
    binding, _, _ = await start(service, base)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": True})
    first, second, admitted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    launches = []
    async def observe(*args):
        launches.append(args)
        await (first if len(launches) == 1 else second).wait()
        return "A visible cup."
    adapters.observe = observe
    admit = ports.admit
    async def record_admit(*args):
        result = await admit(*args)
        admitted.set()
        return result
    ports.admit = record_admit
    async def send_frame(sequence, jpeg):
        clock[0] += 2
        await service.invoke("owner", {**binding, "operation": "observe", "frame_sequence": sequence,
            "image_b64": base64.b64encode(bytes.fromhex(jpeg)).decode(),
            "captured_at": datetime.now(timezone.utc).isoformat()})
        await asyncio.sleep(0)
    try:
        if previous_failure:
            async def fail(*args):
                raise RuntimeError("Temporary observation failure")
            adapters.observe = fail
            await send_frame(1, "ffd8ffc00008080012001000ffd9")
            await service.calls["call-1"].vision_task
            assert service.calls["call-1"].vision_error
            adapters.observe = observe
        for sequence, jpeg in enumerate(
                ("ffd8ffc00008080010001000ffd9", "ffd8ffc00008080011001000ffd9"),
                start=2 if previous_failure else 1):
            await send_frame(sequence, jpeg)
        await service.invoke("owner", {**binding, "operation": "turn", "turn_id": "turn-1",
            "sequence": 1, "audio_b64": wav()})
        await asyncio.sleep(0)
        assert not ports.accepted
        first.set()
        # The first result is ready, while the same observer keeps serving newer frames.
        await asyncio.wait_for(admitted.wait(), timeout=0.5)
        assert len(launches) == 2
        assert not second.is_set()
        assert not service.calls["call-1"].vision_task.done()
        assert ports.accepted[0][2] == "A visible cup."
    finally:
        await service.close()
