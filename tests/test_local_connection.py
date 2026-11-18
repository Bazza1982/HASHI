from __future__ import annotations

import json
from pathlib import Path

import pytest
from onboarding.connection import ConnectionError, backend_configuration, save, validate
from orchestrator.her_v2.config import HERv2Config
from tui.connection import ConnectionApp
from textual.widgets import Input,Checkbox,Select,Button


def test_connection_save_preserves_identity_history_and_existing_credentials(tmp_path):
    workspace=tmp_path/'workspaces'/'keep';workspace.mkdir(parents=True)
    (workspace/'transcript.jsonl').write_text('keep history\n')
    config={'global':{'instance_id':'KEEP','workbench_port':18123},'agents':[
        {'name':'keep','workspace_dir':'workspaces/keep','active_backend':'codex-cli'},
        {'name':'hashiko','workspace_dir':'workspaces/existing','display_name':'Existing',
         'telegram_token_key':'keep-bot','allowed_backends':[]}]}
    (tmp_path/'agents.json').write_text(json.dumps(config))
    (tmp_path/'secrets.json').write_text('{"keep-bot":"original-token","unrelated":"original"}')
    result=save(tmp_path,'openrouter-api','openai/gpt-4o','new-selected-key',
        verified={'engine':'openrouter-api','model':'openai/gpt-4o','verified':True})
    saved=json.loads((tmp_path/'agents.json').read_text())
    assert saved['global']==config['global'] and saved['agents'][0]==config['agents'][0]
    hashiko=saved['agents'][1]
    assert hashiko['active_backend']=='her-v2' and hashiko['telegram_token_key']=='keep-bot'
    assert hashiko['workspace_dir']=='workspaces/existing' and hashiko['display_name']=='Existing'
    her=next(b['her_v2'] for b in hashiko['allowed_backends'] if b['engine']=='her-v2')
    assert {p.engine for p in HERv2Config.from_mapping(her).profiles.values()}=={'openrouter-api'}
    assert (workspace/'transcript.jsonl').read_text()=='keep history\n'
    secrets=json.loads((tmp_path/'secrets.json').read_text())
    assert secrets['keep-bot']=='original-token' and secrets['unrelated']=='original'
    assert 'new-selected-key' not in (tmp_path/'agents.json').read_text()
    assert result['runtime_ready'] is False
    before=(tmp_path/'secrets.json').read_bytes()
    with pytest.raises(ConnectionError,match='REPLACE_CONFIRMATION_REQUIRED'):
        save(tmp_path,'openrouter-api','openai/gpt-4o','replacement',
             verified={'engine':'openrouter-api','model':'openai/gpt-4o','verified':True})
    assert (tmp_path/'secrets.json').read_bytes()==before


def test_connection_save_new_instance_defaults_tools_open(tmp_path):
    result = save(
        tmp_path,
        'codex-cli',
        'gpt-5.4',
        '',
        verified={'engine': 'codex-cli', 'model': 'gpt-5.4', 'verified': True},
    )

    saved = json.loads((tmp_path/'agents.json').read_text())
    assert saved['global']['default_tools'] == {'allowed': ['*']}
    assert all(
        'tools' not in backend
        for backend in saved['agents'][0]['allowed_backends']
    )
    assert result['runtime_ready'] is False


def test_private_connection_save_protects_empty_temporary_before_secret_write(tmp_path, monkeypatch):
    from onboarding.onboarding_main import _atomic_write_json
    from tools import private_files
    original = private_files.protect_private_file
    observed = []
    def protect(path):
        observed.append(path.read_bytes())
        original(path)
    monkeypatch.setattr(private_files, 'protect_private_file', protect)
    path = tmp_path / 'secrets.json'
    _atomic_write_json(path, {'test_key': 'synthetic-private-value'}, private=True)
    assert observed == [b''], 'secret bytes must never precede protection of the temporary file'
    assert json.loads(path.read_text())['test_key'] == 'synthetic-private-value'
    assert list(tmp_path.iterdir()) == [path]


