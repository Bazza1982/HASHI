from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orchestrator.config import GlobalConfig
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.message_context import MESSAGE_CONTEXT_METADATA_KEY
from orchestrator.request_activity import RequestActivityStore
from orchestrator.session_store import SessionStore
from orchestrator.workbench_api import WorkbenchApiServer
from remote.exchange_transport import ExchangeTransport, ExchangeTransportError


hashi_exchange_core = pytest.importorskip("hashi_exchange.core")
hashi_exchange_identity = pytest.importorskip("hashi_exchange.identity")
hashi_exchange_lab = pytest.importorskip("hashi_exchange.lab")
hashi_exchange_server = pytest.importorskip("hashi_exchange.server")


class _Logger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def exception(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


class _HashiAcceptanceRuntime(FlexibleAgentRuntime):
    """Real HASHI admission/queue methods with a deterministic test executor."""

    def __init__(self, root: Path, *, instance_id: str, agent: str):
        self.name = agent
        self.global_config = GlobalConfig(
            0,
            project_root=root,
            bridge_home=root,
            instance_id=instance_id,
            workbench_port=0,
        )
        self.workspace_dir = root / "workspaces" / agent
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.session_store = SessionStore(
            root / "state" / "sessions.sqlite3",
            instance_id=instance_id,
        )
        self.queue = asyncio.Queue()
        self.request_seq = 0
        self.session_id_dt = "exchange-acceptance"
        self.message_logger = _Logger()
        self.error_logger = _Logger()
        self.logger = _Logger()
        self.request_activity = RequestActivityStore()
        self.skill_manager = None
        self.startup_success = True
        self._transfer_state = None
        self._agent_move_quiesced = False
        self._authorized_telegram_ids = []
        self._active_chat_ids = {}


def _write_instance(
    root: Path,
    *,
    instance_id: str,
    agent: str,
    authority: dict,
    credential: str,
    exchange_url: str,
) -> None:
    row = next(
        item
        for item in authority["instances"]
        if agent in item["allowed_agents"]
        and item["instance_id"] in {"ins_home", "ins_server"}
    )
    root.mkdir()
    (root / "agents.json").write_text(
        json.dumps(
            {
                "global": {
                    "instance_id": instance_id,
                    "deployment_profile": "personal",
                    "workbench_port": 0,
                },
                "exchange": {
                    "enabled": True,
                    "authority_id": authority["authority_id"],
                    "url": exchange_url,
                    "registered_instance_id": row["instance_id"],
                    "instance_alias": row["instance_alias"],
                    "credential_ref": "exchange_token",
                    "published_agents": [agent],
                },
                "agents": [
                    {
                        "name": agent,
                        "display_name": agent.title(),
                        "is_active": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "secrets.json").write_text(
        json.dumps(
            {
                "hashi_remote_shared_token": (
                    f"synthetic-local-ingress-{instance_id.lower()}"
                ),
                "exchange_token": credential.strip(),
            }
        ),
        encoding="utf-8",
    )


async def _start_workbench(root: Path, runtime: _HashiAcceptanceRuntime):
    server = WorkbenchApiServer(
        config_path=root / "agents.json",
        global_config=runtime.global_config,
        runtimes=[runtime],
    )
    await server.start()
    return server, server.bound_port


async def _wait_ready(transport: ExchangeTransport, timeout: float = 8) -> None:
    await asyncio.wait_for(transport._ready_event.wait(), timeout=timeout)


async def _wait_outbox_state(
    transport: ExchangeTransport,
    message_id: str,
    state: str,
    *,
    timeout: float = 5,
) -> None:
    async with asyncio.timeout(timeout):
        while True:
            record = transport.outbox.get(message_id)
            if record is not None and record.state == state:
                return
            await asyncio.sleep(0.01)


def _finish_run(
    runtime: _HashiAcceptanceRuntime,
    item,
    answer: str,
) -> dict:
    assert (
        runtime.session_store.mark_request_running(
            item.request_id,
            worker_id="deterministic-hashi-acceptance",
        )
        == 1
    )
    result = runtime.session_store.finish_request(
        item.request_id,
        success=True,
        assistant_text=answer,
        assistant_source="deterministic-hashi-acceptance",
    )
    runtime.queue.task_done()
    return result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_hashi_runtimes_accept_run_reply_and_deduplicate(
    tmp_path,
    monkeypatch,
):
    lab_dir = tmp_path / "authority"
    authority_path = hashi_exchange_lab.provision(lab_dir)
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    exchange = hashi_exchange_core.Exchange(
        hashi_exchange_identity.LocalAuthority(authority_path)
    )

    async with hashi_exchange_server.LabServer(
        exchange,
        host="127.0.0.1",
        port=0,
    ) as lab_server:
        exchange_url = f"ws://127.0.0.1:{lab_server.port}/v1/connect"
        home_root = tmp_path / "hashi_home"
        server_root = tmp_path / "hashi_server"
        _write_instance(
            home_root,
            instance_id="HASHI_HOME",
            agent="planner",
            authority=authority,
            credential=(lab_dir / "home.credential").read_text(
                encoding="utf-8"
            ),
            exchange_url=exchange_url,
        )
        _write_instance(
            server_root,
            instance_id="HASHI_SERVER",
            agent="reviewer",
            authority=authority,
            credential=(lab_dir / "server.credential").read_text(
                encoding="utf-8"
            ),
            exchange_url=exchange_url,
        )
        home_runtime = _HashiAcceptanceRuntime(
            home_root,
            instance_id="HASHI_HOME",
            agent="planner",
        )
        server_runtime = _HashiAcceptanceRuntime(
            server_root,
            instance_id="HASHI_SERVER",
            agent="reviewer",
        )
        home_api, home_port = await _start_workbench(
            home_root,
            home_runtime,
        )
        server_api, server_port = await _start_workbench(
            server_root,
            server_runtime,
        )
        home_transport = ExchangeTransport(
            hashi_root=home_root,
            instance_info={"instance_id": "HASHI_HOME"},
            workbench_port=home_port,
        )
        server_transport = ExchangeTransport(
            hashi_root=server_root,
            instance_info={"instance_id": "HASHI_SERVER"},
            workbench_port=server_port,
        )
        try:
            await home_transport.start()
            await server_transport.start()
            await asyncio.gather(
                _wait_ready(home_transport),
                _wait_ready(server_transport),
            )

            sent = await home_transport.send_message(
                from_agent="planner",
                to_address="reviewer@server.barrytianli",
                text="Please perform the deterministic HASHI acceptance.",
                message_id="message_acceptance_1",
                conversation_id="conversation_acceptance_1",
            )
            assert sent["ok"] is True
            server_item = await asyncio.wait_for(
                server_runtime.queue.get(),
                timeout=5,
            )
            server_context = server_item.request_metadata[
                MESSAGE_CONTEXT_METADATA_KEY
            ]
            server_run = server_runtime.session_store.get_run_by_request(
                server_item.request_id
            )
            assert server_run["state"] == "queued"
            assert server_context["sender"]["claim"] == (
                "planner@home.barrytianli"
            )
            assert server_context["verified_remote_principal"][
                "actor_id"
            ] == "act_barry"
            assert server_context["exchange_message"] == {
                "message_id": "message_acceptance_1",
                "conversation_id": "conversation_acceptance_1",
                "message_type": "agent_message",
                "in_reply_to": None,
                "expires_at": server_context["exchange_message"]["expires_at"],
                "authorization_expires_at": server_context[
                    "exchange_message"
                ]["authorization_expires_at"],
            }

            answer = "Deterministic HASHI acceptance response."
            completed_server_run = _finish_run(
                server_runtime,
                server_item,
                answer,
            )
            main_loop = asyncio.get_running_loop()
            reply_calls = []

            def send_reply(to_agent, from_agent, text, **kwargs):
                reply_calls.append((to_agent, from_agent, text, kwargs))
                future = asyncio.run_coroutine_threadsafe(
                    server_transport.send_message(
                        from_agent=from_agent,
                        to_address=to_agent,
                        text=text,
                        **kwargs,
                    ),
                    main_loop,
                )
                return bool(future.result(timeout=8)["ok"])

            monkeypatch.setattr("tools.hchat_send.send_hchat", send_reply)
            await server_runtime._hchat_route_reply(server_item, answer)

            home_item = await asyncio.wait_for(
                home_runtime.queue.get(),
                timeout=5,
            )
            home_context = home_item.request_metadata[
                MESSAGE_CONTEXT_METADATA_KEY
            ]
            assert home_context["sender"]["claim"] == (
                "reviewer@server.barrytianli"
            )
            assert home_context["exchange_message"]["message_type"] == (
                "agent_reply"
            )
            assert home_context["exchange_message"]["in_reply_to"] == (
                "message_acceptance_1"
            )
            assert home_context["exchange_message"]["conversation_id"] == (
                "conversation_acceptance_1"
            )
            completed_home_run = _finish_run(
                home_runtime,
                home_item,
                answer,
            )
            await home_runtime._hchat_route_reply(
                home_item,
                "must not loop",
            )
            assert len(reply_calls) == 1

            await _wait_outbox_state(
                home_transport,
                "message_acceptance_1",
                "delivered",
            )
            replay = await home_transport.send_message(
                from_agent="planner",
                to_address="reviewer@server.barrytianli",
                text="Please perform the deterministic HASHI acceptance.",
                message_id="message_acceptance_1",
                conversation_id="conversation_acceptance_1",
            )
            assert replay["replayed"] is True
            assert replay["state"] == "delivered"
            await asyncio.sleep(0.05)
            assert server_runtime.queue.empty()

            server_messages = server_runtime.session_store.messages(
                completed_server_run["session_id"]
            )
            home_messages = home_runtime.session_store.messages(
                completed_home_run["session_id"]
            )
            assert [item["role"] for item in server_messages] == [
                "user",
                "assistant",
            ]
            assert [item["role"] for item in home_messages] == [
                "user",
                "assistant",
            ]

            expiring = await home_transport.send_message(
                from_agent="planner",
                to_address="reviewer@server.barrytianli",
                text="Accept this Run before its delivery deadline.",
                message_id="message_short_delivery",
                conversation_id="conversation_long_run",
                expires_in_seconds=1,
            )
            assert expiring["ok"] is True
            long_item = await asyncio.wait_for(
                server_runtime.queue.get(),
                timeout=5,
            )
            await asyncio.sleep(1.05)
            assert server_runtime.session_store.get_run_by_request(
                long_item.request_id
            )["state"] == "queued"
            completed_after_expiry = _finish_run(
                server_runtime,
                long_item,
                "Run completed after the delivery deadline.",
            )
            assert completed_after_expiry["state"] == "completed"

            server_config = json.loads(
                (server_root / "agents.json").read_text(encoding="utf-8")
            )
            server_config["exchange"]["published_agents"] = []
            (server_root / "agents.json").write_text(
                json.dumps(server_config),
                encoding="utf-8",
            )
            async with asyncio.timeout(5):
                while (
                    server_transport.status()["published_agents"] != []
                    or exchange.sessions["ins_server"].agents != {}
                ):
                    await asyncio.sleep(0.02)
            with pytest.raises(
                ExchangeTransportError,
                match="RECIPIENT_UNAVAILABLE",
            ):
                await home_transport.send_message(
                    from_agent="planner",
                    to_address="reviewer@server.barrytianli",
                    text="This hidden target must reject delivery.",
                    message_id="message_hidden_target",
                    conversation_id="conversation_hidden_target",
                )
            assert server_runtime.queue.empty()
            print(
                json.dumps(
                    {
                        "request_receipt": sent["state"],
                        "server_run": completed_server_run["state"],
                        "reply_run": completed_home_run["state"],
                        "server_sender": server_context["sender"]["claim"],
                        "reply_sender": home_context["sender"]["claim"],
                        "reply_conversation": home_context[
                            "exchange_message"
                        ]["conversation_id"],
                        "deduplicated": server_runtime.queue.empty(),
                        "answer": answer,
                    },
                    sort_keys=True,
                )
            )
        finally:
            await asyncio.gather(
                home_transport.stop(),
                server_transport.stop(),
                return_exceptions=True,
            )
            await asyncio.gather(
                home_api.shutdown(),
                server_api.shutdown(),
                return_exceptions=True,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transport_reauthenticates_and_republishes_after_exchange_restart(
    tmp_path,
):
    lab_dir = tmp_path / "authority"
    authority_path = hashi_exchange_lab.provision(lab_dir)
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    first_exchange = hashi_exchange_core.Exchange(
        hashi_exchange_identity.LocalAuthority(authority_path)
    )
    first_server = hashi_exchange_server.LabServer(
        first_exchange,
        host="127.0.0.1",
        port=0,
    )
    await first_server.__aenter__()
    first_active = True
    second_server = None
    transport = None
    try:
        exchange_url = f"ws://127.0.0.1:{first_server.port}/v1/connect"
        root = tmp_path / "hashi_home"
        _write_instance(
            root,
            instance_id="HASHI_HOME",
            agent="planner",
            authority=authority,
            credential=(lab_dir / "home.credential").read_text(
                encoding="utf-8"
            ),
            exchange_url=exchange_url,
        )
        transport = ExchangeTransport(
            hashi_root=root,
            instance_info={"instance_id": "HASHI_HOME"},
            workbench_port=18802,
        )
        await transport.start()
        await _wait_ready(transport)
        first_epoch = transport.status()["connection_epoch"]
        port = first_server.port

        await first_server.__aexit__(None, None, None)
        first_active = False
        async with asyncio.timeout(5):
            while transport.status()["connected"]:
                await asyncio.sleep(0.01)

        second_exchange = hashi_exchange_core.Exchange(
            hashi_exchange_identity.LocalAuthority(authority_path)
        )
        second_server = hashi_exchange_server.LabServer(
            second_exchange,
            host="127.0.0.1",
            port=port,
        )
        await second_server.__aenter__()
        async with asyncio.timeout(10):
            while True:
                status = transport.status()
                if (
                    status["connected"]
                    and status["connection_epoch"] != first_epoch
                    and status["published_agents"] == ["planner"]
                ):
                    break
                await asyncio.sleep(0.02)

        server_session = second_exchange.sessions["ins_home"]
        assert server_session.ready is True
        assert server_session.agents == {
            "planner": {"agent_message", "agent_reply"}
        }
    finally:
        if transport is not None:
            await transport.stop()
        if second_server is not None:
            await second_server.__aexit__(None, None, None)
        if first_active:
            await first_server.__aexit__(None, None, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_receiver_process_restart_requeues_the_same_pao_run(tmp_path):
    lab_dir = tmp_path / "authority"
    authority_path = hashi_exchange_lab.provision(lab_dir)
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    exchange = hashi_exchange_core.Exchange(
        hashi_exchange_identity.LocalAuthority(authority_path)
    )

    async with hashi_exchange_server.LabServer(
        exchange,
        host="127.0.0.1",
        port=0,
    ) as lab_server:
        exchange_url = f"ws://127.0.0.1:{lab_server.port}/v1/connect"
        home_root = tmp_path / "hashi_home"
        receiver_root = tmp_path / "hashi_receiver"
        _write_instance(
            home_root,
            instance_id="HASHI_HOME",
            agent="planner",
            authority=authority,
            credential=(lab_dir / "home.credential").read_text(
                encoding="utf-8"
            ),
            exchange_url=exchange_url,
        )
        _write_instance(
            receiver_root,
            instance_id="HASHI_SERVER",
            agent="reviewer",
            authority=authority,
            credential=(lab_dir / "server.credential").read_text(
                encoding="utf-8"
            ),
            exchange_url=exchange_url,
        )
        first_runtime = _HashiAcceptanceRuntime(
            receiver_root,
            instance_id="HASHI_SERVER",
            agent="reviewer",
        )
        first_api, first_port = await _start_workbench(
            receiver_root,
            first_runtime,
        )
        home_transport = ExchangeTransport(
            hashi_root=home_root,
            instance_info={"instance_id": "HASHI_HOME"},
            workbench_port=18802,
        )
        first_transport = ExchangeTransport(
            hashi_root=receiver_root,
            instance_info={"instance_id": "HASHI_SERVER"},
            workbench_port=first_port,
        )
        second_api = None
        second_transport = None
        try:
            await home_transport.start()
            await first_transport.start()
            await asyncio.gather(
                _wait_ready(home_transport),
                _wait_ready(first_transport),
            )
            sent = await home_transport.send_message(
                from_agent="planner",
                to_address="reviewer@server.barrytianli",
                text="Survive the receiver process restart.",
                message_id="message_receiver_restart",
                conversation_id="conversation_receiver_restart",
            )
            assert sent["ok"] is True
            lost_item = await asyncio.wait_for(
                first_runtime.queue.get(),
                timeout=5,
            )
            original_run = first_runtime.session_store.get_run_by_request(
                lost_item.request_id
            )
            assert original_run["state"] == "queued"

            await first_transport.stop()
            await first_api.shutdown()
            first_transport = None
            first_api = None

            restarted_runtime = _HashiAcceptanceRuntime(
                receiver_root,
                instance_id="HASHI_SERVER",
                agent="reviewer",
            )
            second_api, second_port = await _start_workbench(
                receiver_root,
                restarted_runtime,
            )
            recovered_item = await asyncio.wait_for(
                restarted_runtime.queue.get(),
                timeout=5,
            )
            assert recovered_item.request_id == lost_item.request_id
            assert recovered_item.run_id == lost_item.run_id
            assert recovered_item.message_id == lost_item.message_id
            recovered_run = restarted_runtime.session_store.get_run_by_request(
                recovered_item.request_id
            )
            assert recovered_run["run_id"] == original_run["run_id"]
            assert [
                message["role"]
                for message in restarted_runtime.session_store.messages(
                    recovered_run["session_id"]
                )
            ] == ["user"]

            second_transport = ExchangeTransport(
                hashi_root=receiver_root,
                instance_info={"instance_id": "HASHI_SERVER"},
                workbench_port=second_port,
            )
            await second_transport.start()
            await _wait_ready(second_transport)
            _finish_run(
                restarted_runtime,
                recovered_item,
                "Recovered without a duplicate Run.",
            )
            await second_api._exchange_ingress_service().recover_pending()
            assert second_api._exchange_ingress_service().inbox.scheduled() == []
        finally:
            await home_transport.stop()
            if first_transport is not None:
                await first_transport.stop()
            if second_transport is not None:
                await second_transport.stop()
            if first_api is not None:
                await first_api.shutdown()
            if second_api is not None:
                await second_api.shutdown()
