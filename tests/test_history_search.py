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
    assert literal_candidate("x" * 257)(row("prefix " + "x" * 257))
    for invalid in ("", "\x00", "\ud800", [], float("nan")):
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
        from orchestrator.history_search import history_search_limits
        assert (await client.post(url, data=" " * (history_search_limits()["max_request_bytes"] + 1),
            headers={"Content-Type": "application/json"})).status == 413
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



@pytest.mark.asyncio
async def test_departed_agent_history_is_discoverable_and_readable_only_by_its_owner(native, tmp_path):
    store, _ = native
    moved = store.ensure_default_session(owner_id="user:7", agent_id="moved")
    foreign = store.ensure_default_session(owner_id="other", agent_id="secret-only")
    for session in (moved, foreign):
        store.append_presentation_message(
            session_id=session["session_id"], owner_id=session["owner_id"],
            agent_id=session["agent_id"], role="assistant", text="retained needle",
            source="fixture", idempotency_key="retained", history_eligible=True,
        )
    api = WorkbenchApiServer.__new__(WorkbenchApiServer)
    api.session_store = store
    api.global_config = SimpleNamespace(instance_id="HISTORY-SEARCH")
    api.admin_token = "synthetic-only"
    api.config_path = tmp_path / "agents.json"
    api._runtime_map = lambda: {"alice": object(), "bob": object()}
    api._v1_owner_id = lambda request: None if request.headers.get("Unauth") else request.headers.get("Test-Owner", "user:7")
    app = web.Application()
    app.router.add_get("/api/v1/session-history/agents", api.handle_v1_history_agents)
    app.router.add_get("/api/v1/agents/{agent_id}/history", api.handle_v1_agent_history)
    app.router.add_post("/api/v1/agents/{agent_id}/history/search", api.handle_v1_agent_history_search)
    app.router.add_get("/api/v1/sessions/{session_id}/messages", api.handle_v1_session_messages)
    async with TestClient(TestServer(app)) as client:
        cursor, discovered = None, []
        while True:
            params = {"limit": "1"}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get("/api/v1/session-history/agents", params=params)
            assert response.status == 200
            assert "no-store" in response.headers["Cache-Control"]
            page = await response.json()
            discovered.extend(page["agents"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert discovered == ["alice", "bob", "moved"]
        other = await (await client.get("/api/v1/session-history/agents", headers={"Test-Owner": "other"})).json()
        assert other["agents"] == ["alice", "secret-only"]
        assert (await client.get("/api/v1/session-history/agents", headers={"Unauth": "1"})).status == 401
        assert (await client.get("/api/v1/session-history/agents", params={"limit": "201"})).status == 400

        history = await client.get("/api/v1/agents/moved/history")
        assert history.status == 200
        assert [row["text"] for row in (await history.json())["messages"]] == ["retained needle"]
        search = await client.post("/api/v1/agents/moved/history/search", json={"query": "needle"})
        assert search.status == 200
        assert [row["text"] for row in (await search.json())["messages"]] == ["retained needle"]
        messages = await client.get(f"/api/v1/sessions/{moved['session_id']}/messages")
        assert messages.status == 200
        assert [row["text"] for row in (await messages.json())["messages"]] == ["retained needle"]
        for url, method in (
            ("/api/v1/agents/moved/history", "get"),
            ("/api/v1/agents/moved/history/search", "post"),
        ):
            kwargs = {"headers": {"Test-Owner": "other"}}
            if method == "post":
                kwargs["json"] = {"query": "needle"}
            assert (await getattr(client, method)(url, **kwargs)).status == 404
        assert (await client.get(
            f"/api/v1/sessions/{moved['session_id']}/messages",
            headers={"Test-Owner": "other"},
        )).status == 404



@pytest.mark.asyncio
async def test_workbench_search_reads_retained_history_over_isolated_http(tmp_path):
    """Exercise real Workbench search against the local Session API fixture."""
    import asyncio
    import os
    from pathlib import Path

    node = os.environ.get("HASHI_TEST_NODE_EXE")
    workbench = os.environ.get("HASHI_TEST_WORKBENCH_ROOT")
    if not node or not workbench:
        pytest.skip("set synthetic integration Node and Workbench checkout paths")
    module = (Path(workbench) / "server" / "chat_search_service.js").resolve()
    assert module.is_file()
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="ISOLATED")
    retained = store.ensure_default_session(owner_id="user:7", agent_id="moved")
    foreign = store.ensure_default_session(owner_id="user:8", agent_id="private")
    for session in (retained, foreign):
        store.append_presentation_message(
            session_id=session["session_id"], owner_id=session["owner_id"],
            agent_id=session["agent_id"], role="assistant", text="needle " + session["agent_id"],
            source="synthetic", idempotency_key="retained", history_eligible=True,
        )
    long_query = "\U0001f33f" * 600 + " end"
    store.append_presentation_message(
        session_id=retained["session_id"], owner_id="user:7", agent_id="moved",
        role="assistant", text="retained " + long_query, source="synthetic",
        idempotency_key="long-retained", history_eligible=True,
    )
    api = WorkbenchApiServer.__new__(WorkbenchApiServer)
    api.session_store = store
    api.global_config = SimpleNamespace(instance_id="ISOLATED")
    api.admin_token = "fixture-token"
    api.config_path = tmp_path / "agents.json"
    api._runtime_map = lambda: {}
    api._v1_owner_id = lambda request: request.headers.get("X-Test-Owner")

    @web.middleware
    async def synthetic_auth(request, handler):
        if request.headers.get("Authorization") != "Bearer fixture-token":
            return web.json_response({"ok": False}, status=401)
        return await handler(request)

    async def agents(_request):
        return web.json_response({"agents": []})

    async def capabilities(_request):
        return web.json_response({
            "session_api_version": "1.0", "agent_history_api_version": "1.0",
            "session_history_agents": {"version": "1.0"},
            "agent_history_search": {
                "version": "1.0", "candidate_filter": "literal_superset_v1", "method": "POST",
                "max_query_characters": 16384,
            },
        })

    app = web.Application(middlewares=[synthetic_auth])
    app.router.add_get("/api/agents", agents)
    app.router.add_get("/api/v1/capabilities", capabilities)
    app.router.add_get("/api/v1/session-history/agents", api.handle_v1_history_agents)
    app.router.add_post("/api/v1/agents/{agent_id}/history/search", api.handle_v1_agent_history_search)
    app.router.add_get("/api/v1/sessions/{session_id}", api.handle_v1_session_get)
    app.router.add_get("/api/v1/sessions/{session_id}/messages", api.handle_v1_session_messages)
    script = r"""
      import assert from 'node:assert/strict';
      const { ChatSearchService } = await import(process.argv[1]);
      const base = new URL(process.argv[2]);
      const registry = { active: () => ({id:'c',enabled:true,remote_url:'https://synthetic.invalid'}),
        sharedToken: () => 'fixture-token' };
      const gateway = { fromInstance:'WB', async request(path, init) {
        const url = new URL(path, base);
        assert.equal(url.origin, base.origin);
        return fetch(url, {...init, headers:{...init.headers, Authorization:'Bearer fixture-token',
          'X-Test-Owner':'user:7'}});
      } };
      const service = new ChatSearchService({ registry, gateway, pageBudget:50,
        readProjection: async () => { throw new Error('no live Agent expected'); } });
      const longQuery = String.fromCodePoint(0x1f33f).repeat(600) + ' end';
      let result = await service.start({connection_id:'c',query:longQuery});
      while (!result.done) result = await service.advance('c', result.search_id);
      assert.equal(result.coverage.complete, true);
      assert.deepEqual(result.results.map(row=>row.agent_id), ['moved']);
      const context = await service.context('c',result.search_id,result.results[0].result_id);
      assert.equal(context.read_only,true);
      assert.ok(context.messages.some(row=>row.text==='retained ' + longQuery));
      service.dispose(result.search_id);
      process.stdout.write('isolated search and read succeeded');
    """
    async with TestClient(TestServer(app)) as client:
        process = await asyncio.create_subprocess_exec(
            node, "--input-type=module", "-e", script, module.as_uri(),
            str(client.make_url("/")), cwd=str(Path(workbench)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, stderr.decode("utf8", errors="replace")
        assert stdout.decode("utf8") == "isolated search and read succeeded"


def test_history_query_selected_budget_is_explicit_and_not_truncated(monkeypatch):
    from orchestrator.history_search import history_search_limits
    monkeypatch.setenv("HASHI_HISTORY_SEARCH_MAX_QUERY_CHARS", "4")
    assert history_search_limits()["max_query_characters"] == 4
    assert literal_candidate("abcd")(row("prefix abcde"))
    with pytest.raises(ValueError, match="configured limit of 4"):
        literal_candidate("abcde")
    monkeypatch.setenv("HASHI_HISTORY_SEARCH_MAX_QUERY_CHARS", "invalid")
    with pytest.raises(ValueError, match="positive integer"):
        history_search_limits()


@pytest.mark.asyncio
async def test_long_unicode_literal_http_pages_preserve_query_and_owner(native, tmp_path, monkeypatch):
    from orchestrator import history_search
    store, own = native
    monkeypatch.setenv("HASHI_HISTORY_SEARCH_MAX_QUERY_CHARS", "1024")
    monkeypatch.setattr(history_search, "MAX_SCAN_ROWS", 2)
    query = "\U0001f33f" * 600 + " end"
    store.append_presentation_message(
        session_id=own["session_id"], owner_id="user:7", agent_id="alice",
        role="assistant", text="owned " + query, source="fixture",
        idempotency_key="long-match", history_eligible=True,
    )
    for index in range(4):
        store.append_presentation_message(
            session_id=own["session_id"], owner_id="user:7", agent_id="alice",
            role="assistant", text="newer unrelated " + str(index), source="fixture",
            idempotency_key="long-gap-" + str(index), history_eligible=True,
        )
    api = WorkbenchApiServer.__new__(WorkbenchApiServer)
    api.session_store = store
    api.global_config = SimpleNamespace(instance_id="HISTORY-SEARCH")
    api.admin_token = "synthetic-only"
    api.config_path = tmp_path / "agents.json"
    api._runtime_map = lambda: {"alice": object()}
    api._v1_owner_id = lambda request: request.headers.get("Test-Owner", "user:7")
    app = web.Application()
    app.router.add_post("/api/v1/agents/{agent_id}/history/search", api.handle_v1_agent_history_search)
    async with TestClient(TestServer(app)) as client:
        url = "/api/v1/agents/alice/history/search"
        first = await client.post(url, json={"query": query, "limit": 20})
        assert first.status == 200
        page = await first.json()
        assert page["messages"] == [] and page["next_cursor"]
        cursor = page["next_cursor"]
        changed = await client.post(url, json={"query": query + "!", "cursor": cursor})
        assert changed.status == 400
        foreign = await client.post(url, json={"query": query, "cursor": cursor}, headers={"Test-Owner": "other"})
        assert foreign.status == 400
        found = []
        while not page["history_complete"]:
            response = await client.post(url, json={"query": query, "cursor": cursor})
            assert response.status == 200
            page = await response.json()
            found.extend(page["messages"])
            cursor = page["next_cursor"]
        assert len(found) == 1 and found[0]["text"] == "owned " + query
        too_long = await client.post(url, json={"query": "x" * 1025})
        assert too_long.status == 400
