from __future__ import annotations
import asyncio
import hashlib
import json
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


def legacy_phone_section(phone, fragments):
    identity = [
        (row["call_id"], row["call_epoch"], row["provider_event_id"])
        for row in fragments
    ]
    digest = hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    key = f"phone_external_context:{digest}"
    payload = {
        "scope": {
            "session_id": phone.session_id,
            "agent_id": phone.agent_id,
            "context_generation": 1,
        },
        "handoff_id": None,
        "authorization_cutoff_ms": None,
        "previous_prefix_already_received": 0,
        "fragments": fragments,
    }
    body = (
        "Quoted Phone conversation from a different connection.\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    return key, (
        "PHONE CONVERSATION CONTEXT",
        body,
        {"key": key, "authority": "history", "protected": True},
    )


def packed_legacy_phone_section(phone, fragments):
    from orchestrator.phone_context_handoff import _packed_payload

    key, section = legacy_phone_section(phone, fragments)
    prefix, encoded = section[1].split("\n", 1)
    payload = _packed_payload(json.loads(encoded))
    body = prefix + "\n" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )
    return key, (
        section[0],
        body,
        {
            **section[2],
            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        },
    )


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
    assert "USER: Keep the budget under 100" in prompt.final_prompt
    assert "ASSISTANT: I understand the limit" in prompt.final_prompt
    assert "provider_event_id" not in prompt.final_prompt
    await runtime_pipeline.run_backend_generation(runtime,item,prompt.final_prompt,on_stream_event=None,audit_active=False)
    later=await runtime_pipeline.build_turn_prompt(runtime,turn_item(phone,"req-phone-2"),is_bridge_request=False)
    assert "Keep the budget under 100" not in later.final_prompt
    assert later.prompt_audit["phone_context_handoff"]["included_fragments"]==0


@pytest.mark.asyncio
async def test_nonincremental_phone_uses_canonical_session_history_once(phone,tmp_path):  # noqa: F811
    from orchestrator.bridge_memory import BridgeContextAssembler,BridgeMemoryStore
    await phone.manager.append_fragment_once(
        phone.binding,Fragment("canonical-user","user","Canonical phone request",0,100)
    )
    await phone.manager.append_fragment_once(
        phone.binding,Fragment("canonical-assistant","assistant","Canonical phone reply",120,200)
    )
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",
            (phone.call_id,),
        )
    runtime=turn_runtime(phone,fixed=False)
    (tmp_path/"pcm").mkdir()
    runtime.context_assembler=BridgeContextAssembler(
        BridgeMemoryStore(tmp_path/"pcm"),None
    )
    prompt=await runtime_pipeline.build_turn_prompt(
        runtime,turn_item(phone),is_bridge_request=False
    )
    assert prompt.final_prompt.count("Canonical phone request")==1
    assert prompt.final_prompt.count("Canonical phone reply")==1
    assert "canonical-user" not in prompt.final_prompt
    assert "provider_event_id" not in prompt.final_prompt
    assert not any(
        section[0] in {"PHONE CONVERSATION CONTEXT","PHONE ACTION CONTEXT"}
        for section in prompt.extra_sections
    )


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
    assert "The important earlier restriction" in second_prompt.final_prompt
    await runtime_pipeline.run_backend_generation(runtime,second,second_prompt.final_prompt,on_stream_event=None,audit_active=False)
    await phone.manager.append_fragment_once(phone.binding,Fragment("tail","assistant","The final phone detail",160,180))
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",(phone.call_id,))
    last=await runtime_pipeline.build_turn_prompt(runtime,turn_item(phone,"req-tail"),is_bridge_request=False)
    if fixed:
        assert "The final phone detail" in last.final_prompt
        assert "Second query" in last.final_prompt
        assert "The important earlier restriction" in last.final_prompt
    else:
        canonical = phone.store.recent_exchanges(phone.session_id, limit=8)
        assert "The final phone detail" in str(canonical)
        assert "Second query" in str(canonical)


