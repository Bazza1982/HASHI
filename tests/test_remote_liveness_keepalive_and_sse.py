from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import remote.api.server as remote_server
from orchestrator.frontend_delivery import freeze_run_delivery_route
from orchestrator.frontend_incremental_delivery import ACTIVE_RUN_SWEEP_SECONDS
from orchestrator.function_worker_supervisor import FunctionWorkerClient, FunctionWorkerSupervisor
from orchestrator.run_questions import RunQuestions
from orchestrator.session_store import SessionStore
from orchestrator.workbench_api import WorkbenchApiServer
from remote.live_endpoints import read_live_endpoints
from remote.main import HashiRemoteApplication
from remote.peer.base import PeerInfo
from remote.peer.lan import LanDiscovery, _service_info_to_peer
from remote.security.pairing import PairingManager
from remote.security.shared_token import build_auth_headers
from remote.terminal.executor import AuthLevel, TerminalExecutor


def _reserve_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _telegram_route(chat_id: int = 777) -> dict[str, Any]:
    return freeze_run_delivery_route(
        message_source_id="telegram",
        session_surface="telegram",
        session_channel_key=str(chat_id),
        chat_id=chat_id,
        telegram_requested=True,
    )


def _build_test_workbench_server(
    tmp_path: Path,
    *,
    port: int,
    instance_id: str = "HASHI_SSE",
    admin_token: str = "test-wb-admin-token",
) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"instance_id": instance_id},
                "agents": [{"name": "lily", "workspace_dir": "workspaces/lily"}],
            }
        ),
        encoding="utf-8",
    )
    return WorkbenchApiServer(
        config_path=config_path,
        global_config=SimpleNamespace(
            bridge_home=tmp_path,
            project_root=tmp_path,
            instance_id=instance_id,
            authorized_id=1,
            deployment_profile="personal",
            persistent_session_v1=True,
            api_host="127.0.0.1",
            workbench_port=port,
            api_gateway_port=None,
        ),
        secrets={"workbench_admin_token": admin_token},
        orchestrator=SimpleNamespace(
            runtimes=[],
            worker_clients={},
            instance_id=instance_id,
        ),
    )


def test_active_run_sweep_interval_relaxed_from_2s() -> None:
    assert ACTIVE_RUN_SWEEP_SECONDS >= 10.0


