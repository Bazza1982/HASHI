from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from orchestrator.capability_broker import CapabilityBroker, CapabilityRegistration, CapabilityLeaseConflict
from orchestrator.desktop_api import DesktopSessionService, register_desktop_api
from orchestrator.desktop_contract import ACTIONS, DesktopError, validate_input
from tools.desktop_session import DesktopController
from tools.device_control_worker import _DeviceLock
from tools.windows_helper.desktop_capture import fit_size, jpeg_bytes


class Native:
    def __init__(self):
        self.calls = []; self.captures = 0; self.resets = 0; self.rev = 'display-one'; self.locked = False
    def available(self):
        if self.locked: raise DesktopError('desktop_locked', 423)
    def displays(self):
        self.available()
        return [
            {'id': 'monitor-1', 'x': -1920, 'y': 0, 'width': 1920, 'height': 1080, 'primary': True},
            {'id': 'monitor-2', 'x': 0, 'y': 0, 'width': 1920, 'height': 1080, 'primary': False}
        ], self.rev
    def cursor(self): return {'visible': True, 'x': -1000, 'y': 200}
    def capture(self, rect, size):
        self.captures += 1
        return Image.new('RGB', size, 'white')
    def inject(self, event): self.calls.append(event.copy())
    def reset(self): self.resets += 1


@pytest.fixture
def desktop(tmp_path):
    now = [100.0]; native = Native()
    c = DesktopController(native, lambda: _DeviceLock(tmp_path/'input.lock'), clock=lambda: now[0], watchdog=False)
    yield c, native, now
    c.close()


def control(c, sid='session-a', actor='owner-a', lease='lease-a'):
    return c.handle('desktop_control', {'mode':'acquire','lease_id':lease}, actor, sid)


def event(c, seq=1, sid='session-a', **kw):
    f = c.sessions[sid].frame_cache['meta']
    return {'seq':seq,'kind':'down','button':'left','x':.5,'y':.5,'frame_id':f['frame_id'],'view_revision':f['view_revision'], **kw}


def test_preview_is_bounded_shared_and_memory_only(desktop, tmp_path):
    c,n,t=desktop
    r=c.frame('', 'session-a')
    with Image.open(io.BytesIO(base64.b64decode(r['jpeg']))) as image:
        assert image.size == (1600,900)
    assert list(tmp_path.iterdir()) == []
    r2=c.frame(r['meta']['frame_id'], 'session-a')
    assert r2['jpeg'] is None and n.captures==1
    t[0]+=.6
    r3=c.frame(r['meta']['frame_id'], 'session-a')
    assert r3['jpeg'] is None and n.captures==2
    assert r3['meta']['age_ms']==0
    t[0]+=11
    assert c.frame('', 'session-a')['meta']['next_poll_ms']==2000
    t[0]+=1
    c.frame('', 'session-a'); assert n.captures==3


def test_capture_limit_and_output_size():
    assert fit_size(3840,2160)==(1600,900)
    assert fit_size(2560,1440,True)==(1280,720)
    assert fit_size(600,900)==(600,900)
    data=jpeg_bytes(Image.effect_noise((1600,900),100).convert('RGB'))
    assert len(data)<=524288


def test_coordinates_crop_and_replay_are_fenced(desktop):
    c,n,t=desktop
    c.handle('desktop_view', {'display_id':'monitor-1','crop':{'x':.5,'y':0,'width':.5,'height':1}}, 'owner-a','session-a')
    c.frame('', 'session-a'); control(c)
    e=event(c)
    assert c.input(e,'owner-a','session-a','lease-a')['seq']==1
    assert n.calls[0]['px']==-480 and n.calls[0]['py']==540
    c.input(e,'owner-a','session-a','lease-a'); assert len(n.calls)==1
    with pytest.raises(DesktopError,match='conflict'):
        c.input({**e,'x':.3},'owner-a','session-a','lease-a')
    with pytest.raises(DesktopError,match='out_of_order'):
        c.input(event(c,seq=3),'owner-a','session-a','lease-a')
    t[0]+=3.1
    with pytest.raises(DesktopError,match='stale_frame'):
        c.input(event(c,seq=2),'owner-a','session-a','lease-a')
    # Releases never require a fresh picture.
    c.input({'seq':2,'kind':'up','button':'left'},'owner-a','session-a','lease-a')
    assert n.calls[-1]['kind']=='up'


