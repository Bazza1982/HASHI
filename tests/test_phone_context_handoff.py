from __future__ import annotations
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest
import pytest_asyncio
from tests.test_live_voice_actions import phone, speak, decision, action
from tests.test_runtime_pipeline import _runtime, _item
from orchestrator import runtime_pipeline
from orchestrator.frontend_live_voice.protocol import Fragment


def turn_runtime(phone, *, fixed=True):
    runtime=_runtime()
    runtime.session_store=phone.store
    runtime.name=phone.agent_id
    runtime.backend_manager.agent_mode="fixed" if fixed else "flex"
    runtime.backend_manager.current_backend.capabilities.supports_sessions=fixed
    runtime.backend_manager.current_backend._session_id="native-thread-1" if fixed else None
    return runtime


def turn_item(phone, request="req-phone-1", metadata=None):
    return _item(request_id=request,session_id=phone.session_id,context_generation=1,
        owner_id=phone.owner_id,request_metadata=metadata or {"owner_id":phone.owner_id})


@pytest.mark.asyncio
async def test_fixed_resumed_text_gets_complete_role_phone_tail_then_no_repeat(phone):
    await phone.manager.append_fragment_once(phone.binding,Fragment("u1","user","Keep the budget under 100",0,100))
    await phone.manager.append_fragment_once(phone.binding,Fragment("a1","assistant","I understand the limit",150,200))
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",(phone.call_id,))
    runtime=turn_runtime(phone)
    item=turn_item(phone)
    prompt=await runtime_pipeline.build_turn_prompt(runtime,item,is_bridge_request=False)
    assert prompt.incremental
    assert "Keep the budget under 100" in prompt.final_prompt
    assert "I understand the limit" in prompt.final_prompt
    assert '"role":"user"' in prompt.final_prompt
    assert '"role":"assistant"' in prompt.final_prompt
    await runtime_pipeline.run_backend_generation(runtime,item,prompt.final_prompt,on_stream_event=None,audit_active=False)
    later=await runtime_pipeline.build_turn_prompt(runtime,turn_item(phone,"req-phone-2"),is_bridge_request=False)
    assert "Keep the budget under 100" not in later.final_prompt
    assert later.prompt_audit["phone_context_handoff"]["included_fragments"]==0


@pytest.mark.asyncio
async def test_actual_action_freezes_both_sides_before_real_run_admission(phone):
    await phone.manager.append_fragment_once(phone.binding,Fragment("constraint","user","Do not modify production records",0,50))
    await phone.manager.append_fragment_once(phone.binding,Fragment("ack","assistant","I will use a test copy",60,90))
    phone.judgments=[decision(action("query","Read the test status"))]
    await speak(phone,"Read the test status",start=100,end=200,source="instruction")
    proposal=phone.admitted_proposals[-1]
    assert proposal.phone_context_handoff_id
    from orchestrator.phone_context_handoff import load_handoff
    frozen=load_handoff(phone.store,proposal.phone_context_handoff_id,
        owner_id=phone.owner_id,agent_id=phone.agent_id,session_id=phone.session_id,context_generation=1)
    assert [r["role"] for r in frozen["fragments"]]==["user","assistant","user"]
    assert frozen["cutoff_ms"]==201
    await phone.manager.append_fragment_once(phone.binding,Fragment("later","user","Now delete production",220,280))
    frozen2=load_handoff(phone.store,proposal.phone_context_handoff_id,
        owner_id=phone.owner_id,agent_id=phone.agent_id,session_id=phone.session_id,context_generation=1)
    assert frozen2==frozen
    runtime=turn_runtime(phone)
    metadata={"owner_id":phone.owner_id,"live_voice":{"call_id":phone.call_id,
        "call_epoch":1,"delegation_id":proposal.delegation_id,
        "phone_context_handoff_id":proposal.phone_context_handoff_id}}
    prompt=await runtime_pipeline.build_turn_prompt(runtime,turn_item(phone,metadata=metadata),is_bridge_request=False)
    assert "Do not modify production records" in prompt.final_prompt
    assert "I will use a test copy" in prompt.final_prompt
    assert "Now delete production" not in prompt.final_prompt


