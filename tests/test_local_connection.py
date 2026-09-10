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
        assert requests and requests[0][0]['Authorization']=='Bearer scoped-credential'
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


def test_connection_default_permissions_do_not_inherit_global_wildcard(tmp_path):
    from orchestrator.config import FlexibleAgentConfig,GlobalConfig
    from orchestrator.flexible_backend_manager import FlexibleBackendManager
    (tmp_path/'agents.json').write_text('{"global":{"default_tools":{"allowed":["*"]}},"agents":[]}')
    active,backends=backend_configuration('codex-cli','gpt-5.4')
    cfg=FlexibleAgentConfig('hashiko',tmp_path,tmp_path/'agent.md','',backends,active,extra={},project_root=tmp_path)
    manager=FlexibleBackendManager(cfg,GlobalConfig(0,project_root=tmp_path,bridge_home=tmp_path),{})
    assert manager._resolve_tools_config(backends[0]) is None


async def test_optional_telegram_rejects_missing_owner_before_any_network(tmp_path):
    from onboarding.connection import connect_telegram
    with pytest.raises(ConnectionError,match='AUTHORIZED_USER_ID_REQUIRED'):
        await connect_telegram(tmp_path,'never-transmitted','',confirmed=True)
    assert not list(tmp_path.iterdir())