@pytest.mark.asyncio
async def test_first_her_full_open_action_becomes_ordinary_history_once_without_tail(
    phone, tmp_path
):
    from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore

    class FixedPhoneBackend:
        def __init__(self):
            self._session_id = None
            self.persistent_session_busy = False
            self.sections = {}
            self.capabilities = SimpleNamespace(
                supports_sessions=True,
                supports_thinking_stream=True,
            )

        def can_resume_fixed_session(self):
            return self._session_id is not None

        def current_pcm_section_keys(self):
            return tuple(self.sections)

        def current_pcm_sections(self, keys):
            return {key: self.sections[key] for key in keys if key in self.sections}

        def prepare_fixed_turn_input(self, *, prompt_payload, user_message, **_kwargs):
            operation = "open_session" if self._session_id is None else "append_turn"
            snapshot = prompt_payload["transport_snapshot"]
            if operation == "open_session":
                self.sections = {}
                self._session_id = "her-first-phone-action"
            for key in snapshot.get("removed_section_keys") or ():
                self.sections.pop(key, None)
            for section in snapshot.get("sections") or ():
                self.sections[section["key"]] = dict(section)
            rendered = user_message + "\n" + "\n".join(
                str(section.get("text") or "") for section in self.sections.values()
            )
            return rendered, {
                "operation": operation,
                "incremental": operation == "append_turn",
                "transport_chars": len(rendered),
            }

    runtime = turn_runtime(phone)
    runtime.config.active_backend = "her-v2"
    (tmp_path / "pcm").mkdir()
    runtime.context_assembler = BridgeContextAssembler(
        BridgeMemoryStore(tmp_path / "pcm"), None
    )
    backend = FixedPhoneBackend()
    runtime.backend_manager.current_backend = backend
    await phone.manager.append_fragment_once(
        phone.binding,
        Fragment("first-open-prefix", "assistant", "Use only the test system", 0, 30),
    )
    phone.judgments = [decision(action("query", "Inspect the test system"))]
    await speak(
        phone,
        "Inspect the test system",
        start=40,
        end=80,
        source="first-open-action",
    )
    proposal = phone.admitted_proposals[-1]
    action_item = turn_item(phone, "first-open-action", {
        "owner_id": phone.owner_id,
        "live_voice": {
            "call_id": phone.call_id,
            "call_epoch": 1,
            "delegation_id": proposal.delegation_id,
            "proposal_version": proposal.version,
            "proposal_digest": proposal.digest,
            "phone_context_handoff_id": proposal.phone_context_handoff_id,
        },
    })
    runtime_pipeline.begin_queue_item(runtime, action_item)
    action_prompt = await runtime_pipeline.build_turn_prompt(
        runtime, action_item, is_bridge_request=False
    )
    assert action_prompt.incremental is False
    assert "phone_action_context" in backend.current_pcm_section_keys()
    await runtime_pipeline.run_backend_generation(
        runtime,
        action_item,
        action_prompt.final_prompt,
        on_stream_event=None,
        audit_active=False,
    )

    with phone.store._lock, phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-27T10:00:00Z' "
            "WHERE call_id=?",
            (phone.call_id,),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM phone_context_consumption WHERE call_id=?",
            (phone.call_id,),
        ).fetchone()[0] == 0

    text_item = turn_item(phone, "after-first-open")
    runtime_pipeline.begin_queue_item(runtime, text_item)
    text_prompt = await runtime_pipeline.build_turn_prompt(
        runtime, text_item, is_bridge_request=False
    )
    phone_audit = text_prompt.prompt_audit["phone_context_handoff"]
    assert phone_audit["included_fragments"] == 2
    assert "phone_action_context" in phone_audit["removed_section_keys"]
    assert "phone_action_context" not in backend.current_pcm_section_keys()
    semantic_keys = [
        key for key in backend.current_pcm_section_keys()
        if key.startswith("recent_exchange:phone:")
    ]
    assert len(semantic_keys) == 1
    assert text_prompt.final_prompt.count("Use only the test system") == 1
    assert text_prompt.final_prompt.count("Inspect the test system") == 1
    await runtime_pipeline.run_backend_generation(
        runtime,
        text_item,
        text_prompt.final_prompt,
        on_stream_event=None,
        audit_active=False,
    )

    later_item = turn_item(phone, "after-first-open-later")
    runtime_pipeline.begin_queue_item(runtime, later_item)
    later_prompt = await runtime_pipeline.build_turn_prompt(
        runtime, later_item, is_bridge_request=False
    )
    assert later_prompt.prompt_audit["phone_context_handoff"]["included_fragments"] == 0
    assert [
        key for key in backend.current_pcm_section_keys()
        if key.startswith("recent_exchange:phone:")
    ] == semantic_keys


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
    from orchestrator.phone_context_handoff import freeze_action_handoff,load_handoff
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
async def test_long_phone_history_uses_pcm_budget_without_a_separate_phone_limit(phone):
    for i in range(750):
        await phone.manager.append_fragment_once(phone.binding, Fragment(
            f"production-shaped-{i:04d}",
            "user" if i % 2 else "assistant",
            " word" if i % 2 else "原话",
            i * 200,
            i * 200 + 100,
        ))
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",(phone.call_id,))
    runtime=turn_runtime(phone)
    prompt = await runtime_pipeline.build_turn_prompt(
        runtime, turn_item(phone), is_bridge_request=False
    )
    assert "USER:" in prompt.final_prompt and "ASSISTANT:" in prompt.final_prompt
    assert "production-shaped-" not in prompt.final_prompt
    assert "provider_event_id" not in prompt.final_prompt
    assert prompt.prompt_audit["phone_context_handoff"]["included_fragments"] == 750
    assert runtime.backend_manager.calls==[]
    with phone.store._lock, phone.store._connection() as connection:
        durable_count = connection.execute(
            "SELECT COUNT(*) FROM live_fragments WHERE call_id=?", (phone.call_id,)
        ).fetchone()[0]
    assert durable_count == 750


