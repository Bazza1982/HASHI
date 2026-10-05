from __future__ import annotations
import json
from types import SimpleNamespace
import pytest
from aiohttp.test_utils import TestClient, TestServer
from orchestrator.session_store import SessionStore, SessionNotFound
from orchestrator.run_questions import RunQuestions, QuestionError, issue_tool_token, verify_tool_token
from orchestrator.workbench_api import WorkbenchApiServer
from tools.registry import ToolRegistry
from tools.gateway.context import GatewayContext

SECRET='question-test-secret'

def active(store, req='req-q1', agent='lily', owner='user:7'):
    s=store.ensure_default_session(owner_id=owner,agent_id=agent)
    r=store.accept_run(session_id=s['session_id'],owner_id=owner,agent_id=agent,
                       request_id=req,text='controlled question test',source='workbench',idempotency_key=req)
    store.mark_request_running(r.request_id,worker_id='qa-worker')
    return s,r

@pytest.fixture
def service(tmp_path):
    store=SessionStore(tmp_path/'sessions.sqlite3',instance_id='HASHI1')
    return RunQuestions(store)

def spec(key='first'):
    return dict(question='Choose a colour',options=[{'id':'blue','label':'Blue'},{'id':'red','label':'Red'}],
                allow_free_text=True,purpose='preference',idempotency_key=key)

def test_durable_out_of_order_answers_and_single_effect(service):
    session,run=active(service.store)
    one=service.create(request_id=run.request_id,agent_id='lily',payload=spec())
    two=service.create(request_id=run.request_id,agent_id='lily',payload=spec('second'))
    assert service.create(request_id=run.request_id,agent_id='lily',payload=spec())['question_id']==one['question_id']
    restarted=RunQuestions(SessionStore(service.store.db_path,instance_id='HASHI1'))
    for q,choice in [(two,'red'),(one,'blue')]:
        answer=dict(idempotency_key='answer-'+choice,option_id=choice)
        saved=restarted.answer(session_id=session['session_id'],owner_id='user:7',question_id=q['question_id'],payload=answer)
        assert saved['state']=='answered' and saved['answer']['grants_permissions'] is False
        assert restarted.answer(session_id=session['session_id'],owner_id='user:7',question_id=q['question_id'],payload=answer)==saved
        seen=restarted.get_answer(request_id=run.request_id,agent_id='lily',question_id=q['question_id'])
        assert seen['state']=='consumed' and seen['answer']['option_id']==choice
    with service.store._connection() as c:
        assert c.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==1
        assert c.execute("SELECT COUNT(*) FROM run_events WHERE kind='run.question.answered'").fetchone()[0]==2
    assert service.store.get_run(run.run_id)['state']=='running'

def test_late_wrong_scope_and_fabricated_answer_rejected(service):
    s,r=active(service.store)
    q=service.create(request_id=r.request_id,agent_id='lily',payload=spec())
    other,_=active(service.store,'req-other','other','user:8')
    with pytest.raises((QuestionError,SessionNotFound)):
        service.answer(session_id=other['session_id'],owner_id='user:8',question_id=q['question_id'],payload={'idempotency_key':'bad','option_id':'blue'})
    with pytest.raises(QuestionError,match='answer_invalid'):
        service.answer(session_id=s['session_id'],owner_id='user:7',question_id=q['question_id'],payload={'idempotency_key':'bad','option_id':'invented'})
    service.store.finish_request(r.request_id,success=False,error_text='controlled stop')
    assert service.list(session_id=s['session_id'],owner_id='user:7')[0]['state']=='cancelled'
    with pytest.raises(QuestionError,match='cancelled'):
        service.answer(session_id=s['session_id'],owner_id='user:7',question_id=q['question_id'],payload={'idempotency_key':'late','option_id':'blue'})

def test_expiry_does_not_approve_or_end_run(service):
    clock=[1000.0]; service.clock=lambda:clock[0]
    s,r=active(service.store)
    q=service.create(request_id=r.request_id,agent_id='lily',payload={**spec(),'expires_seconds':30})
    clock[0]+=31
    assert service.list(session_id=s['session_id'],owner_id='user:7')[0]['state']=='expired'
    assert service.store.get_run(r.run_id)['state']=='running'
    assert service.get_answer(request_id=r.request_id,agent_id='lily',question_id=q['question_id'])['answer'] is None

def test_token_is_instance_and_run_scoped_and_expires():
    token=issue_tool_token(SECRET,instance_id='HASHI1',agent_id='lily',request_id='req-a',now=100)
    assert verify_tool_token(SECRET,token,instance_id='HASHI1',now=101)['request_id']=='req-a'
    for instance,now in [('HASHI4',101),('HASHI1',3700)]:
        with pytest.raises(QuestionError):verify_tool_token(SECRET,token,instance_id=instance,now=now)
    with pytest.raises(QuestionError):verify_tool_token(SECRET,token+'x',instance_id='HASHI1',now=101)

@pytest.mark.asyncio
@pytest.mark.parametrize('engine',['her-v3','codex-cli'])
async def test_both_engine_tool_paths_continue_independent_work_and_receive_answer(tmp_path,engine):
    cfg=SimpleNamespace(bridge_home=tmp_path,project_root=tmp_path,instance_id='HASHI1',authorized_id=7,
                        workbench_port=18800,api_gateway_port=18801,deployment_profile='personal')
    p=tmp_path/'agents.json'; p.write_text(json.dumps({'global':{},'agents':[{'name':'lily'}]}))
    server=WorkbenchApiServer(p,cfg,secrets={'workbench_admin_token':SECRET},reconcile_session_runs=False)
    session,run=active(server.session_store)
    client=TestClient(TestServer(server.app)); await client.start_server()
    try:
        base=str(client.make_url('')).rstrip('/')
        cfg.workbench_port=client.server.port
        reg=ToolRegistry(allowed_tools=['ask_user','get_user_answer','file_list'],workspace_dir=tmp_path,
            access_root=tmp_path,secrets={'workbench_admin_token':SECRET},
            audit_context={'agent_name':'lily','request_id':run.request_id,'instance_id':'HASHI1',
                           'global_config':cfg,'workbench_api_base_url':base})
        if engine=='codex-cli':
            gateway=GatewayContext.from_registry(reg,backend='codex-cli',workbench_api_base_url=base)
            assert SECRET not in json.dumps(gateway.secrets)
            reg=gateway.build_registry()
        (tmp_path/'independent.txt').write_text('unrelated work')
        asked=await reg.execute('ask_user',spec(),'call-q1')
        assert not asked.is_error,asked.output
        q=json.loads(asked.output)['question']
        assert q['state']=='pending'
        work=await reg.execute('file_list',{'path':str(tmp_path)},'call-independent')
        assert not work.is_error and 'independent.txt' in work.output
        assert server.session_store.get_run(run.run_id)['state']=='running'
        url='/api/v1/sessions/'+session['session_id']+'/questions/'+q['question_id']+'/answer'
        refused=await client.post(url,json={'idempotency_key':'answer','option_id':'blue'})
        assert refused.status==401
        reply=await client.post(url,headers={'X-Workbench-Token':SECRET},json={'idempotency_key':'answer','option_id':'blue'})
        assert reply.status==200,await reply.text()
        read=await reg.execute('get_user_answer',{'question_id':q['question_id'],'wait_seconds':0},'call-read')
        assert not read.is_error,read.output
        data=json.loads(read.output)['question']
        assert data['state']=='consumed' and data['answer']['option_id']=='blue'
        assert server.session_store.get_run(run.run_id)['state']=='running'
        with server.session_store._connection() as c:assert c.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==1
    finally:await client.close()
