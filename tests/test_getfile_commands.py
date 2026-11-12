"""Production command dispatcher, real runtime and persistence; no Engine work."""
import hashlib
import json
from types import SimpleNamespace as NS
import pytest
from orchestrator.config import GlobalConfig, FlexibleAgentConfig
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.command_interaction_bridge import dispatch_command_interaction
from orchestrator import runtime_session


@pytest.fixture
def runtime(tmp_path):
    home=tmp_path/"home";home.mkdir()
    work=home/"workspaces"/"alice";work.mkdir(parents=True)
    (work/"agent.md").write_text("Synthetic command acceptance")
    config=FlexibleAgentConfig(name="alice",workspace_dir=work,system_md=work/"agent.md",
        telegram_token_key="",allowed_backends=[{"engine":"codex-cli","model":"gpt-5.6-sol"}],
        active_backend="codex-cli",default_mode="fixed",project_root=home,extra={})
    global_config=GlobalConfig(authorized_id=7,bridge_home=home,project_root=home,
        instance_id="GETFILE-QA",base_logs_dir=home/"logs",base_media_dir=home/"media")
    value=FlexibleAgentRuntime(config,global_config,"123456:synthetic-getfile-offline",{})
    yield value
    value.canonical_audit_buffer.close()


@pytest.mark.asyncio
async def test_open_menu_click_and_explicit_path_execute_no_llm_and_preserve_bytes(runtime):
    path=runtime.workspace_dir/"named report.txt";path.write_bytes(b"source\x00bytes")
    runtime.canonical_audit.record("tool_call",{"tool_name":"file_write","is_error":False,
        "arguments":{"path":str(path)}},provenance={"workspace_dir":str(runtime.workspace_dir),
        "access_roots":[str(runtime.workspace_dir)],"owner_id":"user:7"})
    session=runtime_session.ensure_store(runtime).resolve_primary_session(owner_id="user:7",agent_id="alice",establish=True)
    metadata={"instance_id":"GETFILE-QA","actor_id":7,"session_id":session["session_id"],
        "context_generation":1,"owner_id":"user:7","connection_binding":"connectionabcdefghijkl",
        "session_surface":"workbench","session_channel_key":"default"}
    def payload(op,request,**extra):
        return {"version":1,"op":op,"client_id":"clientabcdefghijkl","request_id":request,
                "ui_locale":"en",**extra}
    opened=await dispatch_command_interaction(runtime,payload("open","request-open-menu",command="/getfile"),metadata)
    assert opened["ok"],opened
    assert "Recent 10 distinct files" in opened["messages"][-1]["text"]
    assert "getfile.coverage" not in opened["messages"][-1]["text"]
    menu=opened["messages"][-1]["command_ui"]
    assert menu["rows"][0][0]["disabled"] is False
    assert menu["rows"][-1][0]["text_input"]["max_length"]==500
    from orchestrator.chat_transcript_projection import build_chat_projection
    projection=build_chat_projection(runtime.session_store,
        session=runtime.session_store.get_session(session["session_id"],owner_id="user:7"),
        owner_id="user:7",include_command_ui=True)
    persisted=next(row["command_ui"] for row in projection["messages"] if row.get("command_ui"))
    assert persisted["rows"][-1][0]["text_input"]==menu["rows"][-1][0]["text_input"]
    assert str(path) not in json.dumps(menu)
    action=payload("act","request-click-file",menu_id=menu["menu_id"],revision=menu["revision"],
                   button_id=menu["rows"][0][0]["button_id"])
    clicked=await dispatch_command_interaction(runtime,action,metadata)
    assert clicked["ok"],clicked
    assert clicked["result"]["getfile"]["sha256"]==hashlib.sha256(path.read_bytes()).hexdigest()
    replay=await dispatch_command_interaction(runtime,action,metadata)
    assert replay["result"]["getfile"]["message_id"]==clicked["result"]["getfile"]["message_id"]
    direct=await dispatch_command_interaction(runtime,payload("open","request-path-file",command='/getfile "'+str(path)+'"'),metadata)
    assert direct["ok"] and direct["result"]["getfile"]["sha256"]==hashlib.sha256(path.read_bytes()).hexdigest(),direct
    assert not runtime_session.ensure_store(runtime).recent_session_runs(session["session_id"],owner_id="user:7")
    assert runtime.queue.empty()
