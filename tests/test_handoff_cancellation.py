from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_handoff_cancel_uses_retiring_worker_without_opening_other_routes():
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle, FunctionWorkerError
    from orchestrator.function_worker_protocol import FunctionWorkerDisconnected

    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Client:
        agent_name = "momo"

        async def call(self, method, params=None, *, timeout):
            calls.append((method, params))
            if method == "runtime.cancel_session_run":
                entered.set()
                await release.wait()
                return {"ok": True, "status": "cancellation_requested", "terminal": False}
            return {"ok": True}

    client = Client()
    handle = AgentRuntimeHandle(SimpleNamespace(), client, {})
    assert await handle.begin_cutover(shared_handoff=True) is client
    cancel = asyncio.create_task(handle.cancel_session_run_during_handoff(
        owner_id="user:1", session_id="s", run_id="r", request_id="q", reason="stop",
    ))
    await asyncio.wait_for(entered.wait(), 1)
    assert handle._route_inflight == 1
    unrelated = asyncio.create_task(handle._route("runtime.enqueue_request", {}))
    await asyncio.sleep(0)
    assert not unrelated.done()
    assert calls == [("runtime.cancel_session_run", {
        "owner_id": "user:1", "session_id": "s", "run_id": "r",
        "request_id": "q", "reason": "stop",
    })]
    release.set()
    result = await asyncio.wait_for(cancel, 1)
    assert result["status"] == "cancellation_requested" and not result["terminal"]
    assert handle._route_inflight == 0
    await handle.abort_cutover()
    assert await asyncio.wait_for(unrelated, 1) == {"ok": True}

    handle.fence_for_reboot(reject_new=True)
    with pytest.raises(FunctionWorkerError):
        await handle.begin_cutover(shared_handoff=True)
    with pytest.raises(FunctionWorkerDisconnected):
        await handle.cancel_session_run_during_handoff(
            owner_id="user:1", session_id="s", run_id="r", request_id="q", reason="stop",
        )
    await handle.release_reboot_fence()
    await handle.begin_cutover()
    with pytest.raises(FunctionWorkerDisconnected):
        await handle.cancel_session_run_during_handoff(
            owner_id="user:1", session_id="s", run_id="r", request_id="q", reason="stop",
        )
    await handle.abort_cutover()


@pytest.mark.asyncio
async def test_draining_worker_accepts_only_exact_cancel(monkeypatch):
    from orchestrator import runtime_cancel
    from orchestrator.function_worker_host import FunctionWorkerHost, FunctionWorkerStateError

    calls = []
    async def cancel(runtime, **scope):
        calls.append((runtime, scope))
        return {"ok": True, "status": "cancellation_requested", "terminal": False}

    monkeypatch.setattr(runtime_cancel, "cancel_session_run", cancel)
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.phase, host.accepting, host.agent_name = "DRAINING", False, "momo"
    host.runtime = SimpleNamespace()
    params = {"owner_id": "user:1", "session_id": "s", "run_id": "r", "request_id": "q", "reason": "stop"}
    result = await host.handle_request("runtime.cancel_session_run", params)
    assert result["status"] == "cancellation_requested" and not result["terminal"]
    assert calls == [(host.runtime, params)]
    with pytest.raises(FunctionWorkerStateError):
        await host.handle_request("runtime.enqueue_request", {})
    host.phase = "QUIESCED"
    with pytest.raises(FunctionWorkerStateError):
        await host.handle_request("runtime.cancel_session_run", params)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["personal", "enterprise"])