def test_run_questions_pending_telegram_deliveries_uses_active_index(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI_TEST")
    questions = RunQuestions(store)

    # Seed 5,000 unrelated connector_delivery_tasks so SQLite would suffer badly if it full-scanned d.
    owner = "user:1"
    bulk_session = store.ensure_default_session(owner_id=owner, agent_id="agent_bulk")
    bulk_run = store.accept_run(
        session_id=bulk_session["session_id"],
        owner_id=owner,
        agent_id="agent_bulk",
        request_id="req-bulk-1",
        source="telegram",
        idempotency_key="idem-bulk-1",
        text="bulk",
        delivery_route=_telegram_route(chat_id=1001),
    )
    now = "2026-11-23T00:00:00+00:00"
    with sqlite3.connect(store.db_path) as conn:
        events = [
            (
                f"evt_bulk_{idx}",
                bulk_session["session_id"],
                bulk_run.run_id,
                10_000 + idx,
                "assistant.output.available",
                "ok",
                "done",
                "durable",
                "",
                "{}",
                now,
            )
            for idx in range(5000)
        ]
        conn.executemany(
            """INSERT INTO run_events(
                event_id, session_id, run_id, sequence, kind, status, phase,
                delivery_class, summary, detail_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            events,
        )
        tasks = [
            (
                f"task_bulk_{idx}",
                bulk_session["session_id"],
                bulk_run.run_id,
                f"evt_bulk_{idx}",
                "telegram",
                "telegram:1001",
                "primary",
                "[]",
                "query_before_retry",
                "pending",
                now,
            )
            for idx in range(5000)
        ]
        conn.executemany(
            """INSERT INTO connector_delivery_tasks(
                task_id, session_id, run_id, event_id, connector_id, endpoint_id,
                role, content_modes_json, retry_class, state, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            tasks,
        )
        conn.commit()

    # Create one real active Telegram question for zelda
    zelda_session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
    zelda_run = store.accept_run(
        session_id=zelda_session["session_id"],
        owner_id=owner,
        agent_id="zelda",
        request_id="req-zelda-q1",
        source="telegram",
        idempotency_key="idem-zelda-q1",
        text="ask question",
        delivery_route=_telegram_route(chat_id=2002),
    )
    with store._connection() as conn:
        conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (zelda_run.run_id,))
    created = questions.create(
        request_id=zelda_run.request_id,
        agent_id="zelda",
        payload={
            "idempotency_key": "q-index-1",
            "question": "Proceed?",
            "options": [{"id": "yes", "label": "Yes"}],
            "allow_free_text": True,
            "purpose": "clarification",
        },
    )

    plan_sql = """
        EXPLAIN QUERY PLAN
        SELECT q.*, e.event_id FROM run_questions AS q
        CROSS JOIN run_events AS e ON e.run_id=q.run_id AND e.kind='run.question.created'
            AND json_extract(e.detail_json,'$.question_id')=q.question_id
        CROSS JOIN connector_delivery_tasks AS d ON d.event_id=e.event_id
        WHERE q.instance_id=? AND q.agent_id=?
            AND q.state IN ('pending','answered') AND q.state='pending'
            AND d.connector_id='telegram' AND (d.state IN ('pending','retry')
                OR (d.state='claimed' AND d.lease_expires_at<=?))
        ORDER BY q.created_at LIMIT ?
    """
    with sqlite3.connect(store.db_path) as conn:
        plan_lines = [
            str(row[3])
            for row in conn.execute(plan_sql, ("HASHI_TEST", "zelda", now, 20)).fetchall()
        ]
    plan_text = "\n".join(plan_lines)
    assert "run_questions_active_by_agent" in plan_text, plan_text
    assert "SCAN d" not in plan_text, plan_text
    assert "USE TEMP B-TREE" not in plan_text, plan_text

    started = time.monotonic()
    zelda_pending = questions.pending_telegram_deliveries(agent_id="zelda")
    empty_pending = questions.pending_telegram_deliveries(agent_id="agent_empty")
    elapsed_ms = (time.monotonic() - started) * 1000.0

    assert len(zelda_pending) == 1
    assert zelda_pending[0]["question_id"] == created["question_id"]
    assert empty_pending == []
    assert elapsed_ms < 50.0, f"expected <50ms, got {elapsed_ms:.2f}ms"


@pytest.mark.asyncio
async def test_workbench_api_sse_pushes_direct_and_worker_ipc_events(tmp_path: Path) -> None:
    port = _reserve_unused_port()
    admin_token = "test-wb-admin-token"
    server = _build_test_workbench_server(
        tmp_path, port=port, instance_id="HASHI_SSE", admin_token=admin_token
    )
    store = server.session_store
    await server.start()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            health_resp = await client.get(f"http://127.0.0.1:{port}/api/health")
            assert health_resp.status_code == 200
            assert "frontend_events_sse_v1" in (health_resp.json().get("capabilities") or [])

        received_events: list[tuple[str, dict[str, Any]]] = []
        ready_seen = asyncio.Event()
        two_changes_seen = asyncio.Event()
        consumer_error: list[BaseException] = []

        async def _consume_sse() -> None:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=3.0, read=10.0, write=3.0, pool=3.0)
                ) as client:
                    async with client.stream(
                        "GET",
                        f"http://127.0.0.1:{port}/api/v2/frontend/events/stream",
                        headers={"X-Workbench-Token": admin_token},
                    ) as resp:
                        assert resp.status_code == 200, f"unexpected status {resp.status_code}"
                        event_name = ""
                        async for line in resp.aiter_lines():
                            if line.startswith("event:"):
                                event_name = line.split(":", 1)[1].strip()
                            elif line.startswith("data:"):
                                payload = json.loads(line.split(":", 1)[1].strip())
                                received_events.append((event_name, payload))
                                if event_name == "ready":
                                    ready_seen.set()
                                elif event_name == "change":
                                    changes = [item for item in received_events if item[0] == "change"]
                                    if len(changes) >= 2:
                                        two_changes_seen.set()
                                        return
            except BaseException as exc:
                consumer_error.append(exc)
                ready_seen.set()
                two_changes_seen.set()

        consumer = asyncio.create_task(_consume_sse())
        await asyncio.wait_for(ready_seen.wait(), timeout=3.0)
        assert not consumer_error, consumer_error[0]

        # 1) Direct SessionStore post-commit event -> pushed immediately to SSE
        session = store.ensure_default_session(owner_id="user:1", agent_id="lily")
        store.accept_run(
            session_id=session["session_id"],
            owner_id="user:1",
            agent_id="lily",
            request_id="req-sse-direct-1",
            source="workbench",
            idempotency_key="idem-sse-direct-1",
            text="hello sse",
        )

        # 2) Worker IPC event forwarded through FunctionWorkerClient -> FunctionWorkerSupervisor
        supervisor = FunctionWorkerSupervisor.__new__(FunctionWorkerSupervisor)
        supervisor.kernel = SimpleNamespace(workbench_api=server)
        supervisor.workers = {}
        worker_client = FunctionWorkerClient.__new__(FunctionWorkerClient)
        worker_client.agent_name = "zelda"
        worker_client.supervisor = supervisor
        await worker_client._handle_event(
            "worker.session_event",
            {
                "agent_id": "zelda",
                "session_id": "sess_zelda_1",
                "run_id": "run_zelda_1",
                "kind": "assistant.output.available",
            },
        )

        await asyncio.wait_for(two_changes_seen.wait(), timeout=3.0)
        await consumer
        assert not consumer_error, consumer_error[0]

        changes = [payload for name, payload in received_events if name == "change"]
        assert any(
            item.get("agent_id") == "lily" and item.get("kind") in {"run.accepted", "assistant.output.available"}
            for item in changes
        ), changes
        assert any(
            item.get("agent_id") == "zelda"
            and item.get("kind") == "assistant.output.available"
            and item.get("run_id") == "run_zelda_1"
            for item in changes
        ), changes
    finally:
        await server.shutdown()