@pytest.mark.asyncio
async def test_pao_ingress_keeps_verified_phone_handoff_for_worker_prompt(phone):
    from orchestrator.session_store import SessionConflict
    await phone.manager.append_fragment_once(phone.binding,Fragment("prior","assistant","Use only the test copy",0,50))
    phone.judgments=[decision(action("query","Read the test status"))]
    await speak(phone,"Read the test status",start=100,end=200,source="instruction")
    proposal=phone.admitted_proposals[-1]
    candidate={"call_id":phone.call_id,"call_epoch":1,"delegation_id":proposal.delegation_id,
        "proposal_version":proposal.version,"proposal_digest":proposal.digest,
        "phone_context_handoff_id":proposal.phone_context_handoff_id}
    scope={"owner_id":phone.owner_id,"agent_id":phone.agent_id,"session_id":phone.session_id,
        "context_generation":1}
    normalized=phone.store.resolve_live_voice_origin(**scope,candidate=candidate)
    assert normalized["phone_context_handoff_id"]==proposal.phone_context_handoff_id
    await phone.manager.append_fragment_once(phone.binding,Fragment("later","user","Delete the real record",220,300))
    runtime=turn_runtime(phone)
    item=turn_item(phone,metadata={"owner_id":phone.owner_id,"live_voice":normalized})
    prompt=await runtime_pipeline.build_turn_prompt(runtime,item,is_bridge_request=False)
    assert "Use only the test copy" in prompt.final_prompt
    assert "Read the test status" in prompt.final_prompt
    assert "Delete the real record" not in prompt.final_prompt
    with pytest.raises(SessionConflict,match="live_voice_origin_invalid"):
        phone.store.resolve_live_voice_origin(**scope,candidate={**candidate,
            "phone_context_handoff_id":"phone-handoff-unrelated"})


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed",[True,False])
async def test_second_action_phone_tail_then_hangup_preserves_provider_continuity(phone,fixed):
    runtime=turn_runtime(phone,fixed=fixed)
    phone.judgments=[decision(action("query","First query")),decision(action("query","Second query"))]
    await phone.manager.append_fragment_once(phone.binding,Fragment("pre","assistant","The important earlier restriction",0,30))
    await speak(phone,"First query",start=40,end=60,source="first")
    def action_item(proposal,req):
        return turn_item(phone,req,{"owner_id":phone.owner_id,"live_voice":{
            "call_id":phone.call_id,"call_epoch":1,"delegation_id":proposal.delegation_id,
            "proposal_version":proposal.version,"proposal_digest":proposal.digest,
            "phone_context_handoff_id":proposal.phone_context_handoff_id}})
    item=action_item(phone.admitted_proposals[-1],"req-action1")
    prompt=await runtime_pipeline.build_turn_prompt(runtime,item,is_bridge_request=False)
    assert "The important earlier restriction" in prompt.final_prompt
    await runtime_pipeline.run_backend_generation(runtime,item,prompt.final_prompt,on_stream_event=None,audit_active=False)
    await phone.manager.append_fragment_once(phone.binding,Fragment("reply","assistant","First query complete",80,100))
    await speak(phone,"Second query",start=120,end=140,source="second")
    second=action_item(phone.admitted_proposals[-1],"req-action2")
    second_prompt=await runtime_pipeline.build_turn_prompt(runtime,second,is_bridge_request=False)
    assert "First query complete" in second_prompt.final_prompt
    assert "Second query" in second_prompt.final_prompt
    assert ("The important earlier restriction" in second_prompt.final_prompt) is (not fixed)
    await runtime_pipeline.run_backend_generation(runtime,second,second_prompt.final_prompt,on_stream_event=None,audit_active=False)
    await phone.manager.append_fragment_once(phone.binding,Fragment("tail","assistant","The final phone detail",160,180))
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",(phone.call_id,))
    last=await runtime_pipeline.build_turn_prompt(runtime,turn_item(phone,"req-tail"),is_bridge_request=False)
    assert "The final phone detail" in last.final_prompt
    assert ("Second query" in last.final_prompt) is (not fixed)