@pytest.mark.asyncio
async def test_word_stream_projects_speech_without_provider_fragment_metadata(phone):
    from orchestrator.phone_context_handoff import prepare_turn
    fragments = [Fragment(f"event_stream_{i:020d}", "user" if i % 2 else "assistant",
                          " word" if i % 2 else "原话", i * 200, i * 200 + 100)
                 for i in range(750)]
    for fragment in fragments:
        await phone.manager.append_fragment_once(phone.binding, fragment)
    with phone.store._lock, phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?", (phone.call_id,))
    runtime = turn_runtime(phone)
    runtime.config.active_backend = "codex-cli"
    sections, audit = prepare_turn(runtime, turn_item(phone), incremental=True)
    rendered = "\n".join(section[1] for section in sections)
    assert "USER:" in rendered
    assert "ASSISTANT:" in rendered
    assert "event_stream_" not in rendered
    assert "provider_event_id" not in rendered
    assert audit["source_event_ids"] == [f.provider_event_id for f in fragments]
    assert audit["included_fragments"] == len(fragments)
    assert sections
    assert all(section[2]["authority"] == "history" for section in sections)
    assert all(section[2]["protected"] is False for section in sections)
    assert all(section[2]["key"].startswith("recent_exchange:") for section in sections)
    assert runtime.backend_manager.calls == []
    with phone.store._lock, phone.store._connection() as connection:
        durable = connection.execute("SELECT provider_event_id,sequence FROM live_fragments WHERE call_id=? ORDER BY start_ms",
                                     (phone.call_id,)).fetchall()
    assert audit["source_event_ids"] == [str(row[0]) for row in durable]
    assert len(durable) == len(fragments)


@pytest.mark.asyncio
async def test_word_stream_action_snapshot_roundtrip_and_legacy_storage_compatibility(phone):
    from orchestrator.phone_context_handoff import load_handoff
    for i in range(320):
        await phone.manager.append_fragment_once(phone.binding, Fragment(
            f"event_stream_{i:020d}", "assistant", "原话", i * 200, i * 200 + 100))
    phone.judgments = [decision(action("query", "Read the scoped file"))]
    await speak(phone, "Read the scoped file", start=65000, end=65200, source="instruction")
    proposal = phone.admitted_proposals[-1]
    scope = {"owner_id": phone.owner_id, "agent_id": phone.agent_id,
             "session_id": phone.session_id, "context_generation": 1}
    frozen = load_handoff(phone.store, proposal.phone_context_handoff_id, **scope)
    assert len(frozen["fragments"]) == 321
    assert frozen["fragments"][0]["text"] == "原话"
    assert frozen["fragments"][-1]["text"] == "Read the scoped file"
    assert frozen["cutoff_ms"] == 65201
    with phone.store._lock, phone.store._connection() as connection:
        # Old Functions persisted the ungrouped representation; it remains readable.
        connection.execute("UPDATE phone_context_handoffs SET payload_json=? WHERE handoff_id=?",
                           (json.dumps(frozen, ensure_ascii=False), proposal.phone_context_handoff_id))
    assert load_handoff(phone.store, proposal.phone_context_handoff_id, **scope) == frozen


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
    assert sections[0][2]["protected"] is False
    assert sections[0][2]["authority"]=="history"
    rendered = "\n".join(section[1] for section in sections)
    assert "USER: User role" in rendered
    assert "ASSISTANT: Frontend role" in rendered
    assert "provider_event_id" not in rendered
    assert '"role"' not in rendered


@pytest.mark.asyncio
async def test_phone_history_units_follow_actual_dialogue_rounds(phone):
    from orchestrator.phone_context_handoff import prepare_turn

    fragments=[
        Fragment("u1a","user","Only operate ",0,20),
        Fragment("u1b","user","on test.",21,40),
        Fragment("a1a","assistant","Understood. ",50,70),
        Fragment("a1b","assistant","Test only.",71,90),
        Fragment("u2","user","Now inspect status.",100,130),
        Fragment("a2","assistant","Status inspected.",140,170),
    ]
    for fragment in fragments:
        await phone.manager.append_fragment_once(phone.binding,fragment)
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",
            (phone.call_id,),
        )
    sections,_=prepare_turn(
        turn_runtime(phone),turn_item(phone),incremental=True
    )
    assert len(sections)==2
    assert "USER: Only operate on test." in sections[0][1]
    assert "ASSISTANT: Understood. Test only." in sections[0][1]
    assert "Now inspect status." not in sections[0][1]
    assert "USER: Now inspect status." in sections[1][1]
    assert "ASSISTANT: Status inspected." in sections[1][1]


