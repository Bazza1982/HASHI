"""Independent negative-boundary review of scoped PAO Run questions."""
from __future__ import annotations
import asyncio
import json
from types import SimpleNamespace
import pytest
from aiohttp.test_utils import TestClient, TestServer
from orchestrator.run_questions import QuestionError, issue_tool_token
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_run_questions import SECRET, active, spec


@pytest.mark.asyncio
async def test_run_token_cannot_answer_admin_or_query_another_run(tmp_path):
    cfg=SimpleNamespace(bridge_home=tmp_path,project_root=tmp_path,instance_id="HASHI1",authorized_id=7,
        workbench_port=18800,api_gateway_port=18801,deployment_profile="personal")
    path=tmp_path/'agents.json'
    path.write_text(json.dumps({'global':{},'agents':[{'name':'lily'}]}))
    server=WorkbenchApiServer(path,cfg,secrets={'workbench_admin_token':SECRET},reconcile_session_runs=False)
    session,run=active(server.session_store)
    other_session,other_run=active(server.session_store,'req-later')
    client=TestClient(TestServer(server.app)); await client.start_server()
    try:
        token=issue_tool_token(SECRET,instance_id='HASHI1',agent_id='lily',request_id=run.request_id)
        create=await client.post('/api/v1/run-questions/tool',headers={'X-Hashi-Run-Question-Token':token},
            json={'operation':'create',**spec()})
        assert create.status==200,await create.text()
        q=(await create.json())['question']
        answer_url='/api/v1/sessions/'+session['session_id']+'/questions/'+q['question_id']+'/answer'
        refused=await client.post(answer_url,headers={'X-Workbench-Token':token},
            json={'idempotency_key':'answer','option_id':'blue'})
        assert refused.status==401
        admin=await client.post('/api/admin/add-agent',headers={'X-Workbench-Token':token},
            json={'name':'must-not-create','backend':'codex-cli'})
        assert admin.status==403
        assert not (tmp_path/'workspaces'/'must-not-create').exists()
        other_token=issue_tool_token(SECRET,instance_id='HASHI1',agent_id='lily',request_id=other_run.request_id)
        wrong_run=await client.post('/api/v1/run-questions/tool',headers={'X-Hashi-Run-Question-Token':other_token},
            json={'operation':'get','question_id':q['question_id']})
        assert wrong_run.status==404
        conflicting=await client.post('/api/v1/run-questions/tool',headers={'X-Hashi-Run-Question-Token':token},
            json={'operation':'create',**spec(),'question':'different question same key'})
        assert conflicting.status==409
        assert (await conflicting.json())['error_code']=='question_idempotency_conflict'
        refused=await client.post('/api/v1/run-questions/tool',json={'operation':'create',**spec('anonymous')})
        assert refused.status==401
        from tools.registry import ToolRegistry
        cfg.workbench_port=client.server.port
        registry=ToolRegistry(allowed_tools=['ask_user','get_user_answer'], workspace_dir=tmp_path,
            access_root=tmp_path,secrets={'workbench_admin_token':SECRET},
            audit_context={'agent_name':'lily','request_id':run.request_id,'instance_id':'HASHI1',
                           'global_config':cfg,'workbench_api_base_url':str(client.make_url('')).rstrip('/')})
        overridden=await registry.execute('ask_user', {**spec('reserved'),
            'operation':'get','question_id':q['question_id']}, 'call-reserved')
        assert overridden.is_error and 'question_reserved_field' in overridden.output
        result=server._questions().list(session_id=session['session_id'],owner_id='user:7')
        assert len(result)==1 and result[0]['state']=='pending' and result[0]['answer'] is None
    finally:
        await client.close()


