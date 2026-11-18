"""Exchange recovery must not enqueue the same live Run twice."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from orchestrator.config import GlobalConfig
from orchestrator.exchange_ingress import ExchangeIngressService
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.request_activity import RequestActivityStore
from orchestrator.session_store import SessionStore
from tests.test_exchange_ingress import _configure, _delivery, _evidence


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


class _Runtime(FlexibleAgentRuntime):
    def __init__(self, root):
        self.name = "reviewer"
        self.global_config = GlobalConfig(
            0, project_root=root, bridge_home=root,
            instance_id="HASHI2", workbench_port=0,
        )
        self.workspace_dir = root / "workspaces" / self.name
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.session_store = SessionStore(root / "state/sessions.sqlite3", instance_id="HASHI2")
        self.queue = asyncio.Queue()
        self.request_seq = 0
        self.session_id_dt = "exchange-replay-test"
        self.message_logger = self.error_logger = self.logger = _Logger()
        self.request_activity = RequestActivityStore()
        self.config = SimpleNamespace()
        self.backend_manager = SimpleNamespace(_read_state_dict=lambda: {})
        self._execution_admissions = {}
        self._session_execution_tasks = {}
        self.skill_manager = None
        self.startup_success = True
        self._transfer_state = None
        self._agent_move_quiesced = False
        self._authorized_telegram_ids = []
        self._active_chat_ids = {}


def test_scheduled_exchange_recovery_only_enqueues_when_no_live_execution_owns_the_run(tmp_path):
    async def exercise():
        _configure(tmp_path)
        runtime = _Runtime(tmp_path)
        service = ExchangeIngressService(
            hashi_root=tmp_path, runtime_map=lambda: {"reviewer": runtime},
            session_store=runtime.session_store,
        )
        prompt, evidence = _evidence(tmp_path, _delivery())
        service.accept(evidence=evidence, prompt=prompt)
        first = await service.schedule(evidence=evidence, prompt=prompt)
        assert runtime.queue.qsize() == 1
        assert runtime.session_store.get_run_by_request(first.request_id)["state"] == "queued"

        await service.recover_pending(recover_scheduled=True)
        assert runtime.queue.qsize() == 1

        # The executor has picked up the item but has not marked it running yet.
        item = runtime.queue.get_nowait()
        runtime.queue.task_done()
        runtime._execution_admissions.pop(item.request_id, None)
        runtime._session_execution_tasks[item.request_id] = object()
        await service.recover_pending(recover_scheduled=True)
        assert runtime.queue.empty()

        # A new process has no live in-memory owner; a queued durable Run must
        # still be reconstructed, and further recovery must not duplicate it.
        restarted = _Runtime(tmp_path)
        recovered = ExchangeIngressService(
            hashi_root=tmp_path, runtime_map=lambda: {"reviewer": restarted},
            session_store=restarted.session_store,
        )
        await recovered.recover_pending(recover_scheduled=True)
        assert restarted.queue.qsize() == 1
        assert restarted.queue._queue[0].request_id == first.request_id
        await recovered.recover_pending(recover_scheduled=True)
        assert restarted.queue.qsize() == 1

    asyncio.run(exercise())



def test_original_and_recovery_admissions_racing_publish_once(tmp_path, monkeypatch):
    import threading

    from orchestrator.message_context import seal_connector_evidence

    async def exercise():
        _configure(tmp_path)
        runtime = _Runtime(tmp_path)
        service = ExchangeIngressService(
            hashi_root=tmp_path, runtime_map=lambda: {"reviewer": runtime},
            session_store=runtime.session_store,
        )
        prompt, evidence = _evidence(tmp_path, _delivery())
        principal, delivery = service._verified(evidence=evidence, prompt=prompt)
        run_evidence = seal_connector_evidence(
            tmp_path, claims=service._runtime_claims(principal, delivery), prompt=prompt,
        )
        owner = "synthetic-owner"
        session = runtime.session_store.resolve_primary_session(
            owner_id=owner, agent_id="reviewer", establish=True,
        )
        metadata = {
            "_connector_evidence": run_evidence, "owner_id": owner,
            "session_id": session["session_id"], "connector_id": "exchange",
            "ingress_transport": "exchange.wss", "session_surface": "workbench",
            "session_channel_key": "default",
        }
        first_entered, release_first = threading.Event(), threading.Event()
        original = runtime.session_store.session_workspace
        counter_lock = threading.Lock()
        calls = 0

        def pause_first(*args, **kwargs):
            nonlocal calls
            with counter_lock:
                calls += 1
                first = calls == 1
            if first:
                first_entered.set()
                assert release_first.wait(10)
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime.session_store, "session_workspace", pause_first)
        args = dict(source="hchat-exchange", request_metadata=metadata,
                    idempotency_key="hchat:message_1")
        original_task = asyncio.create_task(runtime.enqueue_api_text(prompt, **args))
        try:
            assert await asyncio.to_thread(first_entered.wait, 5)
            recovered_id = await asyncio.wait_for(runtime.enqueue_api_text(prompt, **args), 8)
            assert runtime.queue.qsize() == 1
            release_first.set()
            original_id = await asyncio.wait_for(original_task, 8)
            assert original_id == recovered_id
            assert runtime.queue.qsize() == 1
            assert runtime.queue._queue[0].request_id == original_id
        finally:
            release_first.set()
            await asyncio.gather(original_task, return_exceptions=True)

    asyncio.run(exercise())