@pytest.mark.asyncio
async def test_incremental_phone_history_uses_standard_pcm_compaction(phone,tmp_path):  # noqa: F811
    from orchestrator.phone_context_handoff import prepare_turn
    from orchestrator.bridge_memory import BridgeContextAssembler,BridgeMemoryStore
    for i in range(30):
        start=i * 400
        await phone.manager.append_fragment_once(
            phone.binding,Fragment(
                f"budget-user-{i}","user",
                f"ROUND-{i:02d}-USER" + (" important detail" * 20),
                start,start + 100,
            ),
        )
        await phone.manager.append_fragment_once(
            phone.binding,Fragment(
                f"budget-assistant-{i}","assistant",
                f"ROUND-{i:02d}-ASSISTANT" + (" confirmed detail" * 20),
                start + 150,start + 300,
            ),
        )
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",
            (phone.call_id,),
        )
    runtime=turn_runtime(phone)
    sections,_=prepare_turn(runtime,turn_item(phone),incremental=True)
    assert len(sections)==30
    (tmp_path/"pcm").mkdir()
    assembler=BridgeContextAssembler(BridgeMemoryStore(tmp_path/"pcm"),None)
    payload=assembler.build_prompt_payload(
        "Question",
        "codex-cli",
        extra_sections=sections,
        incremental=True,
        prompt_budget_tokens=5000,
    )
    omitted={row["key"] for row in payload["audit"]["history_omitted"]}
    retained={
        row["key"] for row in payload["transport_snapshot"]["sections"]
        if row["key"].startswith("recent_exchange:phone:")
    }
    assert payload["audit"]["budget_applied"]
    assert payload["audit"]["history_capsule"]["present"]
    assert sections[0][2]["key"] in omitted
    assert sections[-1][2]["key"] in retained
    assert omitted and retained
    assert "COMPACTED HISTORY CONTINUITY CAPSULE" in payload["final_prompt"]


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
        phone_parts=[s for s in payload["transport_snapshot"]["sections"] if s["key"].startswith("recent_exchange:phone:")]
        assert len(phone_parts)==1
        assert phone_parts[0]["authority"]=="history" and not phone_parts[0]["protected"]
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
async def test_real_fixed_pcm_migrates_legacy_phone_sections_after_action(phone,tmp_path):  # noqa: F811
    from adapters.her_v2 import HERv2Adapter
    from orchestrator.phone_context_handoff import prepare_turn,commit_success,_fragments
    from orchestrator.bridge_memory import BridgeContextAssembler,BridgeMemoryStore
    from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator

    runtime=turn_runtime(phone)
    runtime.config.active_backend="her-v2"
    coordinator=HerBackendSessionCoordinator(tmp_path/"her-state")
    (tmp_path/"pcm").mkdir()
    assembler=BridgeContextAssembler(BridgeMemoryStore(tmp_path/"pcm"),None)
    historic_call=f"{phone.call_id}-historic"
    historic_binding=replace(
        phone.binding,call_id=historic_call,provider_session_id="prov-historic"
    )

    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("""INSERT INTO live_calls(
            call_id,owner_id,session_id,agent_id,instance_id,instance_generation,
            context_generation,call_epoch,provider_session_id,phase,
            controller_lease,lease_expiry,started_at,max_ends_at,ended_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'ended',?,?,?,?,?)""",(
            historic_call,phone.owner_id,phone.session_id,phone.agent_id,"HASHI","1",
            1,1,"prov-historic","lease-historic","2099-01-01T00:00:00Z",
            "2026-10-03T10:00:00Z","2099-01-01T00:00:00Z",
            "2026-10-03T10:01:00Z",
        ))
    await phone.manager.append_fragment_once(
        historic_binding,
        Fragment("legacy-event","assistant","Remember the earlier phone history",0,30),
    )
    with phone.store._lock,phone.store._connection() as connection:
        legacy_fragments=_fragments(connection,owner_id=phone.owner_id,
            agent_id=phone.agent_id,session_id=phone.session_id,context_generation=1,
            call_id=historic_call,call_epoch=1,ended_only=True)
    legacy_key,legacy_section=legacy_phone_section(phone,legacy_fragments)

    def transport(item,sections,removed=()):
        payload=assembler.build_prompt_payload(
            "Question","her-v2",extra_sections=sections,incremental=True
        )
        encoded,_=coordinator.prepare_transport(
            session_id="her-phone",sections=payload["transport_snapshot"]["sections"],
            resources=[],user_message="Question",request_id=item.request_id,
            message_id=item.request_id,instance_id="HASHI",agent_id=phone.agent_id,
            owner_id=phone.owner_id,hashi_conversation_id=phone.session_id,
            context_generation=1,workzone_identity="test",removed_section_keys=removed,
        )
        return coordinator.accept(encoded)

    seed=turn_item(phone,"legacy-seed")
    opened=transport(seed,[legacy_section])
    coordinator.complete(opened,assistant_text="seeded")

    adapter=HERv2Adapter.__new__(HERv2Adapter)
    adapter._session_coordinator=coordinator
    adapter._session_id="her-phone"
    assert legacy_key in adapter.current_pcm_section_keys()
    assert legacy_key in adapter.current_pcm_sections((legacy_key,))
    runtime.backend_manager.current_backend.current_pcm_section_keys=(
        adapter.current_pcm_section_keys
    )
    runtime.backend_manager.current_backend.current_pcm_sections=(
        adapter.current_pcm_sections
    )

    await phone.manager.append_fragment_once(
        phone.binding,Fragment("before","assistant","Keep the earlier phone fact",0,30)
    )
    phone.judgments=[decision(action("query","Run the phone action"))]
    await speak(phone,"Run the phone action",start=40,end=80,source="action")
    proposal=phone.admitted_proposals[-1]
    action_item=turn_item(phone,"phone-action",{"owner_id":phone.owner_id,"live_voice":{
        "call_id":phone.call_id,"call_epoch":1,"delegation_id":proposal.delegation_id,
        "proposal_version":proposal.version,"proposal_digest":proposal.digest,
        "phone_context_handoff_id":proposal.phone_context_handoff_id}})
    action_sections,action_audit=prepare_turn(runtime,action_item,incremental=True)
    action_text="\n".join(section[1] for section in action_sections)
    assert legacy_key in action_audit["removed_section_keys"]
    assert action_audit["history_migration_fragments"]==1
    assert action_audit["history_migration_source_event_ids"]==["legacy-event"]
    assert "Remember the earlier phone history" in action_text
    assert "Keep the earlier phone fact" in action_text
    assert "Run the phone action" in action_text
    assert "provider_event_id" not in action_text and "call_id" not in action_text
    accepted=transport(action_item,action_sections,action_audit["removed_section_keys"])
    assert "legacy-event" not in accepted.materialized_prompt
    assert "Run the phone action" in accepted.materialized_prompt
    coordinator.complete(accepted,assistant_text="action complete")
    commit_success(action_item,SimpleNamespace(is_success=True))
    with phone.store._lock,phone.store._connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM phone_context_consumption WHERE call_id=?",
            (phone.call_id,),
        ).fetchone()[0]==0
        assert connection.execute(
            "SELECT COUNT(*) FROM phone_context_consumption WHERE call_id=?",
            (historic_call,),
        ).fetchone()[0]==1

    await phone.manager.append_fragment_once(
        phone.binding,Fragment("tail","assistant","Keep the final phone fact",100,130)
    )
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",
            (phone.call_id,),
        )
    ordinary=turn_item(phone,"post-phone")
    ordinary_sections,ordinary_audit=prepare_turn(runtime,ordinary,incremental=True)
    ordinary_text="\n".join(section[1] for section in ordinary_sections)
    assert "phone_action_context" in ordinary_audit["removed_section_keys"]
    assert "Keep the earlier phone fact" in ordinary_text
    assert "Run the phone action" in ordinary_text
    assert "Keep the final phone fact" in ordinary_text
    assert "provider_event_id" not in ordinary_text and "call_id" not in ordinary_text
    settled=transport(ordinary,ordinary_sections,ordinary_audit["removed_section_keys"])
    assert "PHONE ACTION CONTEXT" not in settled.materialized_prompt
    assert "legacy-event" not in settled.materialized_prompt
    assert "Remember the earlier phone history" in settled.materialized_prompt
    assert "Keep the earlier phone fact" in settled.materialized_prompt
    assert "Keep the final phone fact" in settled.materialized_prompt
    coordinator.complete(settled,assistant_text="settled")
    commit_success(ordinary,SimpleNamespace(is_success=True))
    later_sections,_=prepare_turn(
        runtime,turn_item(phone,"post-phone-later"),incremental=True
    )
    assert later_sections==[]


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

