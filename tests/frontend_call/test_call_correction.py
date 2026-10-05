import asyncio
import base64
import json
from datetime import datetime, timezone
import pytest

from test_call_service import setup, start, wav, finish_task
from orchestrator.frontend_call.contract import CallError


def test_selected_route_is_persistent_and_configuration_does_not_activate(tmp_path):
    service, *_ = setup(tmp_path)
    config = service.config
    assert config.route('owner', 'agent-a')['route'] == 'phone'
    context = config.context('owner', 'agent-a')
    config.save('owner', 'agent-a', context['revision'], context['profile'])
    assert config.route('owner', 'agent-a')['route'] == 'phone'
    context = config.context('owner', 'agent-a')
    config.select_route('owner', 'agent-a', context['revision'], 'call')
    assert type(config)(config.path).route('owner', 'agent-a')['route'] == 'call'
    assert config.route('owner', 'other')['route'] == 'phone'
    with pytest.raises(CallError, match='configuration_changed'):
        config.select_route('owner', 'agent-a', context['revision'], 'phone')


async def test_disabled_call_context_keeps_minimal_phone_readiness(tmp_path):
    service, _, _, base, _ = setup(tmp_path)
    doc=json.loads(service.config.path.read_text());doc['enabled']=False
    service.config.path.write_text(json.dumps(doc))
    info=await service.invoke('owner',{**base,'operation':'context'})
    assert info['route']=='phone' and info['call_ready'] is False
    assert info['camera_available'] is False and info['video_policy']['interval_ms']>=1000
    assert 'profile' not in info and 'targets' not in info
    await service.close()


async def test_configured_cloud_start_needs_no_frontend_consent_and_preserves_privacy(tmp_path):
    service, ports, _, base, _ = setup(tmp_path, 'cloud')
    info = await service.invoke('owner', {**base, 'operation': 'context'})
    service.config.select_route('owner', base['agent_id'], info['revision'], 'call')
    info = await service.invoke('owner', {**base, 'operation': 'context'})
    body = {**base, 'operation': 'start', 'call_id': 'call-1',
            'generation': info['generation'], 'revision': info['revision']}
    result = await service.invoke('owner', body)
    assert result['phase'] == 'active'
    await service.close()
    (tmp_path / 'private').mkdir()
    service, ports, _, base, _ = setup(tmp_path / 'private', 'cloud')
    ports.level = 2
    info = await service.invoke('owner', {**base, 'operation': 'context'})
    service.config.select_route('owner', base['agent_id'], info['revision'], 'call')
    info = await service.invoke('owner', {**base, 'operation': 'context'})
    with pytest.raises(CallError, match='privacy'):
        await service.invoke('owner', {**body, 'generation': info['generation'], 'revision': info['revision']})
    await service.close()


async def test_live_video_publishes_completed_fresh_frame_while_newer_frame_is_pending(tmp_path):
    service, _, adapters, base, clock = setup(tmp_path)
    info = service.config.context('owner', 'agent-a')
    info['profile']['vision'] = {'target_id': 'eyes', 'options': {}}
    service.config.save('owner', 'agent-a', info['revision'], info['profile'])
    binding, _, _ = await start(service, base)
    await service.invoke('owner', {**binding, 'operation': 'camera', 'enabled': True})
    first, second = asyncio.Event(), asyncio.Event()
    launches = []
    async def observe(*args):
        launches.append(args)
        await (first if len(launches)==1 else second).wait()
        return 'A visible cup.'
    adapters.observe=observe
    for sequence, jpeg in ((1,'ffd8ffc00008080010001000ffd9'),(2,'ffd8ffc00008080011001000ffd9')):
        clock[0] += 2
        await service.invoke('owner', {**binding,'operation':'observe','frame_sequence':sequence,
            'image_b64':base64.b64encode(bytes.fromhex(jpeg)).decode(),
            'captured_at':datetime.now(timezone.utc).isoformat()})
        await asyncio.sleep(0)
    first.set()
    await asyncio.sleep(0)
    view=await service.invoke('owner',{**binding,'operation':'snapshot'})
    assert view['camera']['state']=='fresh' and view['camera']['observed_sequence']==1
    assert len(launches)==2
    await service.close()


async def test_silent_video_latest_frame_is_bounded_and_camera_off_discards_late_observation(tmp_path):
    service, ports, adapters, base, clock = setup(tmp_path)
    info = service.config.context('owner', 'agent-a')
    info['profile']['vision'] = {'target_id': 'eyes', 'options': {}}
    service.config.save('owner', 'agent-a', info['revision'], info['profile'])
    binding, _, _ = await start(service, base)
    await service.invoke('owner', {**binding, 'operation': 'camera', 'enabled': True})
    # Structurally valid bounded JPEG fixture used by the public media validator.
    image = base64.b64encode(bytes.fromhex('ffd8ffc00008080010001000ffd9')).decode()
    waiting = asyncio.Event()
    calls = []
    async def observe(*args):
        calls.append(args)
        await waiting.wait()
        return 'A raised hand is visible.'
    adapters.observe = observe
    def frame(sequence):
        return {**binding, 'operation': 'observe', 'frame_sequence': sequence,
                'image_b64': image, 'captured_at': datetime.now(timezone.utc).isoformat()}
    await service.invoke('owner', frame(1))
    await asyncio.sleep(0)
    for seq in range(2, 20):
        clock[0] += 2
        await service.invoke('owner', frame(seq))
    assert len(calls) == 1 and not ports.accepted
    call = service.calls['call-1']
    assert call.vision_pending['frame_sequence'] == 19
    await service.invoke('owner', {**binding, 'operation': 'camera', 'enabled': False})
    waiting.set()
    await asyncio.sleep(0)
    view = await service.invoke('owner', {**binding, 'operation': 'snapshot'})
    assert view['camera']['state'] == 'off'
    assert not call.vision_pending and not call.observation
    await service.close()