def test_answer_write_fences_an_independent_worker_cancellation(tmp_path):
    import sqlite3
    from contextlib import contextmanager
    from orchestrator.session_store import SessionStore
    from orchestrator.run_questions import RunQuestions
    store=SessionStore(tmp_path/'questions.sqlite3',instance_id='HASHI1')
    worker_store=SessionStore(store.db_path,instance_id='HASHI1')
    service=RunQuestions(store)
    session,run=active(store)
    question=service.create(request_id=run.request_id,agent_id='lily',payload=spec())
    original_worker_connect=worker_store._connect
    def nonblocking_connection():
        c=original_worker_connect()
        c.execute('PRAGMA busy_timeout=0')
        return c
    worker_store._connect=nonblocking_connection
    outcomes=[]
    class CancellationBeforeWrite:
        def __init__(self,c):self.c=c
        def __getattr__(self,name):return getattr(self.c,name)
        def execute(self,sql,*args):
            if sql.startswith("UPDATE run_questions SET state='answered'"):
                try:
                    worker_store.finish_request(run.request_id,success=False,error_text='QA cancellation')
                    outcomes.append('committed-before-answer')
                except sqlite3.OperationalError as error:
                    if 'locked' not in str(error):raise
                    outcomes.append('fenced-until-answer-commit')
            return self.c.execute(sql,*args)
    original=store._connection
    @contextmanager
    def connection():
        with original() as c:yield CancellationBeforeWrite(c)
    store._connection=connection
    saved=service.answer(session_id=session['session_id'],owner_id='user:7',question_id=question['question_id'],
        payload={'idempotency_key':'answer','option_id':'blue'})
    assert outcomes==['fenced-until-answer-commit']
    assert saved['state']=='answered' and worker_store.get_run(run.run_id)['state']=='running'
    worker_store.finish_request(run.request_id,success=False,error_text='QA cancellation after answer commit')
    with pytest.raises(QuestionError,match='cancelled'):
        service.answer(session_id=session['session_id'],owner_id='user:7',question_id=question['question_id'],
            payload={'idempotency_key':'late','option_id':'red'})
    # Rejected late writes still durably publish the legitimate cancellation,
    # even when the caller does not first perform a list/refresh read.
    with original() as c:
        row=c.execute('SELECT state,answer_key FROM run_questions WHERE question_id=?',(question['question_id'],)).fetchone()
    assert row['state']=='cancelled' and row['answer_key']=='answer'


def test_independent_connections_create_and_answer_exactly_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from orchestrator.session_store import SessionStore
    from orchestrator.run_questions import RunQuestions
    first_store=SessionStore(tmp_path/'questions.sqlite3',instance_id='HASHI1')
    first=RunQuestions(first_store)
    second=RunQuestions(SessionStore(first_store.db_path,instance_id='HASHI1'))
    session,run=active(first_store)
    barrier=Barrier(2)
    def create(service):
        barrier.wait(timeout=3)
        return service.create(request_id=run.request_id,agent_id='lily',payload=spec())
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses=list(workers.map(create,[first,second]))
    assert responses[0]['question_id']==responses[1]['question_id']
    question=responses[0]
    barrier=Barrier(2)
    def answer(service):
        barrier.wait(timeout=3)
        return service.answer(session_id=session['session_id'],owner_id='user:7',question_id=question['question_id'],
            payload={'idempotency_key':'same-answer','option_id':'blue'})
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses=list(workers.map(answer,[first,second]))
    assert responses[0]==responses[1] and responses[0]['state']=='answered'
    with first_store._connection() as c:
        assert c.execute('SELECT COUNT(*) FROM run_questions').fetchone()[0]==1
        assert c.execute("SELECT COUNT(*) FROM run_events WHERE kind='run.question.created'").fetchone()[0]==1
        assert c.execute("SELECT COUNT(*) FROM run_events WHERE kind='run.question.answered'").fetchone()[0]==1


def test_question_uses_canonical_agent_identity_from_run(tmp_path):
    from orchestrator.session_store import SessionStore
    from orchestrator.run_questions import RunQuestions
    store=SessionStore(tmp_path/'questions.sqlite3',instance_id='HASHI1')
    service=RunQuestions(store)
    session,run=active(store,agent='MixedCase')
    question=service.create(request_id=run.request_id,agent_id='MixedCase',payload=spec())
    assert question['agent_id']==store.get_run(run.run_id)['agent_id']
    saved=service.answer(session_id=session['session_id'],owner_id='user:7',question_id=question['question_id'],
        payload={'idempotency_key':'answer','option_id':'blue'})
    assert saved['state']=='answered'
    assert service.get_answer(request_id=run.request_id,agent_id='MixedCase',question_id=question['question_id'])['state']=='consumed'