@pytest.mark.asyncio
async def test_phone_action_includes_unconsumed_ended_call_without_migration(phone):
    from orchestrator.phone_context_handoff import prepare_turn,commit_success

    historic_call=f"{phone.call_id}-previous"
    historic_binding=replace(
        phone.binding,call_id=historic_call,provider_session_id="prov-previous"
    )
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("""INSERT INTO live_calls(
            call_id,owner_id,session_id,agent_id,instance_id,instance_generation,
            context_generation,call_epoch,provider_session_id,phase,
            controller_lease,lease_expiry,started_at,max_ends_at,ended_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'ended',?,?,?,?,?)""",(
            historic_call,phone.owner_id,phone.session_id,phone.agent_id,"HASHI","1",
            1,1,"prov-previous","lease-previous","2099-01-01T00:00:00Z",
            "2026-10-03T09:00:00Z","2099-01-01T00:00:00Z",
            "2026-10-03T09:01:00Z",
        ))
    await phone.manager.append_fragment_once(
        historic_binding,
        Fragment("previous-constraint","user","Only operate on the test environment",0,40),
    )

    runtime=turn_runtime(phone)
    runtime.backend_manager.current_backend.current_pcm_section_keys=lambda: ()
    runtime.backend_manager.current_backend.current_pcm_sections=lambda _keys: {}
    phone.judgments=[decision(action("query","Run the requested operation"))]
    await speak(phone,"Run the requested operation",start=50,end=100,source="action")
    proposal=phone.admitted_proposals[-1]
    item=turn_item(phone,"second-call-action",{"owner_id":phone.owner_id,"live_voice":{
        "call_id":phone.call_id,"call_epoch":1,"delegation_id":proposal.delegation_id,
        "proposal_version":proposal.version,"proposal_digest":proposal.digest,
        "phone_context_handoff_id":proposal.phone_context_handoff_id}})
    sections,_=prepare_turn(runtime,item,incremental=True)
    rendered="\n".join(section[1] for section in sections)
    assert "Only operate on the test environment" in rendered
    assert "Run the requested operation" in rendered

    commit_success(item,SimpleNamespace(is_success=True))
    with phone.store._lock,phone.store._connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM phone_context_consumption WHERE call_id=?",
            (historic_call,),
        ).fetchone()[0]==1
        assert connection.execute(
            "SELECT COUNT(*) FROM phone_context_consumption WHERE call_id=?",
            (phone.call_id,),
        ).fetchone()[0]==0

    fixed_keys={"phone_action_context"}
    runtime.backend_manager.current_backend.current_pcm_section_keys=lambda: tuple(
        fixed_keys
    )
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-27T10:10:00Z' "
            "WHERE call_id=?",
            (phone.call_id,),
        )
    ordinary=turn_item(phone,"after-second-call")
    ordinary_sections,ordinary_audit=prepare_turn(runtime,ordinary,incremental=True)
    ordinary_text="\n".join(section[1] for section in ordinary_sections)
    assert "Only operate on the test environment" not in ordinary_text
    assert ordinary_text.count("Run the requested operation")==1
    assert "phone_action_context" in ordinary_audit["removed_section_keys"]
    fixed_keys.difference_update(ordinary_audit["removed_section_keys"])
    commit_success(ordinary,SimpleNamespace(is_success=True))
    later_sections,later_audit=prepare_turn(
        runtime,turn_item(phone,"after-second-call-later"),incremental=True
    )
    assert later_sections==[]
    assert "phone_action_context" not in later_audit["removed_section_keys"]