def test_remote_keepalive_pool_reuses_single_tcp_connection(tmp_path: Path) -> None:
    seen_client_addrs: set[tuple[str, int]] = set()
    request_count = 0

    class _KeepAliveHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            nonlocal request_count
            request_count += 1
            seen_client_addrs.add(self.client_address)
            body = json.dumps({"ok": True, "instance_id": "HASHI_KA", "n": request_count}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAliveHandler)
    port = int(httpd.server_address[1])
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        shared_token = "test-shared-token-keepalive-123456"
        (tmp_path / "secrets.json").write_text(
            json.dumps({"hashi_remote_shared_token": shared_token}), encoding="utf-8"
        )
        app = remote_server.create_app(
            instance_info={
                "instance_id": "HASHI_KA",
                "display_name": "HASHI KA",
                "workbench_port": port,
                "platform": "windows",
                "hashi_version": "test",
                "remote_port": 18899,
            },
            pairing_manager=PairingManager(lan_mode=False),
            terminal_executor=TerminalExecutor(lan_mode=False, max_allowed_level=AuthLevel.L0_READ_ONLY),
            workbench_port=port,
            hashi_root=str(tmp_path),
        )
        client = TestClient(app)
        for _ in range(10):
            path = "/workbench/v1/proxy/api/health"
            headers = build_auth_headers(
                shared_token=shared_token,
                method="GET",
                path=path,
                from_instance="WORKBENCH",
                body_bytes=b"",
            )
            resp = client.get(path, headers=headers)
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

        assert request_count == 10
        assert len(seen_client_addrs) <= 2, f"Expected Keep-Alive reuse (<=2 sockets), saw {len(seen_client_addrs)}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.asyncio
