from __future__ import annotations
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
from orchestrator.execution_resources import FileLease, leases, limit, tool_resource


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
        while not reasons:await asyncio.sleep(.01)
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
    process=subprocess.Popen([sys.executable,'-I','-c',code,str(Path(__file__).resolve().parents[1]),str(tmp_path),str(ready),str(entered)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
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


@pytest.mark.parametrize('value',[True,False,'2',2.5,0,65])
def test_execution_limit_rejects_unqualified_configuration_values(value):
    with pytest.raises(ValueError):limit(value,'budget',8)
