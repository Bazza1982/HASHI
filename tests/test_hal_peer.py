"""Two isolated instance identities over the real Remote HMAC and Session store."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import socket
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
import uvicorn
from aiohttp import web
from aiohttp.test_utils import TestServer

from hal.dispatch import GatewayDispatchAdapter
from hal.peer import HalPeerClient, RemoteTerminalUnavailable
from hal.service import HalService, handle_hal_peer
from hal.store import HalStore
from orchestrator.session_store import SessionStore
from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.terminal.executor import TerminalExecutor


class SessionRuntime:
    """Runs are admitted through the real SessionStore; finish is test-driven."""

    def __init__(self, store, owner):
        self.store = store
        self.owner = owner
        self.accepted = []

    async def enqueue_api_text(self, text, *, source, idempotency_key, request_metadata, **_):
        session_id = request_metadata.get("session_id")
        if not session_id:
            session_id = self.store.ensure_default_session(owner_id=self.owner, agent_id="temp")["session_id"]
        accepted = self.store.accept_run(
            session_id=session_id, owner_id=self.owner,
            agent_id="temp", request_id=str(uuid4()), text=text,
            source=source, idempotency_key=idempotency_key,
        )
        if not accepted.replayed:
            self.accepted.append(accepted.request_id)
        return accepted.request_id


@pytest.mark.parametrize("credential_error", [OSError, ValueError])
def test_peer_credential_failure_keeps_the_local_hal_loop_running(tmp_path, monkeypatch, credential_error):
    def unreadable(_root):
        raise credential_error("test credential read failure")

    monkeypatch.setattr("hal.peer.load_shared_token", unreadable)
    store = SessionStore(tmp_path / "session.sqlite", instance_id="HALTEST-A")
    owner = "user:7"
    home = SessionRuntime(store, owner)
    peer = HalPeerClient(tmp_path, "HALTEST-A")
    service = HalService(
        store=HalStore(tmp_path / "hal.sqlite"),
        session_store=store,
        dispatch=GatewayDispatchAdapter(SimpleNamespace(_runtime_map=lambda: {"temp": home})),
        peer=peer,
    )

    for target in ("HALTEST-B", "HALTEST-A"):
        rule, _ = service.upsert_rule(
            owner_id=owner, name=target,
            script=f'if @temp@HALTEST-A #turn_finish then @temp@{target} $prompt "{target}".',
        )
        service.set_enabled(owner_id=owner, rule_id=rule["rule_id"], enabled=True)

    session = store.ensure_default_session(owner_id=owner, agent_id="temp")
    initial = store.accept_run(
        session_id=session["session_id"], owner_id=owner, agent_id="temp",
        request_id="initial", text="start", source="workbench", idempotency_key="initial",
    )
    store.finish_request(initial.request_id, success=True, assistant_text="done")

    async def scenario():
        service.start_loop()
        loop = service._loop_task
        try:
            async with asyncio.timeout(3):
                while not home.accepted:
                    if loop.done():
                        await loop
                    await asyncio.sleep(0.01)
                while not any(run["state"] == "completed" for run in service.list_runs(owner, 10)):
                    await asyncio.sleep(0.01)
            assert not loop.done()
            remote_run = next(run for run in service.list_runs(owner, 10) if run["rule_name"] == "HALTEST-B")
            receipt, = service.run_detail(owner, remote_run["exec_id"])["receipts"]
            assert receipt["state"] == "pending_retry"
            assert "authentication unavailable" in receipt["detail"]
            with pytest.raises(RemoteTerminalUnavailable, match="authentication unavailable"):
                await peer.terminal_event("HALTEST-B", "temp", receipt["idempotency_key"])
        finally:
            service.stop_loop()
            with suppress(asyncio.CancelledError):
                await loop

    asyncio.run(scenario())


def test_authenticated_relay_recovers_after_disconnect_without_duplicate_dispatch(tmp_path):
    async def scenario():
        source_root = tmp_path / "source"
        target_root = tmp_path / "target"
        for root in (source_root, target_root):
            root.mkdir()
            (root / "secrets.json").write_text(
                json.dumps({"hashi_remote_shared_token": "test-hal-peer-token"}), encoding="utf-8",
            )
        source = SessionStore(source_root / "session.sqlite", instance_id="HALTEST-A")
        target = SessionStore(target_root / "session.sqlite", instance_id="HALTEST-B")
        owner = "user:7"
        home_runtime = SessionRuntime(source, owner)
        target_runtime = SessionRuntime(target, owner)
        destination = SimpleNamespace(
            session_store=target,
            _v1_owner_id=lambda _request: owner,
            _check_admin_auth=lambda _request: True,
            _runtime_map=lambda: {"temp": target_runtime},
        )

        async def local_peer(request):
            return await handle_hal_peer(destination, request)

        async def health(_request):
            return web.json_response({"ok": True})

        target_api = web.Application()
        target_api.router.add_post("/api/v1/hal/peer", local_peer)
        target_api.router.add_get("/api/health", health)
        async with TestServer(target_api) as api:
            remote_app = create_app(
                {"instance_id": "HALTEST-B", "display_name": "Isolated target"},
                PairingManager(storage_dir=target_root / "pairing", lan_mode=False),
                TerminalExecutor(), hashi_root=str(target_root), workbench_port=api.port,
            )
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                remote = uvicorn.Server(uvicorn.Config(
                    remote_app, lifespan="off", log_level="error",
                ))
                thread = threading.Thread(target=remote.run, kwargs={"sockets": [listener]}, daemon=True)
                thread.start()
                try:
                    async with asyncio.timeout(5):
                        while not remote.started:
                            await asyncio.sleep(0.01)
                    directory = source_root / "instances.json"
                    route = json.dumps({"instances": {"HALTEST-B": {"host": "127.0.0.1", "remote_port": port}}})
                    directory.write_text(route, encoding="utf-8")
                    adapter = GatewayDispatchAdapter(SimpleNamespace(
                        _runtime_map=lambda: {"temp": home_runtime},
                    ))
                    peer = HalPeerClient(source_root, "HALTEST-A")
                    state_path = source_root / "hal.sqlite"
                    service = HalService(store=HalStore(state_path), session_store=source, dispatch=adapter, peer=peer)
                    rule, _ = service.upsert_rule(
                        owner_id=owner, name="round trip",
                        script=(
                            'if @temp@HALTEST-A #turn_finish then @temp@HALTEST-B $prompt "review"; '
                            'if @temp@HALTEST-B #turn_finish then @temp@HALTEST-A $prompt "finish".'
                        ),
                    )
                    service.set_enabled(owner_id=owner, rule_id=rule["rule_id"], enabled=True)
                    session = source.ensure_default_session(owner_id=owner, agent_id="temp")
                    initial = source.accept_run(
                        session_id=session["session_id"], owner_id=owner, agent_id="temp",
                        request_id="initial", text="start", source="workbench", idempotency_key="initial",
                    )
                    source.finish_request(initial.request_id, success=True, assistant_text="done")
                    await service._process_owner(owner)
                    assert len(target_runtime.accepted) == 1
                    initial_exec = service.list_runs(owner, 10)[0]
                    assert initial_exec["stage"] == 1
                    assert not home_runtime.accepted
                    key = service.run_detail(owner, initial_exec["exec_id"])["receipts"][0]["idempotency_key"]

                    repeated = await peer.dispatch_prompt("haltest-b", "temp", "review", key)
                    assert repeated.accepted
                    assert len(target_runtime.accepted) == 1

                    source.finish_request(initial.request_id, success=True, assistant_text="duplicate")
                    directory.write_text('{"instances": {}}', encoding="utf-8")
                    target.finish_request(target_runtime.accepted[0], success=True, assistant_text="reviewed")
                    await service._process_owner(owner)
                    assert "unavailable" in service.list_runs(owner, 10)[0]["detail"]
                    assert not home_runtime.accepted

                    directory.write_text(route, encoding="utf-8")
                    resumed = HalService(store=HalStore(state_path), session_store=source, dispatch=adapter, peer=peer)
                    await resumed._process_owner(owner)
                    await resumed._process_owner(owner)
                    assert len(home_runtime.accepted) == 1
                    assert len(target_runtime.accepted) == 1
                    result = resumed.run_detail(owner, initial_exec["exec_id"])
                    assert result["state"] == "completed"
                    assert [receipt["target_instance"].upper() for receipt in result["receipts"]] == ["HALTEST-B", "HALTEST-A"]
                    final = source.get_run_by_request(home_runtime.accepted[0], owner_id=owner, agent_id="temp")
                    assert final["idempotency_key"] == result["receipts"][1]["idempotency_key"]
                finally:
                    remote.should_exit = True
                    await asyncio.to_thread(thread.join, 5)

    asyncio.run(scenario())