async def test_remote_fast_fails_inactive_hashi_and_broadcasts_liveness(tmp_path: Path) -> None:
    dead_port = _reserve_unused_port()
    shared_token = "test-shared-token-liveness-123456"
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": shared_token}), encoding="utf-8"
    )
    app = remote_server.create_app(
        instance_info={
            "instance_id": "HASHI_OFF",
            "display_name": "HASHI Offline Test",
            "workbench_port": dead_port,
            "platform": "windows",
            "hashi_version": "test",
            "remote_port": 18898,
        },
        pairing_manager=PairingManager(lan_mode=False),
        terminal_executor=TerminalExecutor(lan_mode=False, max_allowed_level=AuthLevel.L0_READ_ONLY),
        workbench_port=dead_port,
        hashi_root=str(tmp_path),
    )

    transitions: list[dict[str, Any]] = []
    remote_server.set_hashi_liveness_listener(lambda snap: transitions.append(dict(snap)))
    try:
        # Transition active -> inactive triggers immediate listener callback
        remote_server.record_hashi_liveness(hashi_state="active", health={"ok": True})
        snap = remote_server.refresh_hashi_liveness(timeout=0.3)
        assert snap["hashi_state"] == "inactive"
        assert snap["hashi_running"] is False
        assert any(t.get("hashi_state") == "inactive" for t in transitions)

        client = TestClient(app)

        # /health reports remote ready + hashi inactive (pure in-memory read)
        health_resp = client.get("/health")
        assert health_resp.status_code == 200
        health_data = health_resp.json()
        assert health_data["status"] == "ready"
        assert health_data["remote_status"] == "ready"
        assert health_data["hashi_status"] == "inactive"
        assert health_data["hashi_running"] is False
        assert health_data["can_start_hashi"] is True
        assert health_data["instance"]["hashi_status"] == "inactive"

        # /workbench/v1/status reports workbench_online=False + hashi_status=inactive + can_start_hashi=True
        status_path = "/workbench/v1/status"
        status_headers = build_auth_headers(
            shared_token=shared_token,
            method="GET",
            path=status_path,
            from_instance="WORKBENCH",
            body_bytes=b"",
        )
        status_resp = client.get(status_path, headers=status_headers)
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        assert status_data["remote_status"] == "ready"
        assert status_data["hashi_status"] == "inactive"
        assert status_data["workbench_online"] is False
        assert status_data["can_start_hashi"] is True

        # Proxied GET fast-fails in <250ms with rich inactive metadata (no 42s multi-host walk)
        proxy_path = "/workbench/v1/proxy/api/agents"
        proxy_headers = build_auth_headers(
            shared_token=shared_token,
            method="GET",
            path=proxy_path,
            from_instance="WORKBENCH",
            body_bytes=b"",
        )
        started = time.monotonic()
        proxy_resp = client.get(proxy_path, headers=proxy_headers)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        assert proxy_resp.status_code == 503
        assert elapsed_ms < 250.0, f"Expected fast-fail <250ms, took {elapsed_ms:.1f}ms"
        proxy_data = proxy_resp.json()
        assert proxy_data["remote_status"] == "ready"
        assert proxy_data["remote_alive"] is True
        assert proxy_data["hashi_status"] == "inactive"
        assert proxy_data["hashi_running"] is False
        assert proxy_data["can_start"] is True
        assert proxy_data["reason_code"] == "hashi_inactive"

        # mDNS TXT encoding & decoding carry hashi_state=inactive and can_start_hashi=1
        lan = LanDiscovery(self_instance_id="HASHI_OFF")
        peer = PeerInfo(
            instance_id="HASHI_OFF",
            display_name="HASHI Offline Test",
            host="127.0.0.1",
            port=18898,
            workbench_port=dead_port,
            platform="windows",
            hashi_version="test",
            capabilities=["workbench_gateway_v1", "frontend_events_sse_v1"],
            properties={"hashi_state": "inactive", "can_start_hashi": True},
        )
        service_info = lan._service_info_for_peer(peer)
        decoded_peer = _service_info_to_peer(service_info, self_instance_id="HASHI_OTHER")
        assert decoded_peer is not None
        assert decoded_peer.properties["hashi_state"] == "inactive"
        assert decoded_peer.properties["hashi_running"] is False
        assert decoded_peer.properties["can_start_hashi"] is True

        # HashiRemoteApplication._maintain_discovery_once broadcasts active -> inactive transition
        advertised_states: list[str] = []

        class _FakeDiscovery:
            backend_name = "lan"

            def get_status(self) -> dict[str, Any]:
                return {"backend": "lan", "readiness": "ready"}

            async def update_advertisement(self, info: PeerInfo) -> bool:
                advertised_states.append(str(info.properties.get("hashi_state")))
                return True

        class _FakeProtocolManager:
            def get_cached_local_agent_directory_state(self) -> dict[str, Any]:
                return {"version": "v1", "directory_state": "ready"}

            def record_shared_token_config_state(self, *_args: Any) -> None:
                return None

            def reload_shared_token(self, *_args: Any) -> bool:
                return False

        remote_app = HashiRemoteApplication(hashi_root=tmp_path, port=18898, use_tls=False)
        remote_app._discoveries = [_FakeDiscovery()]
        remote_app._protocol_manager = _FakeProtocolManager()
        remote_app._last_shared_token_source_signature = remote_app._shared_token_source_signature()

        remote_server.record_hashi_liveness(hashi_state="active", health={"ok": True})
        res1 = await remote_app._maintain_discovery_once(
            instance_info={"display_name": "HASHI Offline Test", "platform": "windows", "hashi_version": "test"},
            instance_id="HASHI_OFF",
            workbench_port=dead_port,
            local_capabilities=["workbench_gateway_v1"],
        )
        assert res1["refreshed"] is True
        assert advertised_states[-1] == "active"

        remote_server.record_hashi_liveness(hashi_state="inactive", health=None)
        res2 = await remote_app._maintain_discovery_once(
            instance_info={"display_name": "HASHI Offline Test", "platform": "windows", "hashi_version": "test"},
            instance_id="HASHI_OFF",
            workbench_port=dead_port,
            local_capabilities=["workbench_gateway_v1"],
        )
        assert res2["refreshed"] is True
        assert advertised_states[-1] == "inactive"
        live = read_live_endpoints(tmp_path)
        assert live["hashi_off"]["hashi_state"] == "inactive"
        assert live["hashi_off"]["can_start_hashi"] is True
    finally:
        remote_server.set_hashi_liveness_listener(None)