async def test_connection_consent_and_secret_control_never_submit_chat(tmp_path):
    with pytest.raises(ConnectionError,match='CONFIRMATION_REQUIRED'):
        await validate(tmp_path,'openrouter-api','openai/gpt-4o','secret',confirmed=False)
    app=ConnectionApp(tmp_path)
    async with app.run_test(size=(65,30)) as pilot:
        await pilot.pause()
        screen=app.screen
        screen.query_one('#connection-backend',Select).value='openrouter-api'
        await pilot.pause()
        key=screen.query_one('#connection-key',Input)
        assert key.password
        key.value='never-in-chat'
        screen.connect()
        await pilot.pause(.3)
        assert key.value==''
        assert not (tmp_path/'agents.json').exists()
        assert not (tmp_path/'secrets.json').exists()


async def test_her_connection_uses_actual_http_adapter_and_keeps_key_out_of_prompt(tmp_path, monkeypatch):
    from aiohttp import web
    from onboarding import connection
    from orchestrator.config import GlobalConfig
    requests=[]
    async def reply(request):
        body=await request.json()
        requests.append((dict(request.headers),body))
        payload={'id':'local-proof','choices':[{'index':0,'message':{'role':'assistant','content':'HASHI_CONNECTED'},'delta':{'content':'HASHI_CONNECTED'},'finish_reason':'stop'}], 'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
        if body.get('stream'):
            return web.Response(text='data: '+json.dumps(payload)+'\n\ndata: [DONE]\n\n',content_type='text/event-stream')
        return web.json_response(payload)
    app=web.Application();app.router.add_post('/chat/completions',reply)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    def globals_(*a,**kw):
        return GlobalConfig(*a,**kw,openrouter_url=f'http://127.0.0.1:{port}/chat/completions')
    monkeypatch.setattr(connection,'GlobalConfig',globals_)
    models=next(r['models'] for r in connection.choices(tmp_path) if r['engine']=='openrouter-api')
    try:
        result=await connection.validate(tmp_path,'openrouter-api',models[0],'scoped-credential',confirmed=True)
        assert result['verified']
        assert [body.get('model') for _, body in requests] == [models[0]]
        custom = await connection.validate(
            tmp_path, 'openrouter-api', 'example/future-model', 'scoped-credential', confirmed=True
        )
        assert custom['verified'] and custom['model'] == 'example/future-model'
        assert [body.get('model') for _, body in requests] == [models[0], 'example/future-model']
        assert requests and requests[0][0]['Authorization']=='Bearer scoped-credential'
        assert all(not body.get('tools') for _, body in requests)
        assert 'scoped-credential' not in json.dumps([body for _,body in requests])
        assert not (tmp_path/'secrets.json').exists()
    finally:
        await runner.cleanup()


def test_verified_connection_revision_supersedes_old_backend_state_once(tmp_path):
    from orchestrator.config import FlexibleAgentConfig,GlobalConfig
    from orchestrator.flexible_backend_manager import FlexibleBackendManager
    active,backends=backend_configuration('codex-cli','gpt-5.4')
    backends.append({'engine':'claude-cli','model':'claude-sonnet-4-6'})
    cfg=FlexibleAgentConfig('hashiko',tmp_path,tmp_path/'agent.md','',backends,active,
        extra={'connection_revision':'verified-1'},project_root=tmp_path)
    (tmp_path/'state.json').write_text(json.dumps({'active_backend':'claude-cli','active_model':'old-model','custom_unrelated':{'keep':True}}))
    manager=FlexibleBackendManager(cfg,GlobalConfig(0,project_root=tmp_path,bridge_home=tmp_path),{})
    assert manager.config.active_backend=='codex-cli'
    state=json.loads((tmp_path/'state.json').read_text())
    assert state['connection_revision']=='verified-1'
    assert state['custom_unrelated']=={'keep':True}
    state['active_backend']='claude-cli'
    (tmp_path/'state.json').write_text(json.dumps(state))
    manager=FlexibleBackendManager(cfg,GlobalConfig(0,project_root=tmp_path,bridge_home=tmp_path),{})
    assert manager.config.active_backend=='claude-cli'


def test_connection_default_permissions_inherit_global_wildcard(tmp_path):
    from orchestrator.config import FlexibleAgentConfig,GlobalConfig
    from orchestrator.flexible_backend_manager import FlexibleBackendManager
    (tmp_path/'agents.json').write_text('{"global":{"default_tools":{"allowed":["*"]}},"agents":[]}')
    active,backends=backend_configuration('codex-cli','gpt-5.4')
    cfg=FlexibleAgentConfig('hashiko',tmp_path,tmp_path/'agent.md','',backends,active,extra={},project_root=tmp_path)
    manager=FlexibleBackendManager(cfg,GlobalConfig(0,project_root=tmp_path,bridge_home=tmp_path,
        config_path=tmp_path/'agents.json'),{})
    assert manager._resolve_tools_config(backends[0]) == {'allowed': ['*']}


async def test_optional_telegram_rejects_missing_owner_before_any_network(tmp_path):
    from onboarding.connection import connect_telegram
    with pytest.raises(ConnectionError,match='AUTHORIZED_USER_ID_REQUIRED'):
        await connect_telegram(tmp_path,'never-transmitted','',confirmed=True)
    assert not list(tmp_path.iterdir())


async def test_connection_adoption_requires_authenticated_durable_single_agent_receipt(tmp_path):
    import asyncio
    from types import MethodType, SimpleNamespace
    from aiohttp import web, ClientSession
    from onboarding.connection import adopt_if_running
    from orchestrator.admin_local_testing import execute_local_command
    from orchestrator.config import GlobalConfig
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.reboot_manager import RebootManager
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle
    from orchestrator.workbench_api import WorkbenchApiServer

    handles = []
    kernel = SimpleNamespace(paths=SimpleNamespace(bridge_home=tmp_path),
        runtimes=handles, shutdown_event=asyncio.Event(),
        configured_agent_names=lambda: ['hashiko', 'keep'],
        _runtime_map=lambda: {h.name: h for h in handles}, _restart_request=None)
    def make_handle(name):
        workspace = tmp_path / 'workspaces' / name
        workspace.mkdir(parents=True)
        worker = SimpleNamespace(
            name=name,
            orchestrator=kernel,
            workspace_dir=workspace,
            global_config=SimpleNamespace(
                authorized_id=42,
                bridge_home=tmp_path,
                project_root=tmp_path,
                instance_id='CONNECTION-TEST',
                ui_language='en',
            ),
            _authorized_telegram_ids={42},
            _is_command_allowed=lambda command: command == 'reboot',
        )
        worker._is_authorized_user = MethodType(FlexibleAgentRuntime._is_authorized_user, worker)

        async def reply_text(_self, update, text, **kwargs):
            return await update.message.reply_text(text, **kwargs)

        worker._reply_text = MethodType(reply_text, worker)
        worker.cmd_reboot = MethodType(FlexibleAgentRuntime.cmd_reboot, worker)

        async def call(method, params=None, **_kwargs):
            params = dict(params or {})
            if method == 'runtime.slash':
                return await execute_local_command(
                    worker,
                    str(params.get('text') or ''),
                    source_channel=str(params.get('source_channel') or 'api_chat'),
                    chat_id=params.get('chat_id'),
                    session_metadata=params.get('session_metadata'),
                )
            if method == 'worker.metadata':
                return {'is_generating': False, 'queue_depth': 0}
            raise AssertionError(f'unexpected Worker call: {method}')

        client = SimpleNamespace(agent_name=name, call=call)
        return AgentRuntimeHandle(kernel, client, {
            'name':name, 'display_name':name,
            'worker_phase':'ACTIVE', 'worker_accepting':True,
        })

    for name in ('hashiko','keep'):
        handles.append(make_handle(name))
    manager = RebootManager(kernel, None)
    kernel.reboot_manager=manager
    async def request_reboot(**request):
        return manager.submit(request)
    kernel.request_reboot = request_reboot
    cfg = {'global': {'instance_id': 'CONNECTION-TEST'}, 'agents':[
        {'name':'hashiko','connection_revision':'saved-revision'}, {'name':'keep'}]}
    (tmp_path/'agents.json').write_text(json.dumps(cfg))
    (tmp_path/'secrets.json').write_text('{"workbench_admin_token":"local-owner"}')
    server = WorkbenchApiServer(tmp_path/'agents.json',
        GlobalConfig(0,project_root=tmp_path,bridge_home=tmp_path,instance_id='CONNECTION-TEST'),
        runtimes=handles, secrets={'workbench_admin_token':'local-owner'}, orchestrator=kernel)
    runner=web.AppRunner(server.app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    base='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
    state=tmp_path/'state';state.mkdir(exist_ok=True)
    (state/'service_endpoints.json').write_text(json.dumps({'services':{'workbench':{'base_url':base}}}))
    try:
        async with ClientSession() as client:
            async with client.post(base+'/api/admin/reboot-agent',json={'agent':'hashiko','request_key':'unauthorized'}) as response:
                assert response.status==403
        assert manager.receipts.records()==[]
        manager.active_operation='existing-reboot'
        async with ClientSession() as client:
            async with client.post(base+'/api/admin/reboot-agent',
                    headers={'X-Workbench-Token':'local-owner'},
                    json={'agent':'hashiko','request_key':'busy'}) as response:
                assert response.status==409
                assert (await response.json())['error']=='AGENT_BUSY'
        assert manager.receipts.records()==[]
        manager.active_operation=None
        admission=asyncio.create_task(adopt_if_running(tmp_path))
        for _ in range(100):
            if manager.receipts.records():break
            await asyncio.sleep(.01)
        assert not admission.done(), 'admission alone must not report completed adoption'
        record=manager.receipts.records()[0]
        manager.receipts.update(record['id'],status='succeeded')
        first=await admission
        second=await adopt_if_running(tmp_path)
        assert first['reload_requested'] and first['adopted']
        assert first['operation_id']==second['operation_id']
        receipts=manager.receipts.records()
        assert len(receipts)==1 and receipts[0]['targets']==['hashiko']
        assert receipts[0]['origin']['surface']=='workbench'
        assert kernel._restart_request['targets']==['hashiko']
        cfg['global']['instance_id']='WRONG-INSTANCE'
        (tmp_path/'agents.json').write_text(json.dumps(cfg))
        with pytest.raises(ConnectionError,match='LOCAL_ENDPOINT_MISMATCH'):
            await adopt_if_running(tmp_path)
        assert len(manager.receipts.records())==1
    finally:
        await runner.cleanup()



async def test_save_unverified_tui_never_probes_or_reloads(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    import tui.connection as connection_ui

    probe = AsyncMock(side_effect=AssertionError("probe was not selected"))
    adopt = AsyncMock(side_effect=AssertionError("unverified save must not reload"))
    monkeypatch.setattr(connection_ui, "validate", probe)
    monkeypatch.setattr(connection_ui, "adopt_if_running", adopt)
    app = ConnectionApp(tmp_path)
    async with app.run_test(size=(100, 45)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#connection-backend", Select).value = "openrouter-api"
        await pilot.pause()
        custom = screen.query_one("#connection-custom-model", Input)
        assert not custom.disabled
        custom.value = "example/future-model"
        secret = screen.query_one("#connection-key", Input)
        assert secret.password
        secret.value = "synthetic-key"
        await pilot.click("#connection-save-unverified")
        await pilot.pause(0.4)
    probe.assert_not_awaited()
    adopt.assert_not_awaited()
    config = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
    agent = next(a for a in config["agents"] if a["name"] == "hashiko")
    assert agent["connection_verification"] == {
        "engine": "openrouter-api", "model": "example/future-model", "verified": False,
    }
    assert "synthetic-key" not in json.dumps(config)
    assert json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))[
        "hashiko.connection.openrouter-api"
    ] == "synthetic-key"


def test_save_unverified_rejects_stale_snapshot_and_preserves_credentials(tmp_path):
    from onboarding.connection import connection_revisions

    config = {"global": {"instance_id": "TEST"}, "agents": []}
    (tmp_path / "agents.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "secrets.json").write_text('{"unrelated":"keep"}', encoding="utf-8")
    revisions = connection_revisions(tmp_path)
    config["global"]["new_value"] = "other writer"
    (tmp_path / "agents.json").write_text(json.dumps(config), encoding="utf-8")
    before = (tmp_path / "secrets.json").read_bytes()
    with pytest.raises(ConnectionError, match="CONFIG_CHANGED"):
        save(tmp_path, "openrouter-api", "example/future-model", "new-key",
             allow_unverified=True, expected_revisions=revisions)
    assert (tmp_path / "secrets.json").read_bytes() == before
    assert json.loads((tmp_path / "agents.json").read_text())["global"]["new_value"] == "other writer"


def test_connection_conflict_rolls_back_only_its_own_secret_write(tmp_path, monkeypatch):
    from onboarding import connection

    (tmp_path / "agents.json").write_text('{"global":{},"agents":[]}', encoding="utf-8")
    (tmp_path / "secrets.json").write_text('{"unrelated":"keep"}', encoding="utf-8")
    original_write = connection.write_config_json
    def concurrent_change(path, payload, **kwargs):
        if path.name == "agents.json":
            path.write_text('{"global":{"external":true},"agents":[]}', encoding="utf-8")
        return original_write(path, payload, **kwargs)
    monkeypatch.setattr(connection, "write_config_json", concurrent_change)
    with pytest.raises(ConnectionError, match="CONFIG_CHANGED"):
        save(tmp_path, "openrouter-api", "example/future-model", "new-key", allow_unverified=True)
    assert json.loads((tmp_path / "secrets.json").read_text()) == {"unrelated": "keep"}
    assert json.loads((tmp_path / "agents.json").read_text())["global"]["external"] is True


def test_connection_conflict_never_overwrites_a_newer_secret(tmp_path, monkeypatch):
    from onboarding import connection

    (tmp_path / "agents.json").write_text('{"global":{},"agents":[]}', encoding="utf-8")
    (tmp_path / "secrets.json").write_text('{"unrelated":"keep"}', encoding="utf-8")
    original_write = connection.write_config_json
    def concurrent_change(path, payload, **kwargs):
        if path.name == "agents.json":
            current = connection.read_config_json(tmp_path / "secrets.json")
            current["other-writer"] = "newer"
            original_write(tmp_path / "secrets.json", current)
            path.write_text('{"global":{"external":true},"agents":[]}', encoding="utf-8")
        return original_write(path, payload, **kwargs)
    monkeypatch.setattr(connection, "write_config_json", concurrent_change)
    with pytest.raises(ConnectionError, match="SAVE_PARTIAL"):
        save(tmp_path, "openrouter-api", "example/future-model", "new-key", allow_unverified=True)
    assert json.loads((tmp_path / "secrets.json").read_text())["other-writer"] == "newer"


def test_unverified_connection_cannot_forge_a_mismatched_verification(tmp_path):
    with pytest.raises(ConnectionError, match="VALIDATION_MISMATCH"):
        save(tmp_path, "openrouter-api", "example/new", "",
             verified={"engine": "openrouter-api", "model": "example/old", "verified": True},
             allow_unverified=True)
    assert not (tmp_path / "agents.json").exists()


async def test_confirmed_probe_does_not_retry_after_provider_failure(tmp_path, monkeypatch):
    from aiohttp import web
    from onboarding import connection
    from orchestrator.config import GlobalConfig

    requests = []
    async def unavailable(request):
        requests.append(await request.json())
        return web.json_response({'error': {'message': 'unavailable'}}, status=503)

    app = web.Application()
    app.router.add_post('/chat/completions', unavailable)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(connection, 'GlobalConfig', lambda *a, **kw: GlobalConfig(
        *a, **kw, openrouter_url=f'http://127.0.0.1:{port}/chat/completions',
    ))
    try:
        with pytest.raises(ConnectionError):
            await connection.validate(tmp_path, 'openrouter-api', 'example/future-model',
                                      'synthetic-key', confirmed=True)
        assert len(requests) == 1
        assert requests[0]['model'] == 'example/future-model'
    finally:
        await runner.cleanup()


async def test_connection_tui_reports_ambiguous_save_without_inviting_blind_retry(
    tmp_path, monkeypatch
):
    import tui.connection as connection_ui
    from textual.widgets import Static

    for code, expected in [
        ('SAVE_PARTIAL', 'state is uncertain or partial'),
        ('SAVE_DURABILITY_UNKNOWN', 'state is uncertain or partial'),
        ('SAVED_WORKSPACE_PENDING', 'workspace setup is incomplete'),
        ('CONFIG_CHANGED', 'Settings changed elsewhere'),
    ]:
        def fail_save(*args, **kwargs):
            raise ConnectionError(code)
        monkeypatch.setattr(connection_ui, 'save', fail_save)
        app = ConnectionApp(tmp_path)
        async with app.run_test(size=(100, 45)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.query_one('#connection-backend', Select).value = 'openrouter-api'
            await pilot.pause()
            screen.query_one('#connection-key', Input).value = 'synthetic-key'
            await pilot.click('#connection-save-unverified')
            await pilot.pause(0.3)
            display = str(screen.query_one('#connection-result', Static).render())
            assert code in display and expected in display
            assert 'synthetic-key' not in display


async def test_herv3_configured_retry_budget_counts_real_http_attempts(tmp_path):
    from aiohttp import web
    from types import SimpleNamespace
    from orchestrator.config import FlexibleAgentConfig, GlobalConfig
    from orchestrator.flexible_backend_manager import FlexibleBackendManager
    from orchestrator.pcm import atomic_write_pcm, render_pcm_document

    requests = []
    failing = 0
    async def respond(request):
        nonlocal failing
        body = await request.json()
        requests.append(body)
        if failing:
            failing -= 1
            return web.json_response({'error': {'message': 'unavailable'}}, status=503)
        payload = {
            'id': 'local-retry', 'choices': [{
                'index': 0, 'message': {'role': 'assistant', 'content': 'recovered'},
                'delta': {'content': 'recovered'}, 'finish_reason': 'stop',
            }], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        }
        if body.get('stream'):
            return web.Response(text='data: ' + json.dumps(payload) + '\n\n'
                                + 'data: [DONE]\n\n', content_type='text/event-stream')
        return web.json_response(payload)

    app = web.Application()
    app.router.add_post('/chat/completions', respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        for selected, failures, succeeds in [(0, 1, False), (2, 2, True), (4, 5, False)]:
            requests.clear()
            failing = failures
            root = tmp_path / f'case-{selected}'
            root.mkdir()
            active, backends = backend_configuration('openrouter-api', 'openai/gpt-4o')
            for backend in backends:
                backend['tools'] = {'enabled': False}
                if backend['engine'] == 'her-v2':
                    backend['her_v2']['provider_recovery'] = {
                        'max_provider_retries': selected, 'base_delay_s': 0,
                        'max_delay_s': 0, 'capacity_delays_s': [],
                    }
            cfg = FlexibleAgentConfig(
                name='hashiko', workspace_dir=root, system_md=root/'agent.md',
                telegram_token_key='', allowed_backends=backends, active_backend=active,
                access_scope='workspace', project_root=root,
                extra={'ephemeral_session': True, 'habit_learning_eligible': False,
                       'habit_meditation_enabled': False},
            )
            atomic_write_pcm(cfg.agent_md, render_pcm_document(persona='Test', system='Reply briefly.'))
            global_cfg = GlobalConfig(
                authorized_id=0, project_root=root, bridge_home=root, base_logs_dir=root/'logs',
                openrouter_url=f'http://127.0.0.1:{port}/chat/completions',
            )
            manager = FlexibleBackendManager(
                cfg, global_cfg, {'hashiko.connection.openrouter-api': 'synthetic-key'},
            )
            manager.runtime = SimpleNamespace(backend_manager=manager)
            try:
                assert await manager.initialize_active_backend()
                result = await manager.generate_response('Say hello.', f'retry-{selected}', silent=True)
            finally:
                await manager.shutdown()
            assert result.is_success is succeeds, (selected, result.error)
            assert len(requests) == selected + 1
            assert all(body['model'] == 'openai/gpt-4o' for body in requests)
    finally:
        await runner.cleanup()
