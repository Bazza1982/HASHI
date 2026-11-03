"""Native search candidates preserve scopes, display ambiguity and scan cursors."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.history_search import collect_candidates, literal_candidate
from orchestrator.session_store import SessionStore
from orchestrator.workbench_api import WorkbenchApiServer


def row(text, *, display=None, content=None):
    return {"text": text, "display_text": display, "content_json":json.dumps(content or []), "message_context_json":"{}"}


def test_literal_candidate_is_a_superset_of_renderer_display_matches():
    for query, raw in [
        ("NEEDLE", row("some needle")),
        ("你好", row("问一下你好")),
        ("k", row("K")),
        ("s", row("ſ")),
        ("𐐨", row("𐐀")),
        ("needle", row("hidden", display="visible NEEDLE")),
        ("hello world", row("hello <b>world</b>")),
        ("&", row("&amp;")),
        ("ABC", row("&#65;&#66;&#67;")),
        ("image-caption", row("placeholder",content=[{"type":"attachment","attachment_id":"media"}])),
        ("[x].*", row("literal [x].*")),
    ]:
        assert literal_candidate(query)(raw)
    assert not literal_candidate("needle")(row("ordinary"))
    assert not literal_candidate("never")(row("plain", display="override"))
    for invalid in ("", "x"*257, "\x00", [], float("nan")):
        with pytest.raises(ValueError):
            literal_candidate(invalid)


def test_scan_limit_and_byte_limit_resume_after_examined_row_without_skipping(monkeypatch):
    from orchestrator import history_search
    rows = [dict(row("ordinary" if i<4 else "needle"), ordinal=i) for i in range(8)]
    monkeypatch.setattr(history_search,"MAX_SCAN_ROWS",4)
    matches, last, more, scanned = collect_candidates(iter(rows),query="needle",limit=200)
    assert matches == [] and last["ordinal"] == 3 and more and scanned == 4
    matches, last, more, scanned = collect_candidates(iter(rows[4:]),query="needle",limit=2)
    assert [r["ordinal"] for r in matches] == [4,5] and last["ordinal"] == 5 and more
    monkeypatch.setattr(history_search,"MAX_SCAN_BYTES",1)
    matches,last,more,scanned = collect_candidates(iter(rows),query="needle",limit=200)
    assert last["ordinal"] == 0 and more and scanned == 1


@pytest.fixture
def native(tmp_path):
    store=SessionStore(tmp_path/"sessions.sqlite3",instance_id="HISTORY-SEARCH")
    own=store.ensure_default_session(owner_id="user:7",agent_id="alice")
    foreign=store.ensure_default_session(owner_id="other",agent_id="alice")
    bob=store.ensure_default_session(owner_id="user:7",agent_id="bob")
    for session in (own,foreign,bob):
        for index in range(6):
            store.append_presentation_message(session_id=session["session_id"],owner_id=session["owner_id"],
                agent_id=session["agent_id"],role="assistant",text="needle "+str(index),
                source="synthetic",idempotency_key=str(index),history_eligible=True)
    return store,own


def test_native_filtered_page_has_no_binding_writes_and_exact_scope(native):
    store,own=native
    before_binding=store.resolve_primary_session(owner_id="user:7",agent_id="alice")
    first=store.agent_history_page(owner_id="user:7",agent_id="alice",candidate_query="needle",limit=2)
    second=store.agent_history_page(owner_id="user:7",agent_id="alice",candidate_query="needle",
        before=first["next_boundary"],limit=2)
    assert len(first["messages"])==len(second["messages"])==2
    assert {r["message_id"] for r in first["messages"]}.isdisjoint({r["message_id"] for r in second["messages"]})
    assert all(r["session_id"]==own["session_id"] for r in first["messages"]+second["messages"])
    assert store.resolve_primary_session(owner_id="user:7",agent_id="alice")==before_binding
    empty=store.agent_history_page(owner_id="user:7",agent_id="alice",candidate_query="absent")
    assert empty["messages"]==[] and empty["history_complete"]


async def test_actual_search_http_binds_cursor_to_query_owner_agent_and_protocol(native,tmp_path):
    store,own=native
    api=WorkbenchApiServer.__new__(WorkbenchApiServer)
    api.session_store=store
    api.global_config=SimpleNamespace(instance_id="HISTORY-SEARCH")
    api.admin_token="synthetic-only"
    api.config_path=tmp_path/"agents.json"
    api._runtime_map=lambda: {"alice":object(),"bob":object()}
    api._v1_owner_id=lambda request: None if request.headers.get("Unauth") else request.headers.get("Test-Owner","user:7")
    app=web.Application()
    app.router.add_post("/api/v1/agents/{agent_id}/history/search",api.handle_v1_agent_history_search)
    async with TestClient(TestServer(app)) as client:
        url="/api/v1/agents/alice/history/search"
        response=await client.post(url,json={"query":"needle","limit":2})
        assert response.status==200 and "no-store" in response.headers["Cache-Control"]
        first=await response.json()
        assert len(first["messages"])==2 and first["next_cursor"]
        assert first["candidate_filter_version"]==1
        assert all(r["session_id"]==own["session_id"] for r in first["messages"])
        for path,body,headers in [
            (url,{"query":"other query","cursor":first["next_cursor"]},{}),
            (url,{"query":"needle","cursor":first["next_cursor"]},{"Test-Owner":"other"}),
            (url.replace("alice","bob"),{"query":"needle","cursor":first["next_cursor"]},{}),
            (url,{"query":"needle","owner_id":"other"},{}),
            (url,{"query":"needle","limit":True},{}),
        ]:
            denied=await client.post(path,json=body,headers=headers)
            assert denied.status==400
        assert (await client.post(url,json={"query":"needle"},headers={"Unauth":"1"})).status==401
        assert (await client.post(url,data='{"query":"needle","query":"other"}',
            headers={"Content-Type":"application/json"})).status==400
        assert (await client.post(url,data=" "*4097,headers={"Content-Type":"application/json"})).status==413
        with pytest.raises(ValueError):
            api._decode_agent_history_cursor(first["next_cursor"],owner_id="user:7",agent_id="alice")

@pytest.mark.asyncio
async def test_same_eight_session_5552_message_http_search_reduces_reads_without_losing_hits(tmp_path,record_testsuite_property):
    import time
    from datetime import datetime,timedelta,timezone
    store=SessionStore(tmp_path/"large.sqlite3",instance_id="HISTORY-BENCH")
    sessions=[store.create_session(owner_id="user:7",agent_id="alice") for _ in range(8)]
    rows=[]
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    expected=set()
    for group,session in enumerate(sessions):
        for ordinal in range(1,695):
            mid=f"fixture-{group}-{ordinal}"
            text=("独特关键字" if ordinal==1 else "ordinary multilingual 合成内容")+f" {group} {ordinal} "+("x"*600)
            if ordinal==1:
                expected.add(mid)
            rows.append((mid,session["session_id"],ordinal,1,"assistant","user:7","synthetic",
                "{}","[]",text,"visible",1,hashlib.sha256(text.encode()).hexdigest(),
                (start+timedelta(seconds=group*694+ordinal)).isoformat()))
    with store._connection() as connection:
        connection.executemany("""INSERT INTO messages(message_id,session_id,ordinal,context_generation,
            role,author_id,source,message_context_json,content_json,text,visibility,history_eligible,
            content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",rows)
        for session in sessions:
            connection.execute("UPDATE sessions SET next_message_ordinal=695 WHERE session_id=?",(session["session_id"],))
    api=WorkbenchApiServer.__new__(WorkbenchApiServer)
    api.session_store=store
    api.global_config=SimpleNamespace(instance_id="HISTORY-BENCH")
    api.admin_token="synthetic-only"
    api.config_path=tmp_path/"agents.json"
    api._runtime_map=lambda: {"alice":object()}
    api._v1_owner_id=lambda request:"user:7"
    app=web.Application()
    app.router.add_get("/history/{agent_id}",api.handle_v1_agent_history)
    app.router.add_post("/search/{agent_id}",api.handle_v1_agent_history_search)
    evidence={}
    async with TestClient(TestServer(app)) as client:
        for filtered in (False,True):
            cursor=None
            hits=set()
            count=byte_count=source_count=0
            began=time.perf_counter()
            while True:
                if filtered:
                    response=await client.post("/search/alice",json={"query":"独特关键字","limit":200,
                        **({"cursor":cursor} if cursor else {})})
                else:
                    response=await client.get("/history/alice",params={"limit":"200",
                        **({"cursor":cursor} if cursor else {})})
                assert response.status==200
                data=await response.read()
                byte_count+=len(data)
                count+=1
                payload=json.loads(data)
                source_count+=payload.get("scanned_messages",len(payload["messages"]))
                hits.update(message["message_id"] for message in payload["messages"] if "独特关键字" in message["text"])
                if payload["history_complete"]:
                    break
                assert payload["next_cursor"] and payload["next_cursor"]!=cursor
                cursor=payload["next_cursor"]
                assert count<50
            assert hits==expected
            evidence["filtered" if filtered else "unfiltered"]={"requests":count,"bytes":byte_count,
                "scanned_messages":source_count,"seconds":time.perf_counter()-began,"hits":len(hits)}
    assert evidence["unfiltered"]["requests"]==28 and evidence["filtered"]["requests"]==2
    assert evidence["filtered"]["bytes"] < evidence["unfiltered"]["bytes"]/100
    assert evidence["filtered"]["scanned_messages"]==5552
    record_testsuite_property("history_search_8_sessions_5552_messages",json.dumps(evidence))