def test_shared_token_peer_can_start_inactive_hashi_via_living_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_token = "test-shared-token-peer-start-123456"
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": shared_token}), encoding="utf-8"
    )
    app = remote_server.create_app(
        instance_info={
            "instance_id": "HASHI_TARGET",
            "display_name": "HASHI Target",
            "workbench_port": 18897,
            "platform": "windows",
            "hashi_version": "test",
            "remote_port": 18896,
        },
        pairing_manager=PairingManager(lan_mode=False),
        terminal_executor=TerminalExecutor(lan_mode=False, max_allowed_level=AuthLevel.L0_READ_ONLY),
        workbench_port=18897,
        hashi_root=str(tmp_path),
    )

    state = {"running": False}
    transitions: list[str] = []
    remote_server.set_hashi_liveness_listener(
        lambda snap: transitions.append(str(snap.get("hashi_state")))
    )
    try:
        def _fake_fetch_health(timeout: float = 1.0) -> dict[str, Any] | None:
            if state["running"]:
                health = {"ok": True, "instance_id": "HASHI_TARGET"}
                remote_server.record_hashi_liveness(hashi_state="active", health=health)
                return health
            remote_server.record_hashi_liveness(hashi_state="inactive", health=None)
            return None

        def _fake_start_hashi() -> dict[str, Any]:
            state["running"] = True
            return {
                "pid": 42424,
                "command": ["python", "-m", "main"],
                "log_path": str(tmp_path / "start.log"),
                "launcher_kind": "test",
                "platform": "windows",
            }

        monkeypatch.setattr(remote_server, "_fetch_workbench_health", _fake_fetch_health)
        monkeypatch.setattr(remote_server, "_start_hashi_process", _fake_start_hashi)
        monkeypatch.setattr(
            remote_server,
            "_read_hashi_pid_state",
            lambda: {
                "pid_file_exists": state["running"],
                "pid": 42424 if state["running"] else None,
                "pid_alive": state["running"],
            },
        )

        # Peer HASHI9 authenticates with Shared Token HMAC and starts inactive HASHI_TARGET
        body = json.dumps({"reason": "peer_recovery_from_hashi9"}).encode("utf-8")
        path = "/control/hashi/start"
        headers = {
            "Content-Type": "application/json",
            **build_auth_headers(
                shared_token=shared_token,
                method="POST",
                path=path,
                from_instance="HASHI9",
                body_bytes=body,
            ),
        }
        client = TestClient(app)
        resp = client.post(path, content=body, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["started"] is True
        assert data["status"]["hashi_running"] is True
        assert "starting" in transitions
        assert transitions[-1] == "active"

        health = client.get("/health").json()
        assert health["hashi_status"] == "active"
        assert health["hashi_running"] is True
    finally:
        remote_server.set_hashi_liveness_listener(None)


@pytest.mark.asyncio
async def test_remote_proxy_streams_sse_events_live(tmp_path: Path) -> None:
    wb_port = _reserve_unused_port()
    admin_token = "test-wb-admin-token"
    wb_server = _build_test_workbench_server(
        tmp_path,
        port=wb_port,
        instance_id="HASHI_PROXY_SSE",
        admin_token=admin_token,
    )
    store = wb_server.session_store
    await wb_server.start()
    try:
        shared_token = "test-shared-token-proxy-sse-123456"
        (tmp_path / "secrets.json").write_text(
            json.dumps(
                {
                    "hashi_remote_shared_token": shared_token,
                    "workbench_admin_token": admin_token,
                }
            ),
            encoding="utf-8",
        )
        remote_app = remote_server.create_app(
            instance_info={
                "instance_id": "HASHI_PROXY_SSE",
                "display_name": "HASHI Proxy SSE",
                "workbench_port": wb_port,
                "platform": "windows",
                "hashi_version": "test",
                "remote_port": 18895,
            },
            pairing_manager=PairingManager(lan_mode=False),
            terminal_executor=TerminalExecutor(lan_mode=False, max_allowed_level=AuthLevel.L0_READ_ONLY),
            workbench_port=wb_port,
            hashi_root=str(tmp_path),
        )

        remote_port = _reserve_unused_port()
        import uvicorn

        uv_config = uvicorn.Config(
            app=remote_app,
            host="127.0.0.1",
            port=remote_port,
            log_level="error",
            access_log=False,
        )
        uv_server = uvicorn.Server(uv_config)
        uv_task = asyncio.create_task(uv_server.serve())
        # Wait briefly until uvicorn binds and starts serving
        for _ in range(40):
            if uv_server.started:
                break
            await asyncio.sleep(0.05)
        assert uv_server.started, "uvicorn failed to start in test"

        proxy_path = "/workbench/v1/proxy/api/v2/frontend/events/stream"
        headers = build_auth_headers(
            shared_token=shared_token,
            method="GET",
            path=proxy_path,
            from_instance="WORKBENCH",
            body_bytes=b"",
        )
        headers["Accept"] = "text/event-stream"

        ready_seen = asyncio.Event()
        change_seen = asyncio.Event()
        frames: list[tuple[str, dict[str, Any]]] = []
        consumer_error: list[BaseException] = []

        async def _consume_via_remote() -> None:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=3.0, read=10.0, write=3.0, pool=3.0),
                ) as client:
                    async with client.stream(
                        "GET",
                        f"http://127.0.0.1:{remote_port}{proxy_path}",
                        headers=headers,
                    ) as resp:
                        assert resp.status_code == 200, f"unexpected status {resp.status_code}"
                        assert "text/event-stream" in (resp.headers.get("content-type") or "")
                        event_name = ""
                        async for line in resp.aiter_lines():
                            if line.startswith("event:"):
                                event_name = line.split(":", 1)[1].strip()
                            elif line.startswith("data:"):
                                payload = json.loads(line.split(":", 1)[1].strip())
                                frames.append((event_name, payload))
                                if event_name == "ready":
                                    ready_seen.set()
                                elif event_name == "change":
                                    change_seen.set()
                                    return
            except BaseException as exc:
                consumer_error.append(exc)
                ready_seen.set()
                change_seen.set()

        try:
            task = asyncio.create_task(_consume_via_remote())
            await asyncio.wait_for(ready_seen.wait(), timeout=3.0)
            assert not consumer_error, consumer_error[0]

            session = store.ensure_default_session(owner_id="user:1", agent_id="sakura")
            store.accept_run(
                session_id=session["session_id"],
                owner_id="user:1",
                agent_id="sakura",
                request_id="req-proxy-sse-1",
                source="workbench",
                idempotency_key="idem-proxy-sse-1",
                text="hi via remote sse",
            )

            await asyncio.wait_for(change_seen.wait(), timeout=3.0)
            await task
            assert not consumer_error, consumer_error[0]

            changes = [payload for name, payload in frames if name == "change"]
            assert any(item.get("agent_id") == "sakura" for item in changes), changes
        finally:
            uv_server.should_exit = True
            await asyncio.wait_for(uv_task, timeout=5.0)
    finally:
        await wb_server.shutdown()