@pytest.mark.parametrize("endpoint", ["session", "request"])
async def test_drain_cancel_is_scoped_tracked_and_sealed(tmp_path, profile, endpoint):
    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.enterprise import IdentityService
    from orchestrator.session_store import SessionStore
    from tests.test_scheduler_run_authorization import setup_personal
    from tests.test_workbench_scheduler_api import _server as scheduler_server

    server, runtime = scheduler_server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    headers = {"X-Workbench-Token": server.admin_token}
    foreign_headers = None
    if profile == "enterprise":
        identity = IdentityService.from_path(tmp_path / "identity.sqlite")
        identity.create_organization(org_id="ORG-001", name="Fixture")
        for name in ("alice", "bob"):
            identity.create_user(org_id="ORG-001", email=f"{name}@example.invalid",
                                 display_name=name, password="fixture-only-password", user_id=name)
        headers = {"Authorization": "Bearer " + identity.create_session(user_id="alice").token}
        foreign_headers = {"Authorization": "Bearer " + identity.create_session(user_id="bob").token}
        server.identity_service = identity
        server.global_config.deployment_profile = "enterprise"
        server.global_config.organization_id = "ORG-001"
        owner_id = "enterprise:alice"
    else:
        owner_id = SessionStore.owner_id_for(server.global_config)
    store = server.session_store
    session = store.ensure_default_session(owner_id=owner_id, agent_id="momo")
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id=owner_id, agent_id="momo",
        request_id="drain-cancel-fixture", text="fixture", source="test",
        idempotency_key="drain-cancel-fixture",
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def cancel_during_handoff(**scope):
        calls.append(scope)
        entered.set()
        await release.wait()
        return {"ok": True, "status": "cancellation_requested", "terminal": False,
                "request_id": scope["request_id"]}

    runtime.cancel_session_run_during_handoff = cancel_during_handoff
    idle = asyncio.Event()
    idle.set()
    owner = SimpleNamespace(_handoff_draining=True, _shared_committed=True,
                            _handoff_cancel_sealed=False, _handoff_requests=0,
                            _handoff_requests_idle=idle, runtimes=[runtime])
    server.orchestrator = owner
    payload = {"session_id": session["session_id"], "run_id": accepted.run_id}
    if endpoint == "session":
        route = f"/api/v1/sessions/{session['session_id']}/runs/{accepted.run_id}/cancel"
        wrong = f"/api/v1/sessions/other/runs/{accepted.run_id}/cancel"
        wrong_payload = payload
    else:
        route = f"/api/agents/momo/requests/{accepted.request_id}/cancel"
        wrong = route
        wrong_payload = {**payload, "run_id": "wrong-run"}
    async with TestClient(TestServer(server.app)) as client:
        assert (await client.post(route, json=payload)).status == 401
        assert (await client.post(wrong, headers=headers, json=wrong_payload)).status == 404
        if foreign_headers:
            assert (await client.post(route, headers=foreign_headers, json=payload)).status == 404
        owner._shared_committed = False
        assert (await client.post(route, headers=headers, json=payload)).status == 503
        owner._shared_committed = True
        assert calls == []
        pending = asyncio.create_task(client.post(route, headers=headers, json={**payload, "reason": "stop"}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert owner._handoff_requests == 1 and not idle.is_set()
            assert calls == [{"owner_id": owner_id, "session_id": session["session_id"],
                              "run_id": accepted.run_id, "request_id": accepted.request_id,
                              "reason": "stop"}]
            owner._handoff_cancel_sealed = True
            assert (await client.post(route, headers=headers, json={})).status == 503
            assert (await client.post("/api/v1/sessions", headers=headers, json={})).status == 503
            assert (await client.get(f"/api/v1/sessions/{session['session_id']}", headers=headers)).status == 200
        finally:
            release.set()
        response = await asyncio.wait_for(pending, 2)
        body = await response.json()
        assert response.status == 202
        cancellation = body["cancellation"] if endpoint == "session" else body
        assert cancellation["status"] == "cancellation_requested"
        assert store.get_run(accepted.run_id, owner_id=owner_id)["state"] == "running"
        assert owner._handoff_requests == 0 and idle.is_set()


@pytest.mark.asyncio
async def test_handoff_seals_cancel_admission_before_snapshot(monkeypatch):
    from orchestrator import runtime_handoff
    from orchestrator.runtime_app_host import RuntimeAppHost

    gateway = SimpleNamespace(_active_requests={}, _accepting_requests=True)
    app = SimpleNamespace(
        _restart_request=None, reboot_manager=SimpleNamespace(active_operation=None),
        _startup_tasks={}, _handoff_draining=False, _handoff_requests=0,
        api_gateway=gateway, background_job_manager=SimpleNamespace(_monitor_tasks={}),
        function_workers=SimpleNamespace(_telegram_ingress={}), runtimes=[], whatsapp=None,
    )
    monkeypatch.setattr(runtime_handoff, "snapshot", lambda current: {
        "sealed": current._handoff_cancel_sealed, "outstanding": current._handoff_requests,
    })
    host = RuntimeAppHost(None, {})
    host.app = app
    host.task = asyncio.create_task(asyncio.Event().wait())
    try:
        result = await asyncio.wait_for(host.quiesce(), 1)
        assert result == {"sealed": True, "outstanding": 0}
        assert app._handoff_draining is True
    finally:
        host.task.cancel()
        await asyncio.gather(host.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_drain_http_routes_real_stop_to_selected_old_worker(tmp_path):
    from multiprocessing import Pipe

    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.function_worker_host import FunctionWorkerHost
    from orchestrator.function_worker_protocol import JsonConnectionPeer
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle
    from orchestrator.session_store import SessionStore
    from tests.test_scheduler_run_authorization import setup_personal
    from tests.test_workbench_scheduler_api import _server as scheduler_server

    server, fixture_runtime = scheduler_server(tmp_path, persistent_session_v1=True)
    setup_personal(server, fixture_runtime)
    store = server.session_store
    owner_id = SessionStore.owner_id_for(server.global_config)
    session = store.ensure_default_session(owner_id=owner_id, agent_id="momo")
    other_session = store.create_session(owner_id=owner_id, agent_id="momo")

    def start(selected, request_id):
        accepted = store.accept_run(
            session_id=selected["session_id"], owner_id=owner_id, agent_id="momo",
            request_id=request_id, text="fixture", source="test", idempotency_key=request_id,
        )
        store.mark_request_running(request_id, worker_id="fixture-worker")
        return accepted

    selected = start(session, "selected-stop")
    other = start(other_session, "other-stop")
    target_task = asyncio.create_task(asyncio.Event().wait())
    other_task = asyncio.create_task(asyncio.Event().wait())
    runtime = SimpleNamespace(
        name="momo", session_store=store, queue=asyncio.Queue(), current_request_meta=None,
        _generation_tasks_by_request={"selected-stop": target_task, "other-stop": other_task},
    )
    worker = FunctionWorkerHost.__new__(FunctionWorkerHost)
    worker.phase, worker.accepting, worker.agent_name = "DRAINING", False, "momo"
    worker.runtime = runtime

    waiting, finish_quiesce = asyncio.Event(), asyncio.Event()

    async def dispatch(method, params):
        if method == "worker.quiesce":
            waiting.set()
            await finish_quiesce.wait()
            return {"ok": True}
        return await worker.handle_request(method, params)

    caller_connection, worker_connection = Pipe()
    caller_peer = JsonConnectionPeer(caller_connection, label="fixture-caller")
    worker_peer = JsonConnectionPeer(worker_connection, label="fixture-worker", request_handler=dispatch)
    caller_peer.start()
    worker_peer.start()

    class LocalClient:
        agent_name = "momo"

        async def call(self, method, params=None, *, timeout):
            return await caller_peer.request(method, params, timeout=timeout)

    handle = AgentRuntimeHandle(SimpleNamespace(), LocalClient(), {})
    await handle.begin_cutover(shared_handoff=True)
    idle = asyncio.Event()
    idle.set()
    server.orchestrator = SimpleNamespace(
        _handoff_draining=True, _shared_committed=True, _handoff_cancel_sealed=False,
        _handoff_requests=0, _handoff_requests_idle=idle, runtimes=[handle],
    )
    headers = {"X-Workbench-Token": server.admin_token}
    path = f"/api/v1/sessions/{session['session_id']}/runs/{selected.run_id}/cancel"
    quiesce = asyncio.create_task(caller_peer.request("worker.quiesce", {}, timeout=5))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        async with TestClient(TestServer(server.app)) as client:
            response = await client.post(path, headers=headers, json={})
            result = await response.json()
            assert not quiesce.done()
            assert response.status == 202, result
            assert result["cancellation"]["status"] == "cancellation_requested"
            assert result["run"]["state"] == "running"
            assert target_task.cancelling() > 0
            assert not other_task.cancelling()
            assert store.get_run(other.run_id, owner_id=owner_id)["state"] == "running"
    finally:
        finish_quiesce.set()
        await asyncio.gather(quiesce, return_exceptions=True)
        await caller_peer.close()
        await worker_peer.close()
        target_task.cancel()
        other_task.cancel()
        await asyncio.gather(target_task, other_task, return_exceptions=True)
        await handle.abort_cutover()


@pytest.mark.asyncio
async def test_drain_stop_uncertain_worker_outcome_is_not_reported_as_stopped(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.session_store import SessionStore
    from tests.test_scheduler_run_authorization import setup_personal
    from tests.test_workbench_scheduler_api import _server as scheduler_server

    server, runtime = scheduler_server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    owner_id = SessionStore.owner_id_for(server.global_config)
    session = server.session_store.ensure_default_session(owner_id=owner_id, agent_id="momo")
    accepted = server.session_store.accept_run(
        session_id=session["session_id"], owner_id=owner_id, agent_id="momo",
        request_id="unknown-stop", text="fixture", source="test", idempotency_key="unknown-stop",
    )
    server.session_store.mark_request_running(accepted.request_id, worker_id="fixture")

    async def disconnected(**scope):
        raise TimeoutError("fixture connection dropped")

    runtime.cancel_session_run_during_handoff = disconnected
    server.orchestrator = SimpleNamespace(
        _handoff_draining=True, _shared_committed=True, _handoff_cancel_sealed=False,
        _handoff_requests=0, runtimes=[runtime],
    )
    route = f"/api/v1/sessions/{session['session_id']}/runs/{accepted.run_id}/cancel"
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(route, headers={"X-Workbench-Token": server.admin_token}, json={})
        body = await response.json()
        assert response.status == 503
        assert body["error_code"] == "worker_cancel_outcome_unknown"
        assert server.session_store.get_run(accepted.run_id, owner_id=owner_id)["state"] == "running"
        controls = [event for event in server.session_store.events(session["session_id"], owner_id=owner_id)
                    if event["kind"] == "run.control"]
        assert [event["status"] for event in controls] == ["requested", "outcome_unknown"]


@pytest.mark.asyncio
async def test_inflight_exact_stop_prevents_retirement_until_outcome_is_known(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle
    from orchestrator.runtime_app_host import RuntimeAppHost
    from orchestrator.session_store import SessionStore
    from tests.test_scheduler_run_authorization import setup_personal
    from tests.test_workbench_scheduler_api import _server as scheduler_server

    server, fixture_runtime = scheduler_server(tmp_path, persistent_session_v1=True)
    setup_personal(server, fixture_runtime)
    store = server.session_store
    owner_id = SessionStore.owner_id_for(server.global_config)
    session = store.ensure_default_session(owner_id=owner_id, agent_id="momo")
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id=owner_id, agent_id="momo",
        request_id="handoff-race", text="fixture", source="test", idempotency_key="handoff-race",
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    quiescing, finish_quiesce = asyncio.Event(), asyncio.Event()
    stopping, finish_stop = asyncio.Event(), asyncio.Event()

    class OldClient:
        agent_name = "momo"

        async def call(self, method, params=None, *, timeout):
            if method == "worker.quiesce":
                quiescing.set()
                await finish_quiesce.wait()
                return {"ok": True}
            if method == "runtime.cancel_session_run":
                stopping.set()
                await finish_stop.wait()
                return {"ok": True, "status": "cancellation_requested", "terminal": False}
            raise AssertionError(method)

    old_client = OldClient()
    handle = AgentRuntimeHandle(SimpleNamespace(), old_client, {})
    idle = asyncio.Event()
    idle.set()
    app = SimpleNamespace(
        _restart_request=None, reboot_manager=SimpleNamespace(active_operation=None),
        _startup_tasks={}, _handoff_draining=False, _shared_committed=True,
        _handoff_requests=0, _handoff_requests_idle=idle,
        api_gateway=SimpleNamespace(_active_requests={}, _accepting_requests=True),
        background_job_manager=SimpleNamespace(_monitor_tasks={}),
        function_workers=SimpleNamespace(_telegram_ingress={}), runtimes=[handle], whatsapp=None,
    )
    server.orchestrator = app
    host = RuntimeAppHost(None, {})
    host.app = app
    host.task = asyncio.create_task(asyncio.Event().wait())
    resumed = []

    async def resume():
        resumed.append(True)
        await handle.abort_cutover()
        app._handoff_draining = False
        app.api_gateway._accepting_requests = True

    host.resume = resume
    route = f"/api/v1/sessions/{session['session_id']}/runs/{accepted.run_id}/cancel"
    headers = {"X-Workbench-Token": server.admin_token}
    async with TestClient(TestServer(server.app)) as client:
        handoff = asyncio.create_task(host.quiesce())
        try:
            await asyncio.wait_for(quiescing.wait(), 2)
            pending_stop = asyncio.create_task(client.post(route, headers=headers, json={}))
            try:
                await asyncio.wait_for(stopping.wait(), 2)
                assert app._handoff_requests == 1
                finish_quiesce.set()
                with pytest.raises(RuntimeError, match="backend_requests"):
                    await asyncio.wait_for(handoff, 2)
                assert resumed == [True]
                assert handle.client is old_client
            finally:
                finish_stop.set()
            response = await asyncio.wait_for(pending_stop, 2)
            assert response.status == 202
            assert (await response.json())["cancellation"]["terminal"] is False
            assert app._handoff_requests == 0 and idle.is_set()
        finally:
            finish_quiesce.set()
            finish_stop.set()
            host.task.cancel()
            await asyncio.gather(host.task, handoff, return_exceptions=True)

@pytest.mark.asyncio
async def test_real_shared_process_handoff_preserves_session_reads(tmp_path, monkeypatch):
    """Exercise real old/new Functions processes with local fixture data only."""
    import json
    import socket
    from pathlib import Path

    import aiohttp

    from orchestrator.kernel_process import KernelRuntime, ProcessClient, qualify
    from orchestrator.pcm import render_pcm_document
    from orchestrator.runtime_contract import enforce_runtime_contract
    from orchestrator.session_store import SessionStore

    code_root = Path(__file__).resolve().parents[1]
    # Real qualification intentionally refuses uncommitted Function sources.
    # The full runtime journey becomes actionable after the candidate commit.
    import subprocess

    dirty = subprocess.run(
        ["git", "-C", str(code_root), "diff", "--quiet", "HEAD", "--", "orchestrator"],
        capture_output=True, check=False, timeout=60,
    )
    if dirty.returncode == 1:
        pytest.skip("real Function qualification requires committed candidate sources")
    if dirty.returncode:
        raise RuntimeError("cannot inspect candidate source status")
    untracked = subprocess.run(
        ["git", "-C", str(code_root), "ls-files", "--others", "--exclude-standard", "--", "orchestrator"],
        capture_output=True, check=True, timeout=60,
    )
    if untracked.stdout.strip():
        pytest.skip("real Function qualification requires committed candidate sources")
    home = tmp_path / "isolated-instance"
    workspace = home / "workspaces" / "offline_probe"
    workspace.mkdir(parents=True)
    (workspace / "agent.md").write_text(
        render_pcm_document(persona="Offline fixture", system="Local handoff test."),
        encoding="utf-8",
    )
    remote = home / "remote"
    remote.mkdir()
    (remote / "config.yaml").write_text(
        "lifecycle:\n  remote_enabled: false\n  remote_supervised: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HASHI_REMOTE_ROOT", str(home))
    from orchestrator.remote_lifecycle import load_settings
    assert load_settings(home).enabled is False  # Never start a real Remote.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    (home / "agents.json").write_text(json.dumps({
        "global": {"instance_id": "isolated-test", "workbench_port": port},
        "agents": [{
            "name": "offline_probe", "type": "flex", "is_active": False,
            "workspace_dir": str(workspace), "active_backend": "codex-cli",
            "allowed_backends": [{"engine": "codex-cli"}],
        }],
    }), encoding="utf-8")
    (home / "secrets.json").write_text(json.dumps({
        "workbench_admin_token": "isolated-handoff-admin-token-only",
        "hashi_remote_shared_token": "isolated-handoff-remote-token-only",
    }), encoding="utf-8")
    store = SessionStore(
        home / "state" / "sessions.sqlite3",
        instance_id="isolated-test", attachment_root=home / "media" / "session_attachments",
    )
    saved = store.ensure_default_session(owner_id="user:0", agent_id="offline_probe")

    runtime = enforce_runtime_contract(code_root)
    release = await qualify(code_root, home, runtime)
    kernel = KernelRuntime(code_root, home, runtime)
    old = await ProcessClient.prepare(kernel.bootstrap(release, {"agents": []}, initial=True))
    kernel.active, kernel.release = old, release
    try:
        await old.peer.request("activate", timeout=120)
        await old.peer.request("commit", timeout=60)
        old_pid = old.process.pid
        url = f"http://127.0.0.1:{port}"
        headers = {"X-Workbench-Token": "isolated-handoff-admin-token-only"}
        async with aiohttp.ClientSession() as client:
            before = await client.get(url + "/api/v1/sessions", headers=headers)
            assert before.status == 200
            assert saved["session_id"] in {row["session_id"] for row in (await before.json())["sessions"]}

            await old.peer.request("quiesce", timeout=30)
            during = await client.get(url + "/api/v1/sessions", headers=headers)
            assert during.status == 200
            assert saved["session_id"] in {row["session_id"] for row in (await during.json())["sessions"]}
            denied = await client.post(url + "/api/v1/sessions", headers=headers, json={})
            assert denied.status == 503
            await old.peer.request("resume", timeout=30)

            assert await kernel.replace(release)
            assert kernel.active.process.pid != old_pid
            after = await client.get(url + "/api/v1/sessions", headers=headers)
            assert after.status == 200
            assert saved["session_id"] in {row["session_id"] for row in (await after.json())["sessions"]}
    finally:
        if kernel.active is not None:
            await kernel.active.close()