@pytest.mark.asyncio
async def test_grouped_legacy_phone_section_migrates_after_exact_source_validation(phone):
    from orchestrator.phone_context_handoff import _fragments,prepare_turn

    await phone.manager.append_fragment_once(
        phone.binding,
        Fragment("grouped-source", "user", "Preserve grouped legacy history", 0, 30),
    )
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-27T10:20:00Z' "
            "WHERE call_id=?",
            (phone.call_id,),
        )
        fragments=_fragments(
            connection,owner_id=phone.owner_id,agent_id=phone.agent_id,
            session_id=phone.session_id,context_generation=1,
            call_id=phone.call_id,call_epoch=1,ended_only=True,
        )
    legacy_key,legacy_section=packed_legacy_phone_section(phone,fragments)
    stored_section={
        "key":legacy_key,"title":legacy_section[0],"text":legacy_section[1],
        "authority":"history","protected":True,
        "content_sha256":legacy_section[2]["content_sha256"],
    }
    runtime=turn_runtime(phone)
    runtime.backend_manager.current_backend.current_pcm_section_keys=lambda: (legacy_key,)
    runtime.backend_manager.current_backend.current_pcm_sections=lambda _keys: {
        legacy_key:stored_section
    }

    sections,audit=prepare_turn(
        runtime,turn_item(phone,"grouped-legacy-migration"),incremental=True
    )
    assert legacy_key in audit["removed_section_keys"]
    assert "Preserve grouped legacy history" in "\n".join(
        section[1] for section in sections
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption",["hash","scope","event"])
async def test_invalid_grouped_legacy_phone_section_is_preserved(phone,corruption):
    from orchestrator.phone_context_handoff import PhoneContextError,_fragments,prepare_turn

    await phone.manager.append_fragment_once(
        phone.binding,
        Fragment("valid-source", "user", "The durable source", 0, 30),
    )
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-27T10:30:00Z' "
            "WHERE call_id=?",
            (phone.call_id,),
        )
        fragments=_fragments(
            connection,owner_id=phone.owner_id,agent_id=phone.agent_id,
            session_id=phone.session_id,context_generation=1,
            call_id=phone.call_id,call_epoch=1,ended_only=True,
        )
    legacy_key,legacy_section=packed_legacy_phone_section(phone,fragments)
    prefix,encoded=legacy_section[1].split("\n",1)
    corrupt=json.loads(encoded)
    if corruption=="scope":
        corrupt["scope"]["session_id"]="other-session"
    elif corruption=="event":
        corrupt["fragment_groups"][0]["fragments"][0]["event"][0]="missing-source"
    corrupt_body=prefix+"\n"+json.dumps(
        corrupt,ensure_ascii=False,separators=(",", ":")
    )
    stored={legacy_key:{
        "key":legacy_key,"title":legacy_section[0],"text":corrupt_body,
        "authority":"history","protected":True,
        "content_sha256":(("0"*64) if corruption=="hash" else
            hashlib.sha256(corrupt_body.encode()).hexdigest()),
    }}
    runtime=turn_runtime(phone)
    runtime.backend_manager.current_backend.current_pcm_section_keys=lambda: tuple(stored)
    runtime.backend_manager.current_backend.current_pcm_sections=lambda keys: {
        key:stored[key] for key in keys
    }
    item=turn_item(phone,"invalid-grouped-legacy")

    with pytest.raises(PhoneContextError) as raised:
        prepare_turn(runtime,item,incremental=True)
    assert raised.value.code=="phone_context_legacy_source_incomplete"
    assert tuple(stored)==(legacy_key,)
    assert not hasattr(item,"_phone_context_handoff")


