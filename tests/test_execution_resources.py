from __future__ import annotations
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
from orchestrator.execution_resources import FileLease, execution_configuration, leases, limit, tool_resource


def test_os_leases_allow_independent_readers_but_exclude_overlapping_mutations(tmp_path):
    first=FileLease(tmp_path,'workspace',shared=True)
    second=FileLease(tmp_path,'workspace',shared=True)
    exclusive=FileLease(tmp_path,'workspace')
    assert first.try_acquire()
    try:
        assert second.try_acquire()
        assert not exclusive.try_acquire()
        second.close()
        assert not exclusive.try_acquire(), 'the remaining reader still owns its lease'
    finally:
        first.close();second.close();exclusive.close()
    assert exclusive.try_acquire()
    exclusive.close()


@pytest.mark.asyncio
async def test_failed_multi_quota_pass_releases_partial_slots_and_cancellation_releases_owner(tmp_path):
    occupied=FileLease(tmp_path,'z:0')
    assert occupied.try_acquire()
    reasons=[]
    async def waiting():
        async with leases(tmp_path,[('a',1,False),('z',1,False)],on_wait=reasons.append):
            pytest.fail('occupied slot admitted another owner')
    task=asyncio.create_task(waiting())
    try:
        while not reasons:
            await asyncio.sleep(.01)
        available=FileLease(tmp_path,'a:0')
        assert available.try_acquire(), 'a blocked group cannot retain an earlier quota'
        available.close()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    finally:
        occupied.close();task.cancel();await asyncio.gather(task,return_exceptions=True)
    async with leases(tmp_path,[('a',1,False),('z',1,False)]):pass


@pytest.mark.asyncio
async def test_file_tools_wait_for_shell_scope_but_independent_files_can_overlap(tmp_path):
    registry=SimpleNamespace(workspace_dir=tmp_path,access_roots=[tmp_path],_effective_audit_context=lambda:{})
    entered=asyncio.Event()
    async def write():
        async with tool_resource(registry,'file_write',{'path':'one.txt'}):entered.set()
    async with tool_resource(registry,'shell',{'command':'arbitrary mutation'}):
        task=asyncio.create_task(write())
        await asyncio.sleep(.08)
        assert not entered.is_set()
    await asyncio.wait_for(task,1)
    async with tool_resource(registry,'file_write',{'path':'one.txt'}):
        async with tool_resource(registry,'file_write',{'path':'two.txt'}):
            pass
        async with tool_resource(registry,'file_read',{'path':'one.txt'}):
            pass


@pytest.mark.asyncio
async def test_parent_workspace_mutation_blocks_a_nested_worker_without_shared_root_configuration(tmp_path):
    nested = tmp_path/'nested'
    nested.mkdir()
    outer = SimpleNamespace(workspace_dir=tmp_path,access_roots=[],_effective_audit_context=lambda:{'global_config':SimpleNamespace(bridge_home=tmp_path)})
    inner = SimpleNamespace(workspace_dir=nested,access_roots=[],_effective_audit_context=outer._effective_audit_context)
    entered = asyncio.Event()
    async def write():
        async with tool_resource(inner,'file_write',{'path':'same.txt'}):
            entered.set()
    async with tool_resource(outer,'shell',{}):
        task = asyncio.create_task(write())
        await asyncio.sleep(.08)
        assert not entered.is_set()
    await asyncio.wait_for(task,1)