@pytest.mark.asyncio
async def test_provider_failure_no_watermark_and_restart_model_or_thread_isolation(phone):
    from orchestrator.session_store import SessionStore
    await phone.manager.append_fragment_once(phone.binding,Fragment("data","user","Remember the complete transcript",0,100))
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",(phone.call_id,))
    runtime=turn_runtime(phone)
    item=turn_item(phone)
    first=await runtime_pipeline.build_turn_prompt(runtime,item,is_bridge_request=False)
    runtime.backend_manager.response=SimpleNamespace(is_success=False,text="",error="synthetic failure")
    await runtime_pipeline.run_backend_generation(runtime,item,first.final_prompt,on_stream_event=None,audit_active=False)
    retry=turn_item(phone,"req-retry")
    retry_prompt=await runtime_pipeline.build_turn_prompt(runtime,retry,is_bridge_request=False)
    assert "Remember the complete transcript" in retry_prompt.final_prompt
    runtime.backend_manager.response=SimpleNamespace(is_success=True,text="done")
    await runtime_pipeline.run_backend_generation(runtime,retry,retry_prompt.final_prompt,on_stream_event=None,audit_active=False)
    runtime.session_store=SessionStore(phone.db_path,instance_id="HASHI")
    restored=await runtime_pipeline.build_turn_prompt(runtime,turn_item(phone,"req-restored"),is_bridge_request=False)
    assert "Remember the complete transcript" not in restored.final_prompt
    runtime.backend_manager.current_backend._session_id="native-thread-new"
    changed=await runtime_pipeline.build_turn_prompt(runtime,turn_item(phone,"req-new-thread"),is_bridge_request=False)
    assert "Remember the complete transcript" in changed.final_prompt
    runtime.config.active_backend="claude-code"
    swapped=await runtime_pipeline.build_turn_prompt(runtime,turn_item(phone,"req-new-engine"),is_bridge_request=False)
    assert "Remember the complete transcript" in swapped.final_prompt


@pytest.mark.asyncio
async def test_action_retry_snapshot_exact_and_cross_scope_cannot_read(phone):
    from orchestrator.phone_context_handoff import freeze_action_handoff,load_handoff,PhoneContextError
    await speak(phone,"Read the scoped file",source="action")
    proposal=phone.admitted_proposals[-1]
    original=load_handoff(phone.store,proposal.phone_context_handoff_id,owner_id=phone.owner_id,
        agent_id=phone.agent_id,session_id=phone.session_id,context_generation=1)
    await phone.manager.append_fragment_once(phone.binding,Fragment("late-old","assistant","Delayed old speech",10,20))
    assert freeze_action_handoff(phone.store,phone.binding,proposal)==proposal.phone_context_handoff_id
    assert load_handoff(phone.store,proposal.phone_context_handoff_id,owner_id=phone.owner_id,
        agent_id=phone.agent_id,session_id=phone.session_id,context_generation=1)==original
    for change in ({"owner_id":"other"},{"agent_id":"other"},{"context_generation":2}):
        scope={"owner_id":phone.owner_id,"agent_id":phone.agent_id,"session_id":phone.session_id,"context_generation":1,**change}
        with pytest.raises(Exception): load_handoff(phone.store,proposal.phone_context_handoff_id,**scope)


@pytest.mark.asyncio
async def test_long_context_failclosed_without_silent_tail_or_source_mutation(phone,monkeypatch):
    from orchestrator.phone_context_handoff import PhoneContextError
    await phone.manager.append_fragment_once(phone.binding,Fragment("long","user","X"*6000,0,100))
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",(phone.call_id,))
    monkeypatch.setattr("orchestrator.phone_context_handoff.MAX_CONTEXT_BYTES",1000)
    runtime=turn_runtime(phone)
    with pytest.raises(PhoneContextError,match="phone_context_budget_exceeded") as rejected:
        await runtime_pipeline.build_turn_prompt(runtime,turn_item(phone),is_bridge_request=False)
    assert phone.session_id in str(rejected.value)
    assert runtime.backend_manager.calls==[]
    assert phone.store.live_transcript_segments(phone.session_id)[0]["text"]=="X"*6000