@pytest.mark.asyncio
async def test_legacy_phone_section_without_exact_source_fails_safe(phone):
    from orchestrator.phone_context_handoff import PhoneContextError,prepare_turn

    missing=[{
        "call_id":"missing-call","call_epoch":1,"provider_event_id":"missing-event",
        "sequence":1,"role":"user","text":"Missing durable source",
        "start_ms":0,"end_ms":10,"source":"durable_phone_transcript",
    }]
    legacy_key,legacy_section=legacy_phone_section(phone,missing)
    runtime=turn_runtime(phone)
    runtime.backend_manager.current_backend.current_pcm_section_keys=lambda: (legacy_key,)
    runtime.backend_manager.current_backend.current_pcm_sections=lambda _keys: {
        legacy_key:{
            "key":legacy_key,"title":legacy_section[0],"text":legacy_section[1],
            "authority":"history","protected":True,
        }
    }
    item=turn_item(phone,"missing-legacy-source")
    with pytest.raises(PhoneContextError) as raised:
        prepare_turn(runtime,item,incremental=True)
    assert raised.value.code=="phone_context_legacy_source_incomplete"
    assert not hasattr(item,"_phone_context_handoff")


@pytest.mark.asyncio
async def test_semantic_phone_section_without_complete_source_fails_safe(phone):
    from orchestrator.phone_context_handoff import (
        PhoneContextError,_fragment_digest,_fragments,_semantic_transcript,prepare_turn,
    )

    await phone.manager.append_fragment_once(
        phone.binding,Fragment("available-source","user","Available durable source",0,20)
    )
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",
            (phone.call_id,),
        )
        available=_fragments(connection,owner_id=phone.owner_id,
            agent_id=phone.agent_id,session_id=phone.session_id,context_generation=1,
            call_id=phone.call_id,call_epoch=1,ended_only=True)
    missing={
        **available[0],"provider_event_id":"missing-semantic-source",
        "sequence":available[0]["sequence"] + 1,"text":"Missing durable tail",
        "start_ms":30,"end_ms":40,
    }
    expected=[available[0],missing]
    semantic_key=f"recent_exchange:phone:{_fragment_digest(expected)}"
    semantic_section={
        "key":semantic_key,"title":"RECENT COMPLETED EXCHANGE",
        "text":_semantic_transcript(expected),"authority":"history","protected":False,
        "metadata":{"sequence":available[0]["sequence"]},
    }
    runtime=turn_runtime(phone)
    runtime.backend_manager.current_backend.current_pcm_section_keys=lambda: (semantic_key,)
    runtime.backend_manager.current_backend.current_pcm_sections=lambda _keys: {
        semantic_key:semantic_section
    }
    item=turn_item(phone,"missing-semantic-source")
    with pytest.raises(PhoneContextError) as raised:
        prepare_turn(runtime,item,incremental=True)
    assert raised.value.code=="phone_context_legacy_source_incomplete"
    assert not hasattr(item,"_phone_context_handoff")


@pytest.mark.asyncio
async def test_verified_semantic_phone_section_migrates_to_dialogue_units(phone):
    from orchestrator.phone_context_handoff import (
        _fragments,_ordinary_history_section,prepare_turn,
    )

    for fragment in (
        Fragment("old-u1","user","Only use test.",0,20),
        Fragment("old-a1","assistant","Understood.",30,50),
        Fragment("old-u2","user","Inspect status.",60,80),
        Fragment("old-a2","assistant","Status inspected.",90,110),
    ):
        await phone.manager.append_fragment_once(phone.binding,fragment)
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",
            (phone.call_id,),
        )
        fragments=_fragments(connection,owner_id=phone.owner_id,
            agent_id=phone.agent_id,session_id=phone.session_id,context_generation=1,
            call_id=phone.call_id,call_epoch=1,ended_only=True)
    title,text_value,metadata=_ordinary_history_section(fragments)
    semantic_key=metadata["key"]
    runtime=turn_runtime(phone)
    runtime.backend_manager.current_backend.current_pcm_section_keys=lambda: (semantic_key,)
    runtime.backend_manager.current_backend.current_pcm_sections=lambda _keys: {
        semantic_key:{
            "key":semantic_key,"title":title,"text":text_value,
            "authority":"history","protected":False,
            "metadata":{"sequence":metadata["sequence"]},
        }
    }

    sections,audit=prepare_turn(
        runtime,turn_item(phone,"verified-semantic-migration"),incremental=True
    )
    assert len(sections)==2
    assert semantic_key in audit["removed_section_keys"]
    assert "Only use test." in sections[0][1]
    assert "Inspect status." in sections[1][1]


