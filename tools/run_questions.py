"""Two Engines share the same scoped PAO question API, with bounded polling."""
from __future__ import annotations
import asyncio
import json
import aiohttp
from orchestrator.run_questions import issue_tool_token, QuestionError
from tools.workbench_client import workbench_endpoint

async def execute_run_question_tool(tool_name, arguments, *, audit_context, secrets, tool_call_id=''):
    context=dict(audit_context or {})
    try:
        base,agent=workbench_endpoint(context,require_agent=True)
        request_id=str(context.get('request_id') or '')
        runtime=context.get('_runtime')
        if not request_id and runtime is not None:
            request_id=str(getattr(runtime,'current_request_id','') or '')
        token=secrets.get('run_question_token')
        if not token:
            cfg=context.get('global_config')
            token=issue_tool_token(str(secrets.get('workbench_admin_token') or ''),
                instance_id=str(getattr(cfg,'instance_id',None) or context.get('instance_id') or ''),
                agent_id=agent,request_id=request_id)
        payload=dict(arguments)
        if 'operation' in payload:
            return 'Error: question_reserved_field'
        if tool_name=='ask_user':
            operation='create'
            payload.setdefault('idempotency_key',tool_call_id)
            wait=0
        elif tool_name=='get_user_answer':
            operation='get'
            wait=payload.pop('wait_seconds',0)
            if isinstance(wait,bool) or not isinstance(wait,(int,float)) or not 0<=wait<=30:
                return 'Error: question_wait_invalid'
        else:
            return 'Error: unknown_question_tool'
        deadline=asyncio.get_running_loop().time()+wait
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            while True:
                async with session.post(base+'/api/v1/run-questions/tool',
                        headers={'X-Hashi-Run-Question-Token':token},json={**payload,'operation':operation}) as response:
                    data=await response.json()
                    if response.status>=400 or data.get('ok') is False:
                        return 'Error: '+str(data.get('error_code') or 'question_request_failed')
                q=data.get('question',{})
                if operation=='create' or q.get('state')!='pending' or asyncio.get_running_loop().time()>=deadline:
                    return json.dumps(data,ensure_ascii=False)
                await asyncio.sleep(min(.5,max(0,deadline-asyncio.get_running_loop().time())))
    except (QuestionError,ValueError) as exc:
        return 'Error: '+str(getattr(exc,'code','question_scope_unavailable'))
    except (aiohttp.ClientError,asyncio.TimeoutError):
        return 'Error: question_transport_unknown; reconcile using the same idempotency key'