@pytest.mark.asyncio
async def test_pending_durable_fragment_blocks_hangup_text_before_any_provider_call(phone):
    from orchestrator.phone_context_handoff import PhoneContextError
    await phone.manager.append_fragment_once(phone.binding,Fragment("done","user","Confirmed earlier text",0,100))
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",(phone.call_id,))
        connection.execute("""INSERT INTO live_provider_fragment_inbox VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (phone.owner_id,"pending",phone.call_id,1,phone.session_id,"user","Delayed phone text",120,140,"2026-10-04T12:00:00Z"))
    runtime=turn_runtime(phone)
    with pytest.raises(PhoneContextError,match="phone_context_transcript_pending"):
        await runtime_pipeline.build_turn_prompt(runtime,turn_item(phone),is_bridge_request=False)
    assert runtime.backend_manager.calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine",["codex-cli","claude-code","antigravity-cli","her-v2","openrouter-api"])
async def test_shared_phone_projection_contract_independent_of_engine(phone,engine):
    from orchestrator.phone_context_handoff import prepare_turn
    await phone.manager.append_fragment_once(phone.binding,Fragment("user","user","User role",0,100))
    await phone.manager.append_fragment_once(phone.binding,Fragment("assistant","assistant","Frontend role",120,200))
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",(phone.call_id,))
    runtime=turn_runtime(phone)
    runtime.config.active_backend=engine
    sections,audit=prepare_turn(runtime,turn_item(phone),incremental=True)
    assert audit["included_fragments"]==2
    assert sections[0][2]["protected"] is True
    assert sections[0][2]["authority"]=="history"
    assert '"role":"user"' in sections[0][1] and '"role":"assistant"' in sections[0][1]


@pytest.mark.asyncio
async def test_real_pcm_and_her_fixed_materialize_phone_prefix_and_tail_without_promotion(phone,tmp_path):
    from orchestrator.phone_context_handoff import prepare_turn,commit_success
    from orchestrator.bridge_memory import BridgeContextAssembler,BridgeMemoryStore
    from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
    runtime=turn_runtime(phone)
    runtime.config.active_backend="her-v2"
    coordinator=HerBackendSessionCoordinator(tmp_path/"her-state")
    (tmp_path/"pcm").mkdir()
    assembler=BridgeContextAssembler(BridgeMemoryStore(tmp_path/"pcm"),None)
    await phone.manager.append_fragment_once(phone.binding,Fragment("prefix","user","Retain the first phone constraint",0,100))
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",(phone.call_id,))
    first_item=turn_item(phone)
    sections,_=prepare_turn(runtime,first_item,incremental=True)
    def transport(item,sections):
        payload=assembler.build_prompt_payload("Question","her-v2",extra_sections=sections,incremental=True)
        phone_parts=[s for s in payload["transport_snapshot"]["sections"] if s["key"].startswith("phone_external_context:")]
        assert len(phone_parts)==1
        assert phone_parts[0]["authority"]=="history" and phone_parts[0]["protected"]
        encoded,_=coordinator.prepare_transport(session_id="her-phone",sections=payload["transport_snapshot"]["sections"],
            resources=[],user_message="Question",request_id=item.request_id,message_id=item.request_id,
            instance_id="HASHI",agent_id=phone.agent_id,owner_id=phone.owner_id,
            hashi_conversation_id=phone.session_id,context_generation=1,workzone_identity="test")
        return coordinator.accept(encoded)
    first=transport(first_item,sections)
    coordinator.complete(first,assistant_text="Seen")
    commit_success(first_item,SimpleNamespace(is_success=True))
    await phone.manager.append_fragment_once(phone.binding,Fragment("tail","assistant","Retain the later phone detail",120,200))
    second_item=turn_item(phone,"second-her")
    second_sections,_=prepare_turn(runtime,second_item,incremental=True)
    assert sections[0][2]["key"]!=second_sections[0][2]["key"]
    second=transport(second_item,second_sections)
    assert "Retain the first phone constraint" in second.materialized_prompt
    assert "Retain the later phone detail" in second.materialized_prompt


@pytest.mark.asyncio
async def test_phone_checkpoint_io_failure_preserves_success_without_action_replay(phone,monkeypatch):
    await phone.manager.append_fragment_once(phone.binding,Fragment("text","user","The prior constraint",0,100))
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",(phone.call_id,))
    runtime=turn_runtime(phone)
    item=turn_item(phone)
    prompt=await runtime_pipeline.build_turn_prompt(runtime,item,is_bridge_request=False)
    def fail(_item,_response): raise OSError("secret path")
    monkeypatch.setattr("orchestrator.phone_context_handoff.commit_success",fail)
    result=await runtime_pipeline.run_backend_generation(runtime,item,prompt.final_prompt,on_stream_event=None,audit_active=False)
    assert result.response.is_success
    assert len(runtime.backend_manager.calls)==1
    assert runtime.maintenance_events[-1][0]=="phone_context_checkpoint_failed"
    assert "secret path" not in str(runtime.logger.messages)