def test_frontend_stream_kinds_cover_permission_progress_message_update_question_consumed(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI_TEST")
    seen: list[tuple[str, str, str, str | None]] = []
    store._session_change_listeners.append(
        lambda agent_id, session_id, kind, run_id: seen.append(
            (agent_id, session_id, kind, run_id)
        )
    )
    session = store.ensure_default_session(owner_id="user:1", agent_id="lily")
    run = store.accept_run(
        session_id=session["session_id"],
        owner_id="user:1",
        agent_id="lily",
        request_id="req-kinds-1",
        source="workbench",
        idempotency_key="idem-kinds-1",
        text="hello kinds",
    )
    run_id = str(run.run_id)
    seen.clear()
    with store._connection() as connection:
        for kind in (
            "run.progress",
            "frontend.message.updated",
            "run.question.consumed",
        ):
            store._append_event(
                connection,
                session_id=str(session["session_id"]),
                run_id=run_id,
                kind=kind,
                status="running",
                phase="waiting_permission",
                summary="permission wait",
            )
    notified = {kind for _, _, kind, _ in seen}
    assert {
        "run.progress",
        "frontend.message.updated",
        "run.question.consumed",
    } <= notified

    # Internal telemetry outside the UI contract stays off the stream.
    seen.clear()
    with store._connection() as connection:
        store._append_event(
            connection,
            session_id=str(session["session_id"]),
            run_id=run_id,
            kind="internal.telemetry.noise",
            status="running",
            phase="sampling",
            summary="not for the frontend",
        )
    assert seen == []


async def test_frontend_stream_scopes_events_to_authenticated_owner_in_governed_profile(
    tmp_path: Path,
) -> None:
    port = _reserve_unused_port()
    server = _build_test_workbench_server(
        tmp_path, port=port, instance_id="HASHI_GOV", admin_token="test-token"
    )
    store = server.session_store
    session_a = store.ensure_default_session(owner_id="enterprise:user-a", agent_id="lily")
    session_b = store.ensure_default_session(owner_id="enterprise:user-b", agent_id="lily")
    sid_a = str(session_a["session_id"])
    sid_b = str(session_b["session_id"])

    queue_a: asyncio.Queue = asyncio.Queue(maxsize=8)
    queue_b: asyncio.Queue = asyncio.Queue(maxsize=8)
    queue_all: asyncio.Queue = asyncio.Queue(maxsize=8)
    # Governed-profile subscribers carry the authenticated owner scope.
    server._frontend_event_subscribers.add((queue_a, None, None, "enterprise:user-a"))
    server._frontend_event_subscribers.add((queue_b, None, None, "enterprise:user-b"))
    # Personal-profile subscribers (unscoped) keep receiving everything.
    server._frontend_event_subscribers.add((queue_all, None, None, None))

    server.publish_frontend_session_event("lily", sid_a, "run.accepted", None)
    server.publish_frontend_session_event("lily", sid_b, "run.accepted", None)
    server.publish_frontend_session_event("lily", sid_a, "run.started", None)

    await asyncio.sleep(0)
    a_items = [queue_a.get_nowait() for _ in range(queue_a.qsize())]
    b_items = [queue_b.get_nowait() for _ in range(queue_b.qsize())]
    all_items = [queue_all.get_nowait() for _ in range(queue_all.qsize())]

    assert {item["session_id"] for item in a_items} == {sid_a}
    assert {item["session_id"] for item in b_items} == {sid_b}
    assert {item["session_id"] for item in all_items} == {sid_a, sid_b}

    # Unknown/deleted sessions are never delivered to owner-scoped subscribers.
    server.publish_frontend_session_event("lily", "ses_missing_owner", "run.started", None)
    await asyncio.sleep(0)
    assert queue_a.qsize() == 0
    assert queue_b.qsize() == 0
    assert queue_all.qsize() == 1
# HASHI-Fingerprint: hfp_6d6e6e1f893e4a5f8d5fad8ff3083f73