def test_human_control_holds_real_cross_process_lock(desktop,tmp_path):
    c,n,t=desktop
    control(c)
    other=_DeviceLock(tmp_path/'input.lock')
    with pytest.raises(Exception,match='lock'): other.acquire()
    with pytest.raises(DesktopError,match='busy'): control(c,'session-b','owner-b','lease-b')
    t[0]+=9; c.expire()
    assert c.owner is None and n.resets==1
    other.acquire(); other.release()


def test_wrong_actor_and_display_change_cannot_inject(desktop):
    c,n,t=desktop;c.frame('', 'session-a');control(c)
    with pytest.raises(DesktopError,match='expired'): c.input(event(c),'other-owner','session-a','lease-a')
    n.rev='new-display-layout'
    with pytest.raises(DesktopError,match='display_changed'): c.input(event(c),'owner-a','session-a','lease-a')
    assert not n.calls and c.owner is None


def test_view_change_invalidates_old_coordinates(desktop):
    c,n,t=desktop;c.frame('', 'session-a');control(c); old=event(c)
    c.handle('desktop_view',{'display_id':'monitor-1','small':True},'owner-a','session-a')
    with pytest.raises(DesktopError,match='stale_frame'): c.input(old,'owner-a','session-a','lease-a')


@pytest.mark.parametrize('bad',[
    {'seq':True,'kind':'reset'}, {'seq':1,'kind':'text','text':'x'*4097},
    {'seq':1,'kind':'down','button':'left','x':float('nan'),'y':0},
    {'seq':1,'kind':'key_down','key':'runShell'}, {'seq':1,'kind':'shell','text':'echo x'},
])
def test_reject_unbounded_or_non_input_commands(bad):
    with pytest.raises(DesktopError): validate_input(bad)


def make_broker(tmp_path,c):
    b=CapabilityBroker(SimpleNamespace(global_cfg=SimpleNamespace(instance_id='TEST'),paths=SimpleNamespace(bridge_home=tmp_path,instance_id='TEST')))
    r=CapabilityRegistration('cap-a','computer_control','TEST','device-a','user-a','windows','http','http://127.0.0.1:19111',1,tuple(ACTIONS|{'click'}),123,'generation-a','key-a',time.time(),time.time()+60)
    b._records[r.capability_id]=(r,'private')
    async def transport(reg,token,payload,timeout):
        assert payload['actor']['type']=='user' and 'agent_id' not in payload
        result=c.handle(payload['action'],payload['args'],payload['actor']['id'],payload['desktop_session_id'])
        return {'ok':True,'identity':payload['identity'],'worker_generation':reg.worker_generation,'result':result}
    b.set_transport_for_testing(transport)
    return b,r


async def test_service_binds_actor_and_target_without_agent_run(desktop,tmp_path):
    c,n,t=desktop;b,r=make_broker(tmp_path,c)
    s=DesktopSessionService(lambda:b)
    targets=await s.run('owner-a',{'operation':'targets','client_id':'client-a'})
    target=targets['targets'][0]
    opened=await s.run('owner-a',{'operation':'open','client_id':'client-a','target':target})
    request={'client_id':'client-a','session_id':opened['session_id']}
    await s.run('owner-a',{**request,'operation':'frame'})
    with pytest.raises(DesktopError,match='forbidden'):
        await s.run('owner-b',{**request,'operation':'frame'})
    with pytest.raises(DesktopError,match='forbidden'):
        await s.run('owner-a',{**request,'client_id':'another-tab','operation':'frame'})
    acquired=await s.run('owner-a',{**request,'operation':'control','mode':'acquire'})
    with pytest.raises(CapabilityLeaseConflict): b.acquire_lease(r,agent_id='real-agent',task_id='run-a')
    lease=next(iter(b._leases.values()))
    assert lease.actor_type=='user' and lease.agent_id==''
    await s.run('owner-a',{**request,'operation':'close'})
    assert c.owner is None and not b._leases