def test_os_resource_lease_excludes_a_separate_worker_process(tmp_path):
    ready=tmp_path/'ready';entered=tmp_path/'entered'
    code=('import sys,asyncio;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
          'from orchestrator.execution_resources import leases;'
          'exec("async def main():\\n Path(sys.argv[3]).write_text(\'ready\')\\n async with leases(sys.argv[2],[(\'scope\',1,False)],timeout=2):\\n  Path(sys.argv[4]).write_text(\'entered\')\\nasyncio.run(main())".replace("\\\\n","\\n"))')
    held=FileLease(tmp_path,'scope:0');assert held.try_acquire()
    process=subprocess.Popen([sys.executable,'-I','-B','-c',code,str(Path(__file__).resolve().parents[1]),str(tmp_path),str(ready),str(entered)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        import time
        deadline=time.monotonic()+2
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
        assert ready.exists()
        time.sleep(.1)
        assert not entered.exists()
    finally:
        held.close()
        stdout,stderr=process.communicate(timeout=5)
    assert process.returncode==0,stderr.decode(errors='replace')
    assert entered.exists()


@pytest.mark.parametrize('value',[True,False,'2',2.5,0,-1])
def test_execution_limit_rejects_unqualified_configuration_values(value):
    with pytest.raises(ValueError):limit(value,'budget',8)


def test_execution_capacity_uses_the_selected_positive_values_without_fixed_small_caps():
    settings={'execution_limits':{'instance_sessions':96,'engines':{'example':80},'queue_wait_seconds':30}}
    assert execution_configuration(settings,{'max_concurrent_sessions':12}) == settings['execution_limits']
    assert limit(96,'instance_sessions',8)==96


@pytest.mark.parametrize('value',[True,False,'30',0,-1,float('nan'),float('inf'),10**400])
def test_execution_queue_timeout_rejects_invalid_selected_values(value):
    with pytest.raises(ValueError):
        execution_configuration({'execution_limits':{'queue_wait_seconds':value}},{})


@pytest.mark.asyncio
async def test_unlimited_capacity_wait_does_not_expire_and_remains_cancellable(tmp_path,monkeypatch):
    import orchestrator.execution_resources as resources
    clock=SimpleNamespace(now=0.0)
    monkeypatch.setattr(resources,'time',SimpleNamespace(monotonic=lambda:clock.now))
    held=FileLease(tmp_path,'instance:0')
    assert held.try_acquire()
    reasons=[]
    async def wait():
        async with leases(tmp_path,[('instance',1,False)],on_wait=reasons.append,timeout=None):
            pytest.fail('occupied slot admitted another owner')
    task=asyncio.create_task(wait())
    try:
        while not reasons:
            await asyncio.sleep(.01)
        clock.now=601.0
        await asyncio.sleep(.08)
        assert not task.done(), 'no implicit ten-minute deadline'
        task.cancel()
        assert isinstance((await asyncio.gather(task,return_exceptions=True))[0],asyncio.CancelledError)
    finally:
        held.close()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    async with leases(tmp_path,[('instance',1,False)],timeout=None):
        pass


@pytest.mark.asyncio
async def test_execution_budget_uses_owner_selected_deadline_and_clears_wait_reason(tmp_path,monkeypatch):
    from unittest.mock import AsyncMock
    from orchestrator import runtime_lifecycle
    from orchestrator.execution_resources import execution_budget, instance_state
    monkeypatch.setattr(runtime_lifecycle,'_publish_worker_metadata',AsyncMock())
    runtime=SimpleNamespace(global_config=SimpleNamespace(project_root=tmp_path),
                            config=SimpleNamespace(project_root=tmp_path))
    config=SimpleNamespace(extra={},active_backend='example')
    frozen={'config':config,'settings':{'_agents_json_global':{'execution_limits':{
        'instance_sessions':1,'engines':{'example':1},'queue_wait_seconds':.03}}}}
    item=SimpleNamespace(request_id='selected-deadline')
    held=FileLease(instance_state(runtime),'instance-sessions:0')
    assert held.try_acquire()
    try:
        with pytest.raises(TimeoutError,match='execution_resource_wait_timeout'):
            async with execution_budget(runtime,item,frozen):
                pytest.fail('occupied capacity entered')
        assert runtime._execution_queue_reasons == {}
    finally:
        held.close()


@pytest.mark.asyncio
async def test_execution_budget_default_wait_survives_ten_minutes_and_cancels(tmp_path,monkeypatch):
    from unittest.mock import AsyncMock
    import orchestrator.execution_resources as resources
    from orchestrator import runtime_lifecycle
    clock=SimpleNamespace(now=0.0)
    monkeypatch.setattr(resources,'time',SimpleNamespace(monotonic=lambda:clock.now))
    monkeypatch.setattr(runtime_lifecycle,'_publish_worker_metadata',AsyncMock())
    runtime=SimpleNamespace(global_config=SimpleNamespace(project_root=tmp_path),
                            config=SimpleNamespace(project_root=tmp_path))
    frozen={'config':SimpleNamespace(extra={},active_backend='example'),
            'settings':{'_agents_json_global':{'execution_limits':{'instance_sessions':1}}}}
    item=SimpleNamespace(request_id='default-wait')
    held=FileLease(resources.instance_state(runtime),'instance-sessions:0')
    assert held.try_acquire()
    async def wait():
        async with resources.execution_budget(runtime,item,frozen):
            pytest.fail('occupied capacity entered')
    task=asyncio.create_task(wait())
    try:
        for _ in range(100):
            if getattr(runtime,'_execution_queue_reasons',{}).get(item.request_id):
                break
            await asyncio.sleep(.01)
        assert runtime._execution_queue_reasons[item.request_id]=='instance_capacity'
        clock.now=601.0
        await asyncio.sleep(.08)
        assert not task.done()
        task.cancel()
        assert isinstance((await asyncio.gather(task,return_exceptions=True))[0],asyncio.CancelledError)
        assert runtime._execution_queue_reasons == {}
    finally:
        held.close()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
# HASHI-Fingerprint: hfp_12b4015e6db54ed88682cc8a203b5e3e