@pytest.mark.asyncio
async def test_existing_semantic_v2_matches_identity_when_events_arrive_out_of_order(phone):
    from orchestrator.phone_context_handoff import (
        _fragments,_ordinary_history_section,prepare_turn,
    )

    await phone.manager.append_fragment_once(
        phone.binding,
        Fragment("answer-first", "assistant", "Acknowledged.", 40, 60),
    )
    await phone.manager.append_fragment_once(
        phone.binding,
        Fragment("question-later", "user", "Use the test environment.", 0, 20),
    )
    with phone.store._lock,phone.store._connection() as connection:
        original=_fragments(
            connection,owner_id=phone.owner_id,agent_id=phone.agent_id,
            session_id=phone.session_id,context_generation=1,
            call_id=phone.call_id,call_epoch=1,
        )
    title,text_value,metadata=_ordinary_history_section(original)
    semantic_key=metadata["key"]

    await phone.manager.append_fragment_once(
        phone.binding,
        Fragment("late-middle", "user", " Late clarification.", 20, 30),
    )
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-27T10:40:00Z' "
            "WHERE call_id=?",
            (phone.call_id,),
        )
    stored_section={
        "key":semantic_key,"title":title,"text":text_value,
        "authority":"history","protected":False,
        "metadata":{"sequence":metadata["sequence"]},
    }
    runtime=turn_runtime(phone)
    runtime.backend_manager.current_backend.current_pcm_section_keys=lambda: (
        semantic_key,
    )
    runtime.backend_manager.current_backend.current_pcm_sections=lambda _keys: {
        semantic_key:stored_section
    }

    sections,audit=prepare_turn(
        runtime,turn_item(phone,"semantic-v2-out-of-order"),incremental=True
    )
    rendered="\n".join(section[1] for section in sections)
    assert semantic_key in audit["removed_section_keys"]
    assert "Use the test environment. Late clarification." in rendered
    assert "Acknowledged." in rendered


def test_semantic_v2_identity_match_handles_long_fragmented_dialogue():
    from orchestrator.phone_context_handoff import (
        _fragment_digest,_semantic_source_matches,_semantic_transcript,
    )

    fragments=[{
        "call_id":"long-call","call_epoch":1,
        "provider_event_id":f"word-{index:04d}","sequence":index+1,
        "role":"user","text":f"[{index:04d}]",
        "start_ms":index*10,"end_ms":index*10+5,
        "source":"durable_phone_transcript",
    } for index in range(1200)]
    key=f"recent_exchange:phone:{_fragment_digest(fragments)}"
    section={"text":_semantic_transcript(fragments),"metadata":{"sequence":1}}

    assert _semantic_source_matches(key,section,fragments)


@pytest.mark.asyncio
async def test_consumed_phone_fragments_are_not_reparsed_on_later_turn(phone):
    from orchestrator.phone_context_handoff import prepare_turn,commit_success

    for i in range(120):
        await phone.manager.append_fragment_once(
            phone.binding,Fragment(
                f"consumed-{i:04d}","user" if i % 2 else "assistant",
                f"old-{i:04d}",i * 20,i * 20 + 10,
            ),
        )
    with phone.store._lock,phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-04T12:00:00Z' WHERE call_id=?",
            (phone.call_id,),
        )
    runtime=turn_runtime(phone)
    runtime.backend_manager.current_backend.current_pcm_section_keys=lambda: ()
    runtime.backend_manager.current_backend.current_pcm_sections=lambda _keys: {}
    first_item=turn_item(phone,"consume-old-phone-history")
    first_sections,_=prepare_turn(runtime,first_item,incremental=True)
    assert first_sections
    commit_success(first_item,SimpleNamespace(is_success=True))

    with phone.store._lock,phone.store._connection() as connection:
        connection.execute("""UPDATE run_events SET detail_json='not-json'
            WHERE event_id IN (
                SELECT event_id FROM live_fragments
                WHERE call_id=? AND provider_event_id LIKE 'consumed-%'
            )""",(phone.call_id,))
    await phone.manager.append_fragment_once(
        phone.binding,Fragment("new-tail","assistant","Only parse this new tail",3000,3010)
    )

    second_sections,second_audit=prepare_turn(
        runtime,turn_item(phone,"incremental-tail"),incremental=True
    )
    assert len(second_sections)==1
    assert "Only parse this new tail" in second_sections[0][1]
    assert second_audit["requested_fragments"]==1
    assert second_audit["included_fragments"]==1