async def test_pinned_worker_generation_never_reroutes(desktop,tmp_path):
    c,n,t=desktop;b,r=make_broker(tmp_path,c)
    pin=b.desktop_targets()[0]
    pin['worker_generation']='old-generation'
    with pytest.raises(DesktopError,match='target_changed'):
        await b.invoke_desktop(pin,actor_id='owner-a',session_id='session-a',operation='desktop_frame',args={})
    assert n.captures==0


async def test_concurrent_opens_cannot_exceed_session_limit():
    class SlowBroker:
        def desktop_targets(self): return [{'capability_id': 'cap-a'}]
        async def invoke_desktop(self, *_args, **_kwargs):
            await asyncio.sleep(0)
            return {'displays': []}
    service = DesktopSessionService(SlowBroker)
    target = service.broker().desktop_targets()[0]
    results = await asyncio.gather(*(service.run('owner-a', {'operation': 'open', 'client_id': f'client-{n}', 'target': target}) for n in range(9)), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 8
    assert sum(isinstance(r, DesktopError) and r.code == 'desktop_session_limit' for r in results) == 1
    assert len(service.sessions) == 8


async def test_http_api_defaults_off_and_requires_existing_auth(tmp_path,monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    api=SimpleNamespace(app=web.Application(),orchestrator=None,admin_token='',_is_governed_profile=lambda:False,
                        _check_admin_auth=lambda r:r.headers.get('X-Workbench-Token')=='existing-token',
                        _v1_owner_id=lambda r:'owner-a')
    register_desktop_api(api)
    async with TestClient(TestServer(api.app)) as client:
        payload={'operation':'targets','client_id':'client-a'}
        monkeypatch.delenv('HASHI_DESKTOP_ENABLED',raising=False)
        r=await client.post('/api/v1/desktop/operation',json=payload); assert r.status==404
        monkeypatch.setenv('HASHI_DESKTOP_ENABLED','1')
        r=await client.post('/api/v1/desktop/operation',json=payload); assert r.status==403
        api.admin_token='existing-token'
        r=await client.post('/api/v1/desktop/operation',json=payload); assert r.status==403
        assert 'no-store' in r.headers['Cache-Control']


async def test_real_http_route_relays_frame_and_input_receipt(desktop,tmp_path,monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    c,n,t=desktop;b,_=make_broker(tmp_path,c)
    api=SimpleNamespace(app=web.Application(),orchestrator=SimpleNamespace(capability_broker=b),admin_token='existing-token',
                        global_config=SimpleNamespace(desktop_enabled=True),_is_governed_profile=lambda:False,
                        _check_admin_auth=lambda r:r.headers.get('X-Workbench-Token')=='existing-token',_v1_owner_id=lambda r:'owner-a')
    register_desktop_api(api)
    async with TestClient(TestServer(api.app)) as client:
        async def call(operation, **values):
            return await client.post('/api/v1/desktop/operation',json={'operation':operation,'client_id':'client-a',**values},headers={'X-Workbench-Token':'existing-token'})
        targets=await (await call('targets')).json()
        opened=await (await call('open',target=targets['targets'][0])).json()
        sid=opened['session_id']
        frame=await call('frame',session_id=sid)
        assert frame.status==200 and frame.content_type=='image/jpeg'
        assert (await frame.read()).startswith(b'\xff\xd8')
        import json
        meta=json.loads(frame.headers['X-Desktop-Meta'])
        same=await call('frame',session_id=sid,after_frame=meta['frame_id'])
        assert same.status==204 and 'X-Desktop-Meta' in same.headers
        lease=await (await call('control',session_id=sid,mode='acquire')).json()
        result=await (await call('input',session_id=sid,lease_id=lease['lease_id'],event=event(c,sid=sid))).json()
        assert result['seq']==1 and result['injected'] is True
        assert (await call('close',session_id=sid)).status==200
        assert c.owner is None


def test_multi_window_views_are_isolated_and_never_crosstalk(desktop):
    c, n, t = desktop
    # Session 1 sets view to monitor-1 with a top-left crop
    c.handle('desktop_view', {'display_id': 'monitor-1', 'crop': {'x': 0, 'y': 0, 'width': 0.5, 'height': 0.5}}, 'owner-1', 'session-1')
    f1 = c.handle('desktop_frame', {}, 'owner-1', 'session-1')
    assert f1['meta']['display_id'] == 'monitor-1'
    assert f1['meta']['rect'] == {'x': -1920, 'y': 0, 'width': 960, 'height': 540}

    # Session 2 in another window sets view to monitor-2 with a bottom-right crop
    c.handle('desktop_view', {'display_id': 'monitor-2', 'crop': {'x': 0.5, 'y': 0.5, 'width': 0.5, 'height': 0.5}}, 'owner-2', 'session-2')
    f2 = c.handle('desktop_frame', {}, 'owner-2', 'session-2')
    assert f2['meta']['display_id'] == 'monitor-2'
    assert f2['meta']['rect'] == {'x': 960, 'y': 540, 'width': 960, 'height': 540}

    # Verify Session 1's view was NOT modified by Session 2
    f1_again = c.handle('desktop_frame', {'after_frame': ''}, 'owner-1', 'session-1')
    assert f1_again['meta']['display_id'] == 'monitor-1'
    assert f1_again['meta']['rect'] == {'x': -1920, 'y': 0, 'width': 960, 'height': 540}

    # Session 1 changes to full monitor-1
    c.handle('desktop_view', {'display_id': 'monitor-1'}, 'owner-1', 'session-1')
    f1_full = c.handle('desktop_frame', {}, 'owner-1', 'session-1')
    assert f1_full['meta']['rect']['width'] == 1920 and f1_full['meta']['rect']['height'] == 1080

    # Session 2's view and frame remain unchanged and completely isolated
    f2_again = c.handle('desktop_frame', {'after_frame': ''}, 'owner-2', 'session-2')
    assert f2_again['meta']['display_id'] == 'monitor-2'
    assert f2_again['meta']['rect'] == {'x': 960, 'y': 540, 'width': 960, 'height': 540}

    # Session 1 closes cleanly, Session 2 continues uninterrupted
    c.handle('desktop_close', {}, 'owner-1', 'session-1')
    f2_post_close = c.handle('desktop_frame', {'after_frame': ''}, 'owner-2', 'session-2')
    assert f2_post_close['meta']['display_id'] == 'monitor-2'


def test_input_requires_a_frame_from_its_own_session(desktop):
    c, native, _ = desktop
    c.handle('desktop_view', {'display_id': 'monitor-2'}, 'owner-b', 'session-b')
    other_frame = c.handle('desktop_frame', {}, 'owner-b', 'session-b')['meta']
    control(c, sid='session-a', actor='owner-a', lease='lease-a')
    event = {
        'seq': 1, 'kind': 'move', 'x': .5, 'y': .5,
        'frame_id': other_frame['frame_id'],
        'view_revision': other_frame['view_revision'],
    }
    with pytest.raises(DesktopError, match='desktop_stale_frame'):
        c.handle('desktop_input', {'lease_id': 'lease-a', 'event': event}, 'owner-a', 'session-a')
    assert native.calls == []


def test_abandoned_view_sessions_release_cached_frames(desktop):
    c, native, now = desktop
    for index in range(12):
        session_id = f'session-{index}'
        c.handle('desktop_view', {'display_id': 'monitor-1'}, 'owner-a', session_id)
        c.handle('desktop_frame', {}, 'owner-a', session_id)
        now[0] += 61
        c.expire()
        assert len(c.sessions) == 0
    assert native.captures == 12
